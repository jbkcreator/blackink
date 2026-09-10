"""
Provision band2_counters — per-client consecutive-clean-send tracker (S-9).

Tracks consecutive successful email dispatches per (client_id, action_class,
touch_step) so the system can promote a template class from BAND_2_ONE_TAP
(human approval required) to BAND_3_AUTO once 50 consecutive sends land
without a failure. See src/services/autonomy_band.py for the read/write API.

touch_step is 0 for action classes that are not differentiated by step
(e.g. a future non-email class), and the literal touch step number for
DISPATCH_EMAIL_TOUCH and DISPATCH_WINBACK_TOUCH (1, 2, 3 …).

promoted_at is stamped once — the first time clean_streak crosses the
threshold — and is never reset on a subsequent failure: a template that
earned autonomous status does not lose it on a single bad send. clean_streak
resets to 0 on failure so the streak has to rebuild from scratch.

Not tenant-bearing (no RLS): this is operational automation metadata, not
customer data. Read/written via system context in autonomy_band.py.

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_band2_counters.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS band2_counters (
        client_id       VARCHAR(40)   NOT NULL,
        action_class    VARCHAR(100)  NOT NULL,
        touch_step      SMALLINT      NOT NULL DEFAULT 0,
        clean_streak    INTEGER       NOT NULL DEFAULT 0,
        promoted_at     TIMESTAMPTZ,
        updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
        PRIMARY KEY (client_id, action_class, touch_step),
        CONSTRAINT ck_band2_clean_streak CHECK (clean_streak >= 0)
    )
    """,
    "GRANT SELECT, INSERT, UPDATE ON band2_counters TO blackink_app",
    "GRANT SELECT, INSERT, UPDATE ON band2_counters TO blackink_system",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        count = db.execute(text("SELECT count(*) FROM band2_counters")).scalar()
    print(f"apply_band2_counters: done — {count} rows present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
