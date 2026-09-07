"""
Provision the owner_visibility_scores table.

One scored row per (company_id, month_key). month_key is YYYY-MM, e.g. '2026-09'.
The UNIQUE constraint prevents duplicate scoring runs from creating phantom history;
a re-run of the sweep for the same month upserts rather than inserts.

The composite index on (county_slug, month_key, score_total DESC) supports the
county-rank query without a cross-table join: the sweep writes
county_slug alongside score_total so ranking is a single index scan.

score_google stays 0 until a live GOOGLE_PLACES_API_KEY is configured —
the stub provider returns MISSING_DATA for all 5 Google signals.
Maximum achievable score without Google API: 42 (38 website + 4 DBPR).

owner_visibility_scores is tenant-scoped through companies.owning_client_id —
registered in config/tenant_policies.py as a join-scoped table. Run
migrations/apply_rls_policies.py after this script.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_owner_visibility_scores.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS owner_visibility_scores (
        score_id           BIGSERIAL    PRIMARY KEY,
        company_id         VARCHAR(64)  NOT NULL REFERENCES companies(company_id),
        month_key          VARCHAR(7)   NOT NULL,
        county_slug        VARCHAR(60)  NOT NULL REFERENCES counties(county_slug),
        score_total        SMALLINT     NOT NULL,
        score_website      SMALLINT     NOT NULL DEFAULT 0,
        score_dbpr         SMALLINT     NOT NULL DEFAULT 0,
        score_google       SMALLINT     NOT NULL DEFAULT 0,
        signal_detail      JSONB,
        data_gaps          TEXT[]       NOT NULL DEFAULT '{}',
        county_rank        SMALLINT,
        county_percentile  SMALLINT,
        peer_comparisons   JSONB,
        scored_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_ovs_month_key   CHECK (month_key  ~ '^[0-9]{4}-[0-9]{2}$'),
        CONSTRAINT ck_ovs_score_total CHECK (score_total BETWEEN 0 AND 100),
        CONSTRAINT uq_ovs_company_month UNIQUE (company_id, month_key)
    )
    """,
    # Idempotent column additions for databases provisioned before rank columns were added.
    "ALTER TABLE owner_visibility_scores ADD COLUMN IF NOT EXISTS county_rank       SMALLINT",
    "ALTER TABLE owner_visibility_scores ADD COLUMN IF NOT EXISTS county_percentile SMALLINT",
    "ALTER TABLE owner_visibility_scores ADD COLUMN IF NOT EXISTS peer_comparisons  JSONB",
    # County-rank queries: list top N firms in a county for a month.
    "CREATE INDEX IF NOT EXISTS ix_ovs_county_month_rank ON owner_visibility_scores (county_slug, month_key, score_total DESC)",
    "CREATE INDEX IF NOT EXISTS ix_ovs_company_month ON owner_visibility_scores (company_id, month_key)",
    # No DELETE for either role — least privilege, on purpose. Neither
    # owner_visibility_sweep.py (INSERT/UPDATE only, an upsert) nor
    # Subtask 3.2.2's show_rate_reminders.py (SELECT only) ever deletes a
    # score row; test-fixture teardown for this table goes through
    # get_owner_db_context() instead (tests/test_show_rate_reminders.py),
    # not a widened production grant. Explicit REVOKE so this migration is
    # self-correcting even against a database an earlier draft's broader
    # GRANT already ran against.
    "REVOKE DELETE ON owner_visibility_scores FROM blackink_app",
    "REVOKE DELETE ON owner_visibility_scores FROM blackink_system",
    "GRANT SELECT, INSERT, UPDATE ON owner_visibility_scores TO blackink_app",
    # owner_visibility_sweep.py runs as blackink_system (BYPASSRLS).
    "GRANT SELECT, INSERT, UPDATE ON owner_visibility_scores TO blackink_system",
    "GRANT USAGE, SELECT ON SEQUENCE owner_visibility_scores_score_id_seq TO blackink_app",
    "GRANT USAGE, SELECT ON SEQUENCE owner_visibility_scores_score_id_seq TO blackink_system",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        count = db.execute(text("SELECT COUNT(*) FROM owner_visibility_scores")).scalar()
    print(f"apply_owner_visibility_scores: done — {count} rows present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
