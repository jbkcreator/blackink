"""
Provision the meeting_outcomes table and add prospect_objections to contacts.

meeting_outcomes captures post-demo Slack form intelligence (PM software,
door count, stated objections, next action). It is separate from
appointment_outcomes (billing / 4-rule verification).

The freshness guard in src/services/meeting_outcomes.py ensures mirrored
fields on contacts and companies always reflect the most recent meeting_occurred_at,
not whichever form was submitted last. Two indexes on (contact_id, meeting_occurred_at)
and (company_id, meeting_occurred_at) support the MAX queries that implement the guard.

meeting_outcomes carries client_id directly and is registered as a direct-mode
tenant-scoped table in config/tenant_policies.py. Run apply_rls_policies.py after
this script.

Idempotent: CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_meeting_outcomes.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    # Additive column on contacts — captures prospect objections gathered during meetings.
    "ALTER TABLE contacts ADD COLUMN IF NOT EXISTS prospect_objections TEXT",

    """
    CREATE TABLE IF NOT EXISTS meeting_outcomes (
        outcome_id          BIGSERIAL    PRIMARY KEY,
        client_id           VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
        contact_id          BIGINT       NOT NULL REFERENCES contacts(contact_id),
        company_id          VARCHAR(64)  NOT NULL REFERENCES companies(company_id),
        meeting_occurred_at TIMESTAMPTZ  NOT NULL,
        attendance_status   VARCHAR(20)  NOT NULL,
        pm_software_stated  VARCHAR(100),
        door_count_stated   INTEGER,
        objections_stated   TEXT,
        next_action         TEXT,
        submitted_by        VARCHAR(120),
        created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_meeting_outcomes_attendance CHECK (
            attendance_status IN ('ATTENDED','NO_SHOW','RESCHEDULED','CANCELLED')
        )
    )
    """,

    # Supports MAX(meeting_occurred_at) WHERE contact_id = ? (freshness guard)
    "CREATE INDEX IF NOT EXISTS ix_meeting_outcomes_contact_occurred"
    " ON meeting_outcomes (contact_id, meeting_occurred_at)",

    # Supports MAX(meeting_occurred_at) WHERE company_id = ? (company-level guard)
    "CREATE INDEX IF NOT EXISTS ix_meeting_outcomes_company_occurred"
    " ON meeting_outcomes (company_id, meeting_occurred_at)",

    "GRANT SELECT, INSERT ON meeting_outcomes TO blackink_app",
    "GRANT SELECT, INSERT ON meeting_outcomes TO blackink_system",
    "GRANT USAGE, SELECT ON SEQUENCE meeting_outcomes_outcome_id_seq TO blackink_app",
    "GRANT USAGE, SELECT ON SEQUENCE meeting_outcomes_outcome_id_seq TO blackink_system",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        count = db.execute(text("SELECT COUNT(*) FROM meeting_outcomes")).scalar()
    print(f"apply_meeting_outcomes: done — {count} rows present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
