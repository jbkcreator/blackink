"""Operator tool: create real Stripe Price objects for the entitlement SKUs.

BLOCKED until the client confirms the final SKU/pricing catalog (Source of
Truth O-02/O-03). Run only after that sign-off. Refuses to act without the
explicit --confirm-catalog flag, which asserts the catalog is final.

    PYTHONPATH=. python scripts/provision_stripe_prices.py --confirm-catalog

Idempotent: skips any SKU that already has a stripe_price_id, and creates
Stripe objects under deterministic idempotency keys, so a re-run is safe.
"""
import argparse
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.services.billing.stripe_prices import CatalogNotConfirmedError, sync_offer_prices


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--confirm-catalog",
		action="store_true",
		help="Assert the SKU/pricing catalog (O-02/O-03) is client-confirmed. Required.",
	)
	args = parser.parse_args()

	try:
		created = sync_offer_prices(catalog_confirmed=args.confirm_catalog)
	except CatalogNotConfirmedError as exc:
		print(f"provision_stripe_prices: {exc}")
		return 2

	if not created:
		print("provision_stripe_prices: no SKUs needed a new Stripe Price (all provisioned or FREE).")
		return 0
	for offer_code, price_id in created.items():
		print(f"  {offer_code} -> {price_id}")
	print(f"provision_stripe_prices: created {len(created)} Stripe Price object(s).")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
