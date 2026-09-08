"""Client billing-account plumbing (PR #37 review — blocking findings 1/2:
neither resolve_sit_charge() nor apply_pending_credits_to_invoice() was ever
called by production code, because no production code path had anywhere to
send the resulting invoice: `clients` (the paying tenant) carries no Stripe
identity anywhere in this repo. `companies.stripe_customer_id` (Subtask
1.2.1) is a DIFFERENT thing — the PROSPECTED PM firm a client is pitching,
not the client's own billing account — so it cannot be reused here without
invoicing the wrong entity.

Adds the minimal column needed to make sit billing reachable: an OPTIONAL
`clients.stripe_customer_id`. No client-portal/onboarding flow exists yet to
populate it (same class of gap as the payment-auth onboarding token and the
calendar-connect link) — until an operator or a future onboarding flow sets
it, `src/services/billing/sit_invoice.py`'s sweep marks every appointment for
that client BLOCKED/NO_STRIPE_CUSTOMER rather than crashing or guessing an
identity.

Also adds `appointments.billing_blocked_reason` — the sit-billing sweep's own
claim-state column, mirroring appointment_disputes.credit_status's role: a
reason for why an appointment could not be invoiced, so the claim query can
exclude it instead of re-attempting it every tick forever.

Idempotent: ADD COLUMN IF NOT EXISTS throughout.

    PYTHONPATH=. python migrations/apply_client_billing_account.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255)",
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS billing_blocked_reason VARCHAR(50)",
    "ALTER TABLE appointments DROP CONSTRAINT IF EXISTS ck_appointments_billing_blocked_reason",
    """
    ALTER TABLE appointments ADD CONSTRAINT ck_appointments_billing_blocked_reason
        CHECK (billing_blocked_reason IS NULL OR billing_blocked_reason IN ('NO_STRIPE_CUSTOMER'))
    """,
    # Claim-query index: unbilled, non-blocked ATTENDED appointments only.
    """
    CREATE INDEX IF NOT EXISTS ix_appointments_unbilled_attended
        ON appointments (client_id, scheduled_for)
        WHERE state = 'ATTENDED' AND is_billable AND billed_offer_code IS NULL
              AND billing_blocked_reason IS NULL
    """,
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'clients' AND column_name = 'stripe_customer_id'"
            )
        ).fetchall()
    print(f"apply_client_billing_account: done — clients.stripe_customer_id present={bool(cols)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
