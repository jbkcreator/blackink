"""Fail-closed guard against a live over-billing gap found in the Week-2
implementation audit (2026-09-14): `run_sit_invoice_sweep` claims any
`state='ATTENDED' AND is_billable` appointment and charges a real Stripe
invoice, with zero check for the two-party attended-duration evidence the
Source of Truth's 4-rule appointment gate requires (Rule 2 — both parties
present >=12 minutes, stored as `proof_ref`). `is_billable` is documented
in this repo's own CLAUDE.md as covering only the two-tier confirmation +
ATTENDED state — "it must not be the sole billing truth" — and the Source
of Truth states this explicitly: an invite is not attendance evidence, and
the accepted failure direction throughout this repo's billing paths is
always under-billing, never over-billing.

This migration does NOT implement the 4-rule gate itself, and does NOT
decide how attendance/duration evidence is captured (calendar-bridge
timestamp? transcript webhook? Zoom/Meet duration API? manual
attestation?) — that provider is still unspecified per the Source of
Truth's own open conflict item O-10 ("`proof_ref` provider... must be
specified"). It adds only the two nullable columns the Source of Truth's
own schema fragments already name for this purpose (`proof_ref`,
`attended_duration_seconds`), and closes the billing sweep so that a row
missing this evidence is blocked rather than silently invoiced. This is
currently a no-op in production: no appointment anywhere has `proof_ref`
set yet (nothing writes it), so every ATTENDED+is_billable row that would
previously have been claimed is now blocked instead — which is the
correct, fail-closed behavior until the capture mechanism is decided and
built.

Idempotent: ADD COLUMN IF NOT EXISTS / DROP+ADD CONSTRAINT throughout.

Must run AFTER migrations/apply_client_billing_account.py — this
migration's CHECK constraint references appointments.billing_blocked_reason,
which that migration is what actually creates. Running this one earlier
fails with "column billing_blocked_reason does not exist" (caught by a
fresh-database migration test, 2026-09-14). See CLAUDE.md's Common
Commands for the full authoritative ordering.

    PYTHONPATH=. python migrations/apply_appointment_attendance_proof.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    # Names taken directly from the Source of Truth's own schema fragments
    # (proof_ref TEXT NOT NULL, attended_duration_seconds INTEGER NOT NULL
    # DEFAULT 0 in the illustrative 4-rule-gate DDL) so a future real
    # implementation of the capture mechanism slots into these same columns
    # without a rename. Nullable here, deliberately: no writer exists yet,
    # and a NOT NULL/DEFAULT 0 column would either fail every existing
    # ATTENDED row's write path or (worse) make an unevaluated row look
    # like a verified zero-duration meeting instead of "not yet captured".
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS proof_ref TEXT",
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS attended_duration_seconds INTEGER",
    # Extend the sit-billing sweep's existing block-reason column (added in
    # apply_client_billing_account.py) with a second, independent reason —
    # reusing the one existing blocking mechanism rather than inventing a
    # parallel one, per this repo's own "one write path" convention.
    "ALTER TABLE appointments DROP CONSTRAINT IF EXISTS ck_appointments_billing_blocked_reason",
    """
    ALTER TABLE appointments ADD CONSTRAINT ck_appointments_billing_blocked_reason
        CHECK (billing_blocked_reason IS NULL
               OR billing_blocked_reason IN ('NO_STRIPE_CUSTOMER', 'MISSING_ATTENDANCE_PROOF'))
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
                "WHERE table_name = 'appointments' AND column_name IN ('proof_ref', 'attended_duration_seconds')"
            )
        ).fetchall()
    print(f"apply_appointment_attendance_proof: done — columns present={sorted(c[0] for c in cols)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
