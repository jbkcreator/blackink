"""Vera check — PM feed freshness.

Measures how many clients have had their PM-book (client_pm_books) synced
in the past FRESHNESS_WINDOW_DAYS days. A count of zero is a real, valid
reading (no syncs happened), not an error — the caller must interpret the
number in business context.

ABSTAIN is returned only when the DB query itself fails, meaning we could
not determine how many syncs happened. Callers must not treat ABSTAIN as
"zero syncs" — those are different facts.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.agents.vera.health_result import ABSTAIN, VALUE, HealthResult

logger = logging.getLogger(__name__)

FRESHNESS_WINDOW_DAYS: int = 7


def check_pm_feed(session: Session) -> HealthResult:
    """Return a VALUE with PM-sync counts, or ABSTAIN if the DB is unreachable.

    Never returns UNKNOWN — the PM-books table is always part of the schema,
    not an optional integration. If it's queryable the answer is real; if the
    query fails we ABSTAIN.
    """
    try:
        row = session.execute(
            text("""
                SELECT
                    COUNT(DISTINCT client_id) FILTER (
                        WHERE synced_at >= NOW() - INTERVAL '7 days'
                    ) AS synced_recently,
                    COUNT(DISTINCT client_id) AS total_clients_with_books
                FROM client_pm_books
            """)
        ).fetchone()
        synced = int(row[0] or 0)
        total = int(row[1] or 0)
        return HealthResult(
            check_name="pm_feed",
            state=VALUE,
            value={"synced_last_7d": synced, "total_with_books": total},
            detail=(
                f"synced_last_7d={synced} total_with_books={total} "
                f"(window={FRESHNESS_WINDOW_DAYS}d)"
            ),
        )
    except Exception as exc:
        logger.error("vera.pm_feed: DB query failed — abstaining: %s", exc)
        return HealthResult(
            check_name="pm_feed",
            state=ABSTAIN,
            value=None,
            detail=f"DB query failed — cannot verify PM feed freshness: {exc}",
        )
