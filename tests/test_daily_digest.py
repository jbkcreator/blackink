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


def test_metrics_sql_excludes_the_demo_sandbox_client():
    """Confirmed live: a digest run right after re-seeding the permanent
    demo sandbox reported synthetic sandbox activity (cold emails,
    appointments) as if it were real cross-client pipeline data, because
    this platform-wide query has no client_id filter at all. The sandbox's
    client_id must be excluded — now via the central DEMO_CLIENT_IDS registry
    (PR #53 review finding 1), which also covers DEMO_CLIENT_WINS."""
    from src.core.demo_clients import DEMO_CLIENT_IDS
    assert "client_id <> ALL(:demo_client_ids)" in _METRICS_SQL
    assert "DEMO_FRIDAY_SANDBOX" in DEMO_CLIENT_IDS


def test_metrics_sql_uses_owner_visibility_score_calculated_not_ghost_shopper():
    """Group D / D-4: the digest must read from owner_visibility_score_calculated
    — the event owner_visibility_sweep.py actually writes — never the
    never-written owner_score_generated name it used to filter on, and never
    the permanently-deferred ghost-shopper events."""
    assert "owner_visibility_score_calculated" in _METRICS_SQL
    assert "owner_score_generated" not in _METRICS_SQL
    assert "ghost_shopper" not in _METRICS_SQL
    assert "audit_pdf_generated" not in _METRICS_SQL
    assert "audit_reply_received" not in _METRICS_SQL
    assert "sendspark_engagement" not in _METRICS_SQL


def test_metrics_sql_county_rank_reports_uses_county_slug_not_county():
    """Group D / D-4: the payload key owner_visibility_sweep.py actually
    writes is county_slug, not county — the DISTINCT-county count must match
    the real payload shape or it silently counts zero distinct values."""
    assert "county_slug" in _METRICS_SQL
    assert "payload->>'county'" not in _METRICS_SQL.replace("payload->>'county_slug'", "")


def test_metrics_sql_open_click_reply_rate_wired_to_real_events_with_nullif_guard():
    """S-8 producers now exist (email_tracking_router.py writes email_opened/
    email_clicked; inbound_ingest.py writes email_replied, all deduped per
    dispatch_id), so the three engagement rates are computed as real
    divisions over the cold-email denominator rather than a literal NULL.
    NULLIF(..., 0) keeps the rate NULL (→ "n/a" via fmt()) when nothing was
    dispatched in the window, so an empty window never reads a misleading 0%;
    once sends exist, an untracked open reads a true 0.0%."""
    assert "NULL::numeric AS open_rate_pct" not in _METRICS_SQL
    assert "event_type = 'email_opened'" in _METRICS_SQL
    assert "event_type = 'email_clicked'" in _METRICS_SQL
    assert "event_type = 'email_replied'" in _METRICS_SQL
    # Divide-by-zero guard on the dispatched denominator, for all three rates.
    assert _METRICS_SQL.count("NULLIF((SELECT COUNT(*) FROM dispatch_cohort), 0)") == 3
    for col in ("open_rate_pct", "click_rate_pct", "reply_rate_pct"):
        assert f"AS {col}" in _METRICS_SQL


def test_metrics_sql_uses_dispatch_time_cohort_for_engagement_events():
    assert "dispatch_cohort" in _METRICS_SQL
    assert "JOIN dispatch_cohort" in _METRICS_SQL
    assert "d.dispatch_id = e.payload->>'dispatch_id'" in _METRICS_SQL
    assert "d.created_at" not in _METRICS_SQL
