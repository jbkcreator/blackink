"""Vera check — ingestion pipeline health.

Counts rows in raw_prospect_companies by validation_status to surface
silent promotion failures. A pending count that stays large across runs
signals that the promotion sweep has stopped clearing rows, even if it
hasn't crashed with an explicit error.

STATUS MEANINGS (from models.py _VALIDATION_STATUSES):
  pending     — received from Akrash, not yet evaluated
  cleared     — passed quarantine gate, waiting for promotion
  quarantined — held for manual review
  rejected    — permanently rejected (reject_reason_code set)

ABSTAIN is returned only when the DB query fails. A count of zero for
any status bucket is a real, valid reading — not evidence of an error.
Never return VALUE with counts that came from swallowing an exception.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.agents.vera.health_result import ABSTAIN, VALUE, HealthResult

logger = logging.getLogger(__name__)

_PIPELINE_WINDOW_HOURS: int = 24


def check_pipeline_health(session: Session) -> HealthResult:
    """Return pipeline staging counts (VALUE) or ABSTAIN if the DB is unreachable.

    value dict keys:
      pending         — rows not yet evaluated by the quarantine gate
      cleared         — rows cleared, awaiting promotion sweep
      quarantined     — rows held for review
      rejected        — rows permanently rejected
      promoted_last_24h — companies promoted to canonical table in last 24h
    """
    try:
        status_row = session.execute(
            text("""
                SELECT
                    COUNT(*) FILTER (WHERE validation_status = 'pending')     AS pending,
                    COUNT(*) FILTER (WHERE validation_status = 'cleared')     AS cleared,
                    COUNT(*) FILTER (WHERE validation_status = 'quarantined') AS quarantined,
                    COUNT(*) FILTER (WHERE validation_status = 'rejected')    AS rejected
                FROM raw_prospect_companies
            """)
        ).fetchone()

        promoted = session.execute(
            text("""
                SELECT COUNT(*)
                FROM companies
                WHERE created_at >= NOW() - INTERVAL '24 hours'
            """)
        ).scalar()

        pending = int(status_row[0] or 0)
        cleared = int(status_row[1] or 0)
        quarantined = int(status_row[2] or 0)
        rejected = int(status_row[3] or 0)
        promoted_last_24h = int(promoted or 0)

        return HealthResult(
            check_name="pipeline",
            state=VALUE,
            value={
                "pending": pending,
                "cleared": cleared,
                "quarantined": quarantined,
                "rejected": rejected,
                "promoted_last_24h": promoted_last_24h,
            },
            detail=(
                f"pending={pending} cleared={cleared} quarantined={quarantined} "
                f"rejected={rejected} promoted_last_24h={promoted_last_24h}"
            ),
        )
    except Exception as exc:
        logger.error("vera.pipeline: DB query failed — abstaining: %s", exc)
        return HealthResult(
            check_name="pipeline",
            state=ABSTAIN,
            value=None,
            detail=f"DB query failed — cannot verify pipeline health: {exc}",
        )
