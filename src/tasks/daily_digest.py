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

# ── CROSS-TASK EVENT CONTRACT — read this before changing an event name ──
# v2 spec correction (Tasks/Updated_client spec/Week1_Tasks_Dev_Split_v2.md
# §Demo Sandbox & Metrics Engine, Subtask 4.2.3): the metric set changed. The
# client spec now explicitly says: "metrics are: Scores generated, county
# rank reports, emails dispatched, open rate, CTR, reply rate, appointments
# booked — not ghost-shopper latency or video completion rate." Ghost-Shopper
# and Sendspark are permanently deferred (v2 blueprint line 1264) — their
# events (audit_reply_received, audit_pdf_generated, sendspark_engagement)
# must never appear in this query again.
#
# Every event_type below is WRITTEN by a different Week 1 task's code, not
# by this one:
#   owner_score_generated     — Owner Visibility Score Engine (replaces the
#       Ghost-Shopper Audit Factory); fields: score_total, county,
#       data_coverage_pct, county_rank (see src/services/events.py's
#       REQUIRED_PAYLOAD_FIELDS)
#   outbound_touch_dispatched — Outbound Sequencer & Booking Engine, Subtask 3.1.1
#   meeting_booked            — Outbound Sequencer & Booking Engine, Subtask 3.2.1
#
# Three event names are DECIDED HERE and are the contract — the producing
# side must emit exactly these, or the metric silently stays at 0/'n/a':
#   email_opened / email_clicked — unchanged from the original decision.
#   email_replied — NEW for reply_rate_pct. No document defines this name;
#       it follows the same naming precedent as email_opened/email_clicked
#       and the blueprint's "Inbound Reply Bridge" concept. Communicate
#       this name to whoever builds inbound reply handling.
#
# county_rank_reports_delivered needs NO new event: county_rank is already
# one of owner_score_generated's required fields (the "County Rank
# Calculator" computes it as part of scoring, not as a separate delivery
# step — v2 blueprint lines 209, 367, 398). Defined here as the number of
# DISTINCT counties scored in the window. If "delivered" is later confirmed
# to mean something else (e.g. a held-back report), this becomes a real
# event and this one query line changes — nothing else in this module does.
_METRICS_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE event_type = 'owner_score_generated') AS scores_generated,
        COUNT(DISTINCT payload->>'county') FILTER (WHERE event_type = 'owner_score_generated') AS county_rank_reports_delivered,
        COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched' AND payload->>'channel' = 'email') AS cold_emails_dispatched,
        (COUNT(*) FILTER (WHERE event_type = 'email_opened')::numeric
            / NULLIF(COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched' AND payload->>'channel' = 'email'), 0) * 100) AS open_rate_pct,
        (COUNT(*) FILTER (WHERE event_type = 'email_clicked')::numeric
            / NULLIF(COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched' AND payload->>'channel' = 'email'), 0) * 100) AS click_rate_pct,
        (COUNT(*) FILTER (WHERE event_type = 'email_replied')::numeric
            / NULLIF(COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched' AND payload->>'channel' = 'email'), 0) * 100) AS reply_rate_pct,
        COUNT(*) FILTER (WHERE event_type = 'meeting_booked') AS appointments_booked
    FROM events
    WHERE created_at >= NOW() - :window
"""
# No numeric-cast regex guards are needed here (unlike the old
# avg_response_latency_sec/video_completion_rate_pct columns) — every
# expression above is either a plain COUNT or a division of two COUNTs,
# neither of which can fail on a malformed payload value the way a
# `(payload->>'x')::numeric` cast on an arbitrary string could.


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
        if value is None:
            return "n/a"
        return f"{round(float(value), 1)}{suffix}"

    return (
        ":bar_chart: *Daily Pipeline Digest — last 24h*\n"
        f"- Owner Visibility Scores generated: {fmt(metrics.get('scores_generated'))}\n"
        f"- County rank reports delivered: {fmt(metrics.get('county_rank_reports_delivered'))}\n"
        f"- Cold emails dispatched: {fmt(metrics.get('cold_emails_dispatched'))}\n"
        f"- Open rate: {fmt(metrics.get('open_rate_pct'), '%')}\n"
        f"- Click-through rate: {fmt(metrics.get('click_rate_pct'), '%')}\n"
        f"- Reply rate: {fmt(metrics.get('reply_rate_pct'), '%')}\n"
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
