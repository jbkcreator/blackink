from decimal import Decimal
from unittest.mock import AsyncMock, patch

from src.tasks.daily_digest import build_digest_text, main


def _rows(**kwargs):
    row = {
        "audits_completed": 0, "avg_response_latency_sec": None, "cold_emails_dispatched": 0,
        "open_rate_pct": None, "click_rate_pct": None, "video_completion_rate_pct": None,
        "appointments_booked": 0,
    }
    row.update(kwargs)
    return row


def test_build_digest_text_includes_all_seven_metrics():
    with patch("src.tasks.daily_digest._query_metrics", return_value=_rows(
        audits_completed=12, avg_response_latency_sec=4230, cold_emails_dispatched=340,
        open_rate_pct=41.2, click_rate_pct=9.8, video_completion_rate_pct=22.5, appointments_booked=6,
    )):
        text_out = build_digest_text()
    for expected in ("12", "4230", "340", "41.2", "9.8", "22.5", "6"):
        assert expected in text_out


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
