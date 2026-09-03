"""Add ghost-shopper audit columns to contacts and create pm_profiles table.

# ============================================================
# DEFERRED — DO NOT RUN ON THE LIVE DATABASE
# ============================================================
# Ghost Shopper was deferred on 2026-09-03 (Source of Truth).
# Owner Visibility Score (OVS) replaces it as the proof-of-need artefact.
#
# The live DB cleanup is handled by:
#   migrations/apply_ghost_shopper_cleanup.py  (on feature/owner-visibility-score-engine)
# which drops the ghost-shopper-only columns and renames audit_pdf_url -> ovs_pdf_url.
#
# This migration file is preserved here for reference in case Ghost Shopper
# is reactivated in a future sprint. It must NOT be added to the CLAUDE.md
# migration runbook or applied to any environment while Ghost Shopper remains deferred.
# ============================================================

Dev 2 owns these columns — they are not part of Dev 1's contacts migration.
This migration runs after apply_contacts.py and apply_companies.py.

Idempotent: ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_ghost_shopper_columns.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    # ── Audit columns on contacts ─────────────────────────────────────────────
    # Written by: IMAP Listener (audit_speed_score_sec),
    #             PDF Generator (audit_loss_dollars_est, audit_pdf_url),
    #             Sendspark subagent (sendspark_video_id, sendspark_landing_url)
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS ghost_submitted_at       BIGINT",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS audit_speed_score_sec   INTEGER",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS audit_loss_dollars_est  INTEGER",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS audit_pdf_url           VARCHAR(2048)",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS sendspark_video_id      VARCHAR(255)",
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS sendspark_landing_url   VARCHAR(2048)",

    # ── pm_profiles — metro benchmark data for PDF Page 2 comparison table ───
    # Populated by a seed script / nightly sync, not by Ghost Shopper itself.
    # average_speed_to_lead_seconds: metro-wide average used in the comparison
    # top10_speed_to_lead_seconds:   top-10% peer speed in the same metro
    """
    CREATE TABLE IF NOT EXISTS pm_profiles (
        profile_id                      BIGSERIAL       PRIMARY KEY,
        company_id                      VARCHAR(64)     NOT NULL UNIQUE
                                            REFERENCES companies(company_id)
                                            ON DELETE CASCADE,
        market_metro                    VARCHAR(100),
        average_speed_to_lead_seconds   INTEGER,
        top10_speed_to_lead_seconds     INTEGER,
        specialty_tags                  VARCHAR(500),
        languages_supported             VARCHAR(500),
        asset_class_strengths           VARCHAR(500),
        historical_close_rate           NUMERIC(5,2),
        show_rate_percentage            NUMERIC(5,2),
        created_at                      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
        updated_at                      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
    )
    """,

    # ADD COLUMN IF NOT EXISTS guards for each pm_profiles column — handles the
    # case where the table already exists from a prior run with a different schema.
    "ALTER TABLE pm_profiles ADD COLUMN IF NOT EXISTS market_metro                  VARCHAR(100)",
    "ALTER TABLE pm_profiles ADD COLUMN IF NOT EXISTS average_speed_to_lead_seconds INTEGER",
    "ALTER TABLE pm_profiles ADD COLUMN IF NOT EXISTS top10_speed_to_lead_seconds   INTEGER",
    "ALTER TABLE pm_profiles ADD COLUMN IF NOT EXISTS specialty_tags                VARCHAR(500)",
    "ALTER TABLE pm_profiles ADD COLUMN IF NOT EXISTS languages_supported           VARCHAR(500)",
    "ALTER TABLE pm_profiles ADD COLUMN IF NOT EXISTS asset_class_strengths         VARCHAR(500)",
    "ALTER TABLE pm_profiles ADD COLUMN IF NOT EXISTS historical_close_rate         NUMERIC(5,2)",
    "ALTER TABLE pm_profiles ADD COLUMN IF NOT EXISTS show_rate_percentage          NUMERIC(5,2)",
    "ALTER TABLE pm_profiles ADD COLUMN IF NOT EXISTS created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    "ALTER TABLE pm_profiles ADD COLUMN IF NOT EXISTS updated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()",

    "CREATE INDEX IF NOT EXISTS ix_pm_profiles_metro ON pm_profiles (market_metro)",

    "GRANT SELECT, INSERT, UPDATE ON pm_profiles TO blackink_app",
    "GRANT USAGE ON SEQUENCE pm_profiles_profile_id_seq TO blackink_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON pm_profiles TO blackink_system",
    "GRANT USAGE ON SEQUENCE pm_profiles_profile_id_seq TO blackink_system",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()

        audit_cols = db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'contacts' "
                "  AND column_name LIKE 'audit_%' OR column_name LIKE 'sendspark_%' "
                "ORDER BY ordinal_position"
            )
        ).fetchall()

        pm_exists = db.execute(
            text(
                "SELECT to_regclass('public.pm_profiles') IS NOT NULL AS exists"
            )
        ).scalar()

    print("apply_ghost_shopper_columns: contacts audit cols —", [c.column_name for c in audit_cols])
    print("apply_ghost_shopper_columns: pm_profiles exists —", pm_exists)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
