import re
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from src.tasks.daily_digest import _METRICS_SQL, build_digest_text, main


def _rows(**kwargs):
    row = {
        "scores_generated": 0, "county_rank_reports_delivered": 0, "cold_emails_dispatched": 0,
        "open_rate_pct": None, "click_rate_pct": None, "reply_rate_pct": None,
        "appointments_booked": 0,
    }
    row.update(kwargs)
    return row


def test_build_digest_text_includes_all_seven_metrics():
    with patch("src.tasks.daily_digest._query_metrics", return_value=_rows(
        scores_generated=12, county_rank_reports_delivered=3, cold_emails_dispatched=340,
        open_rate_pct=41.2, click_rate_pct=9.8, reply_rate_pct=5.5, appointments_booked=6,
    )):
        text_out = build_digest_text()
    for expected in ("12", "3", "340", "41.2", "9.8", "5.5", "6"):
        assert expected in text_out
    # v2 spec, verbatim: "not ghost-shopper latency or video completion rate"
    assert "latency" not in text_out.lower()
    assert "video" not in text_out.lower()


def test_build_digest_text_rounds_full_precision_decimal_to_one_place():
    """Postgres numeric division comes back as a many-digit Decimal — the
    digest must round it to 1 decimal place, not interpolate it raw."""
    with patch("src.tasks.daily_digest._query_metrics", return_value=_rows(
        open_rate_pct=Decimal("41.176470588235294118"),
    )):
        text_out = build_digest_text()
    assert "41.2" in text_out
    assert "41.176470588235294118" not in text_out


def test_build_digest_text_data_unavailable_notice_on_query_failure():
    with patch("src.tasks.daily_digest._query_metrics", side_effect=RuntimeError("replica down")):
        text_out = build_digest_text()
    assert "Data Unavailable" in text_out


def test_main_posts_to_command_channel():
    # AsyncMock, not MagicMock: post_notice is `async def`, and main() runs
    # it through asyncio.run, which requires a real awaitable.
    with patch("src.tasks.daily_digest._query_metrics", return_value=_rows()), \
         patch("src.tasks.daily_digest.flush_pending", return_value=0), \
         patch("src.tasks.daily_digest.post_notice", new_callable=AsyncMock) as mock_post:
        main()
    mock_post.assert_awaited_once()
    assert mock_post.call_args.kwargs["channel_key"] == "command"


def test_main_flushes_buffered_events_before_posting():
    with patch("src.tasks.daily_digest._query_metrics", return_value=_rows()), \
         patch("src.tasks.daily_digest.flush_pending", return_value=3) as mock_flush, \
         patch("src.tasks.daily_digest.post_notice", new_callable=AsyncMock):
        main()
    mock_flush.assert_called_once()


def test_metrics_sql_uses_owner_score_generated_not_ghost_shopper():
    """v2 spec correction: the digest must read from owner_score_generated,
    never from the permanently-deferred ghost-shopper events."""
    assert "owner_score_generated" in _METRICS_SQL
    assert "ghost_shopper" not in _METRICS_SQL
    assert "audit_pdf_generated" not in _METRICS_SQL
    assert "audit_reply_received" not in _METRICS_SQL
    assert "sendspark_engagement" not in _METRICS_SQL


def test_metrics_sql_filters_open_and_click_rate_denominators_to_email_only():
    """PR review fix, still required under v2: open_rate_pct/click_rate_pct
    must divide only by email-channel outbound_touch_dispatched events, not
    every channel. Regression guard on the SQL text — every other test in
    this file mocks _query_metrics() and cannot see a bug inside
    _METRICS_SQL. A live-Postgres test is the stronger check."""
    open_rate_clause = re.search(r"AS open_rate_pct", _METRICS_SQL)
    click_rate_clause = re.search(r"AS click_rate_pct", _METRICS_SQL)
    assert open_rate_clause and click_rate_clause

    open_rate_denominator = _METRICS_SQL[:open_rate_clause.start()].rsplit("NULLIF(", 1)[-1]
    click_rate_denominator = _METRICS_SQL[:click_rate_clause.start()].rsplit("NULLIF(", 1)[-1]
    for denominator in (open_rate_denominator, click_rate_denominator):
        assert "outbound_touch_dispatched" in denominator
        assert "payload->>'channel' = 'email'" in denominator


def test_metrics_sql_reply_rate_denominator_is_also_email_only():
    """reply_rate_pct is a new v2 metric — its denominator must follow the
    same email-only convention as open/click rate, for the same reason
    (SMS/call touches can never produce an email reply)."""
    reply_rate_clause = re.search(r"AS reply_rate_pct", _METRICS_SQL)
    assert reply_rate_clause
    reply_rate_denominator = _METRICS_SQL[:reply_rate_clause.start()].rsplit("NULLIF(", 1)[-1]
    assert "outbound_touch_dispatched" in reply_rate_denominator
    assert "payload->>'channel' = 'email'" in reply_rate_denominator
