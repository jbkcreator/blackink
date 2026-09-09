"""Zero-Deposit Card Auth & ACH Mandate Capture (Subtask 1.2.1).

Adds the columns/tables the card+ACH SetupIntent capture flow needs on
`companies`, plus two new small tables:

- `payment_auth_offer_config` — the config gate that keeps this flow
  offer-scoped rather than a universal zero-upfront rule. The Source of
  Truth is explicit that zero-upfront is NOT universal (self-serve
  Respond/bundle signups charge at signup via Stripe Checkout, a separate
  flow entirely). Every row here ships with zero_deposit_enabled = FALSE;
  an operator flips it only for a client-confirmed offer. No code path
  ever hardcodes an offer_code branch — same "commercial terms belong in
  configuration rows" rule already followed for entitlement_offers.
- `stripe_webhook_events` — idempotency ledger for inbound Stripe webhook
  deliveries (Stripe can and does redeliver on timeout).

Idempotent: ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS
throughout.

    PYTHONPATH=. python migrations/apply_payment_auth_capture.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	# ── companies: additive-only payment-auth columns ───────────────────────
	"ALTER TABLE companies ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(255)",
	"ALTER TABLE companies ADD COLUMN IF NOT EXISTS card_payment_method_id_encrypted TEXT",
	"ALTER TABLE companies ADD COLUMN IF NOT EXISTS ach_payment_method_id_encrypted TEXT",
	"ALTER TABLE companies ADD COLUMN IF NOT EXISTS ach_mandate_id_encrypted TEXT",
	"ALTER TABLE companies ADD COLUMN IF NOT EXISTS payment_auth_offer_code VARCHAR(50)",
	"ALTER TABLE companies ADD COLUMN IF NOT EXISTS payment_auth_hold_payment_intent_id VARCHAR(255)",
	"ALTER TABLE companies ADD COLUMN IF NOT EXISTS payment_auth_completed_at TIMESTAMPTZ",

	# ── payment_auth_offer_config: the config gate ──────────────────────────
	"""
	CREATE TABLE IF NOT EXISTS payment_auth_offer_config (
		offer_code            VARCHAR(50)  PRIMARY KEY,
		zero_deposit_enabled  BOOLEAN      NOT NULL DEFAULT FALSE,
		created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
	)
	""",
	# Not tenant-bearing — reference config, same class as entitlement_offers
	# (see config/tenant_policies.py's comment on global reference tables).
	"GRANT SELECT ON payment_auth_offer_config TO blackink_app",
	"GRANT SELECT ON payment_auth_offer_config TO blackink_system",

	# ── stripe_webhook_events: idempotency ledger for inbound webhooks ──────
	"""
	CREATE TABLE IF NOT EXISTS stripe_webhook_events (
		stripe_event_id  VARCHAR(255)  PRIMARY KEY,
		event_type       VARCHAR(100)  NOT NULL,
		received_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
		processed_at     TIMESTAMPTZ
	)
	""",
	# Not tenant-bearing — a Stripe event carries no client_id of its own
	# until the payload is inspected; RLS would have nothing to scope on.
	"GRANT SELECT, INSERT, UPDATE ON stripe_webhook_events TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON stripe_webhook_events TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_payment_auth_capture: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
