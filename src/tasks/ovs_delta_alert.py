"""
Month-over-month OVS delta alert (S-7).

Compares owner_visibility_scores for the current month (N) vs the prior
month (N-1) for every company that has rows in both months. Posts a
summary to #blackink-economics whenever any company's score_total moves
more than DELTA_THRESHOLD points in either direction.

Runs as blackink_system (BYPASSRLS) — scores span all tenants.

Schedule: run the morning after owner_visibility_sweep.py completes for
the current month (the sweep is idempotent, so re-runs are safe). See
scripts/crontab.txt for the deployed schedule.

Exits 0 when nothing notable happened or the post succeeded. Exits 1
when the Slack post itself failed, so a missing
BLACKINK_ECONOMICS_SLACK_CHANNEL env var surfaces in monitoring rather
than silently disappearing.

    PYTHONPATH=. python -m src.tasks.ovs_delta_alert
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.slack.post import post_notice

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)

DELTA_THRESHOLD = 10
_MAX_ROWS_PER_BUCKET = 10


def _month_keys() -> tuple[str, str]:
    """(current_month, previous_month) as YYYY-MM strings."""
    today = date.today()
    current = today.strftime("%Y-%m")
    if today.month == 1:
        prev = f"{today.year - 1}-12"
    else:
        prev = f"{today.year}-{today.month - 1:02d}"
    return current, prev


def compute_deltas(session, current_month: str, prev_month: str) -> list[dict]:
    """Companies whose score moved strictly more than DELTA_THRESHOLD points.

    Only companies with a scored row in BOTH months are considered — a
    company that first appeared this month has no prior baseline and is
    excluded, avoiding a spurious 0→score alert."""
    rows = session.execute(
        text("""
            SELECT
                co.company_id,
                co.company_name,
                co.county_slug,
                co.owning_client_id,
                cur.score_total                          AS cur_score,
                prev.score_total                         AS prev_score,
                cur.score_total - prev.score_total       AS delta
            FROM owner_visibility_scores cur
            JOIN owner_visibility_scores prev
              ON  prev.company_id = cur.company_id
              AND prev.month_key  = :prev_month
            JOIN companies co ON co.company_id = cur.company_id
            WHERE cur.month_key = :cur_month
              AND ABS(cur.score_total - prev.score_total) > :threshold
            ORDER BY ABS(cur.score_total - prev.score_total) DESC,
                     co.company_name
        """),
        {
            "cur_month": current_month,
            "prev_month": prev_month,
            "threshold": DELTA_THRESHOLD,
        },
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _build_blocks(
    deltas: list[dict],
    current_month: str,
    prev_month: str,
) -> list[dict]:
    gainers = [d for d in deltas if d["delta"] > 0]
    losers  = [d for d in deltas if d["delta"] < 0]

    lines: list[str] = [
        f"*OVS Score Movement — {prev_month} → {current_month}*",
        f"{len(deltas)} compan{'y' if len(deltas) == 1 else 'ies'} "
        f"moved >{DELTA_THRESHOLD} pts",
    ]

    if gainers:
        lines.append(f"\n:chart_with_upwards_trend: *Gainers ({len(gainers)})*")
        for d in gainers[:_MAX_ROWS_PER_BUCKET]:
            lines.append(
                f"  • {d['company_name']} ({d['county_slug']})  "
                f"{d['prev_score']} → {d['cur_score']}  (*+{d['delta']}*)"
            )
        if len(gainers) > _MAX_ROWS_PER_BUCKET:
            lines.append(f"  … and {len(gainers) - _MAX_ROWS_PER_BUCKET} more")

    if losers:
        lines.append(f"\n:chart_with_downwards_trend: *Losers ({len(losers)})*")
        for d in losers[:_MAX_ROWS_PER_BUCKET]:
            lines.append(
                f"  • {d['company_name']} ({d['county_slug']})  "
                f"{d['prev_score']} → {d['cur_score']}  (*{d['delta']}*)"
            )
        if len(losers) > _MAX_ROWS_PER_BUCKET:
            lines.append(f"  … and {len(losers) - _MAX_ROWS_PER_BUCKET} more")

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        }
    ]


async def run_delta_alert(
    current_month: str | None = None,
    prev_month: str | None = None,
) -> bool:
    """Core logic. Month args are injectable for testing (fast-forward
    without touching real clock); defaults to the live calendar months."""
    if current_month is None or prev_month is None:
        current_month, prev_month = _month_keys()

    with get_system_db_context() as session:
        deltas = compute_deltas(session, current_month, prev_month)

    if not deltas:
        logger.info(
            "ovs_delta_alert: no companies moved >%d pts (%s vs %s) — nothing to post",
            DELTA_THRESHOLD, prev_month, current_month,
        )
        return True

    gainers = sum(1 for d in deltas if d["delta"] > 0)
    losers  = len(deltas) - gainers
    logger.info(
        "ovs_delta_alert: %d companies moved >%d pts (%d up / %d down) — posting to #blackink-economics",
        len(deltas), DELTA_THRESHOLD, gainers, losers,
    )

    summary = (
        f"OVS delta alert: {len(deltas)} companies moved >{DELTA_THRESHOLD} pts "
        f"({prev_month} → {current_month})"
    )
    ts = await post_notice(
        channel_key="economics",
        text=summary,
        blocks=_build_blocks(deltas, current_month, prev_month),
    )
    if ts is None:
        logger.error("ovs_delta_alert: Slack post to #blackink-economics failed — check BLACKINK_ECONOMICS_SLACK_CHANNEL")
        return False
    return True


def main() -> int:
    ok = asyncio.run(run_delta_alert())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
