"""Remove ghost-shopper schema from the live DB.

Ghost Shopper was deferred (Source of Truth, Sep 3 2026). Owner Visibility
Score (OVS) replaces it as the proof-of-need artefact. This migration
removes or repurposes every DB object the ghost-shopper migration created:

contacts table
  DROP   ghost_submitted_at      -- epoch ms of form submission (ghost-shopper only)
  DROP   audit_speed_score_sec   -- measured response latency (ghost-shopper only)
  DROP   sendspark_video_id      -- Sendspark integration, no contract, deferred
  DROP   sendspark_landing_url   -- same
  RENAME audit_pdf_url -> ovs_pdf_url   -- OVS sweep writes the OVS PDF URL here
  KEEP   audit_loss_dollars_est  -- OVS revenue model writes the same dollar figure

pm_profiles table (kept -- used for setter context cards)
  DROP   average_speed_to_lead_seconds  -- ghost-shopper metro benchmark, never populated
  DROP   top10_speed_to_lead_seconds    -- same

Idempotent: DROP COLUMN IF EXISTS; RENAME guarded by a DO block existence check.

    PYTHONPATH=. python migrations/apply_ghost_shopper_cleanup.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    # ── contacts: drop ghost-shopper-only columns ─────────────────────────────
    "ALTER TABLE contacts DROP COLUMN IF EXISTS ghost_submitted_at",
    "ALTER TABLE contacts DROP COLUMN IF EXISTS audit_speed_score_sec",
    "ALTER TABLE contacts DROP COLUMN IF EXISTS sendspark_video_id",
    "ALTER TABLE contacts DROP COLUMN IF EXISTS sendspark_landing_url",

    # ── contacts: ensure ovs_pdf_url exists (OVS sweep writes here) ──────────
    # Three cases, all idempotent:
    #   1. audit_pdf_url exists (ghost-shopper was deployed): rename it.
    #   2. ovs_pdf_url already exists: no-op.
    #   3. Neither exists (fresh DB, ghost-shopper never ran): add the column.
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name  = 'contacts'
              AND column_name = 'audit_pdf_url'
        ) THEN
            ALTER TABLE contacts RENAME COLUMN audit_pdf_url TO ovs_pdf_url;
        ELSIF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name  = 'contacts'
              AND column_name = 'ovs_pdf_url'
        ) THEN
            ALTER TABLE contacts ADD COLUMN ovs_pdf_url TEXT;
        END IF;
    END $$
    """,

    # ── contacts: ensure audit_loss_dollars_est exists (OVS revenue model) ─────
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS audit_loss_dollars_est NUMERIC(10,2)",

    # ── pm_profiles: drop ghost-shopper speed-benchmark columns ───────────────
    "ALTER TABLE pm_profiles DROP COLUMN IF EXISTS average_speed_to_lead_seconds",
    "ALTER TABLE pm_profiles DROP COLUMN IF EXISTS top10_speed_to_lead_seconds",
]


def _verify(db) -> None:
    # contacts — confirm dropped columns are gone and rename landed
    cols = {
        r.column_name
        for r in db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'contacts'"
        )).fetchall()
    }
    dropped = {"ghost_submitted_at", "audit_speed_score_sec",
               "sendspark_video_id", "sendspark_landing_url", "audit_pdf_url"}
    still_present = dropped & cols
    if still_present:
        raise RuntimeError(f"Expected columns to be gone but found: {still_present}")

    assert "ovs_pdf_url" in cols,          "contacts.ovs_pdf_url missing after rename"
    assert "audit_loss_dollars_est" in cols, "contacts.audit_loss_dollars_est unexpectedly missing"

    # pm_profiles — confirm speed columns dropped
    pm_cols = {
        r.column_name
        for r in db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pm_profiles'"
        )).fetchall()
    }
    pm_dropped = {"average_speed_to_lead_seconds", "top10_speed_to_lead_seconds"}
    still_present = pm_dropped & pm_cols
    if still_present:
        raise RuntimeError(f"pm_profiles columns still present: {still_present}")


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        _verify(db)

    print("apply_ghost_shopper_cleanup: OK")
    print("  contacts : ghost_submitted_at, audit_speed_score_sec, sendspark_* dropped")
    print("  contacts : audit_pdf_url -> ovs_pdf_url renamed")
    print("  contacts : audit_loss_dollars_est kept (OVS revenue model writes here)")
    print("  pm_profiles : average_speed_to_lead_seconds, top10_speed_to_lead_seconds dropped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
