"""
Computes audit_loss_dollars_est and custom_hook_text for winback_rows via a
three-tier waterfall (S-14 — Master Data Contract §D):

  1. Ghost Shopper — most recent non-timed-out ghost_shopper_replies.loss_est
     for a company owned by this client in the same county.
  2. OVS — (100 - score_total) * 50 from the latest owner_visibility_scores
     for a company owned by this client in the same county. Each missing OVS
     point = $50/yr of owner opportunity cost.
  3. Fixed — $1,200 (one door at $100/month fee rate, matching imap_listener's
     _AVG_FEE_ANNUAL baseline).

custom_hook_text is pre-rendered from audit_loss_dollars_est + county name so
callers (Slack cards, sequence_content) read one column rather than
re-computing. Called from enrichment_verification.run_sweep() after enrichment
completes for a claimed batch.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_FIXED_FALLBACK_DOLLARS = 1_200


def _ghost_loss_est(session: Session, client_id: str, county_slug: str) -> Optional[int]:
    """Most recent non-timed-out ghost shopper loss_est for this client/county."""
    row = session.execute(
        text("""
            SELECT gsr.loss_est
            FROM ghost_shopper_replies gsr
            JOIN companies co ON co.company_id = gsr.company_id
            WHERE co.owning_client_id = :client_id
              AND co.county_slug      = :county_slug
              AND gsr.timed_out       = FALSE
              AND gsr.loss_est        IS NOT NULL
            ORDER BY gsr.received_at DESC
            LIMIT 1
        """),
        {"client_id": client_id, "county_slug": county_slug},
    ).first()
    return int(row.loss_est) if row else None


def _ovs_loss_est(session: Session, client_id: str, county_slug: str) -> Optional[int]:
    """Derive from most recent OVS score: (100 - score_total) * 50."""
    row = session.execute(
        text("""
            SELECT ovs.score_total
            FROM owner_visibility_scores ovs
            JOIN companies co ON co.company_id = ovs.company_id
            WHERE co.owning_client_id = :client_id
              AND ovs.county_slug     = :county_slug
            ORDER BY ovs.month_key DESC
            LIMIT 1
        """),
        {"client_id": client_id, "county_slug": county_slug},
    ).first()
    if row is None:
        return None
    return max(0, int((100 - row.score_total) * 50))


def _county_name(session: Session, county_slug: str) -> str:
    row = session.execute(
        text("SELECT county_name FROM counties WHERE county_slug = :slug"),
        {"slug": county_slug},
    ).first()
    return row.county_name if row else county_slug.replace("_fl", "").replace("_", " ").title()


def _render_hook(county_name: str, loss_est: int) -> str:
    return (
        f"Owners like you in {county_name} are leaving an estimated "
        f"${loss_est:,}/yr on the table self-managing — "
        "mostly from fee lines and pricing that a managed portfolio "
        "captures automatically."
    )


def compute_for_row(session: Session, client_id: str, county_slug: str) -> tuple[int, str]:
    """Waterfall → (audit_loss_dollars_est, custom_hook_text)."""
    loss_est = (
        _ghost_loss_est(session, client_id, county_slug)
        or _ovs_loss_est(session, client_id, county_slug)
        or _FIXED_FALLBACK_DOLLARS
    )
    county_name = _county_name(session, county_slug)
    return loss_est, _render_hook(county_name, loss_est)


def stamp_loss_estimates(session: Session, client_id: str, winback_row_ids: list[int]) -> int:
    """Compute and stamp audit_loss_dollars_est + custom_hook_text for rows
    that don't yet have the estimate. Idempotent — skips rows already stamped
    or missing county_slug. Returns count of rows stamped."""
    if not winback_row_ids:
        return 0

    rows = session.execute(
        text(
            "SELECT winback_row_id, county_slug FROM winback_rows "
            "WHERE winback_row_id = ANY(:ids) "
            "  AND audit_loss_dollars_est IS NULL "
            "  AND county_slug IS NOT NULL"
        ),
        {"ids": winback_row_ids},
    ).fetchall()

    stamped = 0
    for row in rows:
        try:
            loss_est, hook_text = compute_for_row(session, client_id, row.county_slug)
            session.execute(
                text(
                    "UPDATE winback_rows "
                    "SET audit_loss_dollars_est = :loss_est, "
                    "    custom_hook_text        = :hook_text, "
                    "    updated_at              = NOW() "
                    "WHERE winback_row_id = :row_id"
                ),
                {"loss_est": loss_est, "hook_text": hook_text, "row_id": row.winback_row_id},
            )
            stamped += 1
        except Exception:
            logger.error(
                "winback_loss_est: failed to stamp winback_row_id=%s — skipped",
                row.winback_row_id, exc_info=True,
            )
    return stamped
