"""Re-add Ghost Shopper schema to the live DB after reactivation.

Ghost Shopper was deferred on 2026-09-03 and apply_ghost_shopper_cleanup.py
removed its columns. The deferral was reversed; this migration re-adds only
the two columns the active Ink/Ghost Shopper pipeline actually writes to the
contacts table.

What was removed by apply_ghost_shopper_cleanup.py:
  ghost_submitted_at      — re-added here (IMAP listener needs it)
  audit_speed_score_sec   — NOT re-added (removed from compliance gate; Ink
                            pipeline stores latency_sec in LangGraph state only)
  sendspark_video_id      — NOT re-added (Sendspark still deferred)
  sendspark_landing_url   — NOT re-added (Sendspark still deferred)

Columns that must NOT be touched:
  ovs_pdf_url             — was audit_pdf_url, renamed by the cleanup migration;
                            OVS sweep writes here; never reintroduce audit_pdf_url
  audit_loss_dollars_est  — kept by cleanup migration; OVS revenue model writes here

Two columns re-added:
  ghost_submitted_at   BIGINT — epoch-ms when the Ghost Shopper form was submitted;
                                the IMAP listener computes latency_sec from this.
  ghost_work_order_id  TEXT   — Ink LangGraph thread_id (= work_order_id) for the
                                active campaign; the IMAP listener uses this to
                                publish the correct resume signal without scanning
                                LangGraph internals.

Idempotent: ADD COLUMN IF NOT EXISTS throughout. Safe to re-run.

    PYTHONPATH=. python migrations/apply_ghost_shopper_reactivation.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    # Ghost Shopper writes this when a form is submitted successfully.
    # IMAP listener reads it to compute latency_sec = reply_time_ms - ghost_submitted_at.
    # Cleared by the IMAP listener after a reply arrives (or after 24h timeout).
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS ghost_submitted_at  BIGINT",

    # Ink LangGraph thread_id written alongside ghost_submitted_at.
    # Lets the IMAP listener resolve sender_domain → work_order_id in one query
    # without touching LangGraph's internal checkpoint tables.
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS ghost_work_order_id TEXT",
]

_VERIFY_PRESENT  = {"ghost_submitted_at", "ghost_work_order_id", "ovs_pdf_url", "audit_loss_dollars_est"}
_VERIFY_ABSENT   = {"audit_pdf_url", "audit_speed_score_sec", "sendspark_video_id", "sendspark_landing_url"}


def _verify(db) -> None:
    cols = {
        r.column_name
        for r in db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'contacts'"
        )).fetchall()
    }

    missing = _VERIFY_PRESENT - cols
    if missing:
        raise RuntimeError(f"Expected columns missing after migration: {missing}")

    still_present = _VERIFY_ABSENT & cols
    if still_present:
        raise RuntimeError(
            f"Columns that must be absent are present — cleanup migration may not have run: {still_present}"
        )


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        _verify(db)

    print("apply_ghost_shopper_reactivation: OK")
    print("  contacts: ghost_submitted_at  added (epoch-ms of form submission)")
    print("  contacts: ghost_work_order_id added (Ink LangGraph thread_id for IMAP resume)")
    print("  contacts: ovs_pdf_url intact (OVS sweep still owns this)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
