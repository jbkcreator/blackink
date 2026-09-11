"""Reconcile inbound_messages.ack_latency_seconds to a single definition.

Group D defect D-1 (Week 0-2 implementation audit): two migrations used to
create this column with conflicting definitions, each guarded by its own
"only if it doesn't already exist" check —

  - apply_entitlements_billing.py:  ack_latency_seconds INTEGER
        GENERATED ALWAYS AS (... EXTRACT(EPOCH FROM (acked_at - received_at)) ...)
        STORED
  - apply_inbound_messages_lead_fields.py (now fixed, see that file's own
        comment): ack_latency_seconds DOUBLE PRECISION, a plain writable column

Because both guards were "IF NOT EXISTS", whichever of the two migrations
ran first on a given database silently won, and the two never errored
against each other. Depending on which one an environment happened to run
first, ack_latency_seconds ended up either a plain writable column or a
generated one — a schema divergence between environments, not just a
symptom in one of them.

The GENERATED definition is the correct canonical one: it cannot drift out
of agreement with acked_at, and billing rule 1's miss-credit claim query
plus its supporting index (ix_inbound_messages_miss_credit_claim) both
assume a derived value. This migration converts a database currently
holding the plain-column variant to the generated one.

Safety: this only ever runs against a column already verified to hold zero
non-NULL values before it drops it (checked at 2026-09-10 against the live
Hetzner DB: 0 non-null out of 22 rows total, none in a Speed-to-Lead
status). It refuses — rather than silently discarding data — if it finds
any non-NULL value, since a database that has actually processed
Speed-to-Lead responses needs a real backfill, not a drop-and-recreate.

No-op if the column is already GENERATED (i.e. apply_entitlements_billing.py
already won on this database).

Rollback: this repo has no down-migrations. To revert this specific change
(restore the plain writable column), an operator would run, by hand:
    ALTER TABLE inbound_messages DROP COLUMN ack_latency_seconds;
    ALTER TABLE inbound_messages ADD COLUMN ack_latency_seconds DOUBLE PRECISION;
    CREATE INDEX IF NOT EXISTS ix_inbound_messages_miss_credit_claim
        ON inbound_messages(client_id, ack_latency_seconds)
        WHERE channel = 'EMAIL' AND intent IS NULL AND miss_credit_issued_at IS NULL;
Same non-null caveat in reverse: since the GENERATED column can only ever
hold a value derived from acked_at, no data is lost by dropping it — the
column value is always reproducible until acked_at itself is also dropped.
Reverting is expected to be unnecessary (the GENERATED definition is the
one every other part of this codebase — the miss-credit rule and
speed_to_lead_sweep.py — assumes), listed here only per this repo's
migration-review convention of stating the undo path explicitly.

Run AFTER apply_inbound_messages_lead_fields.py, BEFORE apply_rls_policies.py.

    PYTHONPATH=. python migrations/apply_ack_latency_reconcile.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

GENERATED_EXPR = """
    CASE WHEN acked_at IS NOT NULL
        THEN EXTRACT(EPOCH FROM (acked_at - received_at))::INTEGER
        ELSE NULL
    END
"""


def main() -> None:
    with get_owner_db_context() as session:
        row = session.execute(text("""
            SELECT is_generated
            FROM information_schema.columns
            WHERE table_name = 'inbound_messages'
              AND column_name = 'ack_latency_seconds'
        """)).fetchone()

        if row is None:
            print("apply_ack_latency_reconcile: column does not exist yet — "
                  "nothing to reconcile (a later migration will create it).")
            return

        if row[0] == "ALWAYS":
            print("apply_ack_latency_reconcile: already GENERATED — no-op.")
            return

        non_null = session.execute(text(
            "SELECT COUNT(*) FROM inbound_messages WHERE ack_latency_seconds IS NOT NULL"
        )).scalar()
        if non_null:
            raise RuntimeError(
                f"apply_ack_latency_reconcile: refusing to convert — "
                f"{non_null} row(s) already have a non-NULL ack_latency_seconds "
                f"under the plain-column definition. Write a real backfill "
                f"(derive acked_at from these values, then convert) instead of "
                f"running this migration as-is."
            )

        # Dropping the column also drops
        # ix_inbound_messages_miss_credit_claim (apply_entitlements_billing.py),
        # since Postgres cascades a column drop to any index built on it —
        # recreate it identically once the column is back.
        session.execute(text(
            "ALTER TABLE inbound_messages DROP COLUMN ack_latency_seconds"
        ))
        session.execute(text(f"""
            ALTER TABLE inbound_messages ADD COLUMN ack_latency_seconds INTEGER
                GENERATED ALWAYS AS ({GENERATED_EXPR}) STORED
        """))
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_inbound_messages_miss_credit_claim
                ON inbound_messages(client_id, ack_latency_seconds)
                WHERE channel = 'EMAIL' AND intent IS NULL AND miss_credit_issued_at IS NULL
        """))
        session.commit()
        print("apply_ack_latency_reconcile: converted plain column to GENERATED STORED, "
              "recreated ix_inbound_messages_miss_credit_claim.")


if __name__ == "__main__":
    main()
