"""Daily Slack metrics digest -> #blackink-command (blueprint §3.1.7 /
split-doc Subtask 4.2.3). Everything this posts already exists as `events`
rows written by the shared EventLogger (Task 1) — this is a pure read-side
rollup, no new write path.

Scheduling: no in-process scheduler (ponytail: matches every other
src/tasks/*.py job in this codebase — none of them self-schedule either).
Run via external cron: `0 8 * * * PYTHONPATH=. python -m src.tasks.daily_digest`
(8:00 AM America/New_York per the blueprint default).

    PYTHONPATH=. python -m src.tasks.daily_digest
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.events import flush_pending
from src.services.slack.post import post_notice

logger = logging.getLogger(__name__)

_WINDOW = timedelta(hours=24)

# ── CROSS-TEAM EVENT CONTRACT — read this before changing an event name ──
# Every event_type below is WRITTEN by another developer's Week 1 code, not
# by Dev 4. Where the split doc names the event explicitly, this query uses
# that name verbatim:
#   audit_reply_received     — Dev 2, Subtask 2.1.2 (carries audit_speed_score_sec)
#   audit_pdf_generated      — Dev 2, Subtask 2.1.3 (one per completed audit)
#   sendspark_engagement     — Dev 2, Subtask 2.2.3 (payload.watch_percent)
#   outbound_touch_dispatched— Dev 3, Subtask 3.1.1
#   meeting_booked           — Dev 3, Subtask 3.2.1
#
# email_opened / email_clicked are the ONE gap: Subtask 3.1.1 requires
# "open and click tracking pixels active" but never names the resulting
# event types. These two names are DECIDED HERE and are the contract —
# Dev 3's tracking-pixel handler must emit exactly these. Tell Dev 3; do
# not add a translation layer if they picked something else, rename theirs.
#
# THREE OF THESE SEVEN METRICS WILL READ 'n/a' FOR NOW, and that is
# expected, not a bug to chase (client clarifications, Week 1 Open Items
# #6 and #2):
#   audits_completed / avg_response_latency_sec — the Ghost-Shopper is on
#       hold ("Hold. Do not build it." — the client objects to submitting
#       pretext inquiries to real businesses at volume). No audit_* events
#       will be written until Rank v1's public-data approach replaces it.
#   video_completion_rate_pct — Sendspark is deferred ("Do not build the
#       video flow in September — build the trigger hook and leave the
#       provider behind a config row"), so no sendspark_engagement events.
# The query deliberately still asks for all seven: when those features do
# land and start writing events, the digest starts reporting them with no
# code change. NULLIF keeps the divide-by-zero cases as NULL -> 'n/a'.
_METRICS_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE event_type = 'audit_pdf_generated') AS audits_completed,
        AVG((payload->>'audit_speed_score_sec')::numeric)
            FILTER (WHERE event_type = 'audit_reply_received'
                    AND payload->>'audit_speed_score_sec' ~ '^[0-9]+(\\.[0-9]+)?$') AS avg_response_latency_sec,
        COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched' AND payload->>'channel' = 'email') AS cold_emails_dispatched,
        (COUNT(*) FILTER (WHERE event_type = 'email_opened')::numeric
            / NULLIF(COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched'), 0) * 100) AS open_rate_pct,
        (COUNT(*) FILTER (WHERE event_type = 'email_clicked')::numeric
            / NULLIF(COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched'), 0) * 100) AS click_rate_pct,
        (COUNT(*) FILTER (WHERE event_type = 'sendspark_engagement'
                          AND payload->>'watch_percent' ~ '^[0-9]+(\\.[0-9]+)?$'
                          AND (payload->>'watch_percent')::numeric >= 100)::numeric
            / NULLIF(COUNT(*) FILTER (WHERE event_type = 'sendspark_engagement'), 0) * 100) AS video_completion_rate_pct,
        COUNT(*) FILTER (WHERE event_type = 'meeting_booked') AS appointments_booked
    FROM events
    WHERE created_at >= NOW() - :window
"""
# The two `~ '^[0-9]+...'` regex guards are not defensive clutter: `->>`
# returns text, and a single event whose payload carries a non-numeric
# value in one of these keys would abort the WHOLE digest query with a
# cast error, turning one bad row written by another team into a silent
# daily "Data Unavailable". Filtering to castable values degrades one
# metric instead of all seven.


def _query_metrics() -> dict:
    """Platform-wide aggregate across every tenant — #blackink-command is
    the executive overview channel (blueprint §3.0.3), not a per-client
    feed, so this deliberately does not take a client_id. If per-client
    digests are wanted later, that is a different function posting to a
    different channel, not a parameter on this one."""
    with get_system_db_context() as session:
        row = session.execute(text(_METRICS_SQL), {"window": _WINDOW}).mappings().first()
    return dict(row) if row else {}


def build_digest_text() -> str:
    try:
        metrics = _query_metrics()
    except Exception:
        logger.error("[daily_digest] metrics query failed — read-replica unavailable", exc_info=True)
        return ":warning: *Daily Digest* — Data Unavailable (read-replica query failed; see #blackink-qa)"

    def fmt(value, suffix=""):
        return "n/a" if value is None else f"{value}{suffix}"

    return (
        ":bar_chart: *Daily Pipeline Digest — last 24h*\n"
        f"- Audits completed: {fmt(metrics.get('audits_completed'))}\n"
        f"- Avg metro response latency: {fmt(metrics.get('avg_response_latency_sec'), ' sec')}\n"
        f"- Cold emails dispatched: {fmt(metrics.get('cold_emails_dispatched'))}\n"
        f"- Open rate: {fmt(metrics.get('open_rate_pct'), '%')}\n"
        f"- Click-through rate: {fmt(metrics.get('click_rate_pct'), '%')}\n"
        f"- Video completion rate: {fmt(metrics.get('video_completion_rate_pct'), '%')}\n"
        f"- Appointments booked: {fmt(metrics.get('appointments_booked'))}"
    )


def main() -> int:
    # Opportunistic drain of any events buffered while the DB was down —
    # this daily job is the one guaranteed-scheduled process in the system,
    # which is what makes it the natural place to do it (see
    # src/services/events.py::flush_pending's docstring).
    flushed = flush_pending()
    if flushed:
        logger.info("[daily_digest] flushed %d buffered event(s)", flushed)

    text_out = build_digest_text()
    # post_notice is `async def` (src/services/slack/post.py) — calling it
    # without awaiting would create a never-awaited coroutine and silently
    # post nothing. asyncio.run is correct here precisely because this is a
    # standalone CLI process with no running event loop of its own.
    asyncio.run(post_notice(channel_key="command", text=text_out))
    print("daily_digest: posted")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
