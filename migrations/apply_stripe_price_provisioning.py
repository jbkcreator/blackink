"""Add durable Stripe object references for entitlement price provisioning.

The legacy ``stripe_price_id`` column could represent only one component and
made a partially completed Stripe sync impossible to reconcile. These
nullable columns make the Product, base Price, and metered Price independently
durable. Additive and idempotent; run after apply_entitlements_billing.py.

    PYTHONPATH=. python migrations/apply_stripe_price_provisioning.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"ALTER TABLE entitlement_offers ADD COLUMN IF NOT EXISTS stripe_product_id VARCHAR(100)",
	"ALTER TABLE entitlement_offers ADD COLUMN IF NOT EXISTS stripe_base_price_id VARCHAR(100)",
	"ALTER TABLE entitlement_offers ADD COLUMN IF NOT EXISTS stripe_metered_price_id VARCHAR(100)",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_stripe_price_provisioning: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
