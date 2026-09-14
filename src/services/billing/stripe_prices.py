"""Provision real Stripe Price objects for the entitlement SKUs.

Today every SKU in `entitlement_offers` has `stripe_price_id` NULL, so every
charge is a one-off Stripe Invoice rather than a real Price/Subscription
object. Populating `stripe_price_id` is blocked on the client confirming the
final SKU/pricing catalog (Source of Truth open items O-02/O-03 — the
12-product Stripe list and the $99-any-door vs $75-dead-book relationship are
unresolved). This module is the wiring that runs *once that catalog is
confirmed*: it creates a Stripe Product + Price for each SKU from the price
already recorded on its `entitlement_offers` row and writes the resulting
`stripe_price_id` back, idempotently.

Fail-closed by design: `sync_offer_prices()` refuses to do anything unless
the caller passes `catalog_confirmed=True`, because creating live Price
objects from an unconfirmed catalog would bake in prices the client has not
signed off on — exactly the guessing this repo forbids. It never overwrites a
SKU that already has a `stripe_price_id`, and it creates Prices under a
per-offer deterministic idempotency key so a re-run can't produce duplicate
Stripe objects.

Deliberately NOT part of settlement/gateway.py's StripeGateway ABC: that
interface is scoped to the six money-moving calls charge.py is allowed to
make. Creating a Price is catalog setup, not a charge, so it uses the shared
Stripe client (payment_auth._stripe_client) directly.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

from src.core.database import get_system_db_context

logger = logging.getLogger(__name__)

# entitlement_offers.billing_model -> whether the Stripe Price recurs monthly.
# METERED_EVENT (per-sit) and SUBSCRIPTION_MONTHLY recur; one-off packs and
# FREE do not. FREE SKUs (price_cents = 0, e.g. appt_first) get no Price at
# all — Stripe rejects a $0 recurring price and there is nothing to bill.
_RECURRING_MODELS = {"SUBSCRIPTION_MONTHLY", "METERED_EVENT"}


class CatalogNotConfirmedError(RuntimeError):
	"""Raised when sync_offer_prices() is called without catalog_confirmed."""


def sync_offer_prices(*, catalog_confirmed: bool = False) -> dict[str, str]:
	"""Create a Stripe Price for every enabled SKU that lacks one and store
	its id on entitlement_offers.stripe_price_id.

	Returns a mapping of offer_code -> newly-created stripe_price_id (only the
	rows this run created). A SKU that already has a stripe_price_id, or is
	FREE / zero-priced, is skipped.

	Raises CatalogNotConfirmedError unless catalog_confirmed=True — the
	operator asserting the client has signed off on O-02/O-03.
	"""
	if not catalog_confirmed:
		raise CatalogNotConfirmedError(
			"Refusing to create Stripe Price objects: the SKU/pricing catalog "
			"(Source of Truth O-02/O-03) is not confirmed. Pass catalog_confirmed=True "
			"only once the client has signed off on the final SKUs and prices."
		)

	from src.services.payment_auth import _stripe_client

	client = _stripe_client()
	created: dict[str, str] = {}

	with get_system_db_context() as db:
		rows = db.execute(
			text(
				"SELECT offer_code, display_name, price_cents, per_sit_cents, "
				"       billing_model, stripe_price_id "
				"FROM entitlement_offers "
				"WHERE is_enabled = TRUE "
				"ORDER BY offer_code"
			)
		).mappings().fetchall()

		for row in rows:
			if row["stripe_price_id"]:
				continue  # already provisioned — never create a duplicate
			price_id = _create_price_for_offer(client, dict(row))
			if price_id is None:
				continue  # FREE / zero-priced SKU — nothing to bill
			db.execute(
				text(
					"UPDATE entitlement_offers SET stripe_price_id = :pid, updated_at = NOW() "
					"WHERE offer_code = :code AND stripe_price_id IS NULL"
				),
				{"pid": price_id, "code": row["offer_code"]},
			)
			created[row["offer_code"]] = price_id
			logger.info("stripe_prices: created price %s for offer %s", price_id, row["offer_code"])
		db.commit()

	return created


def _create_price_for_offer(client, offer: dict) -> Optional[str]:
	"""Create one Stripe Price (and its Product) for a SKU. Returns the Price
	id, or None for a SKU with nothing to bill (FREE / zero cents)."""
	amount_cents = int(offer["price_cents"] or 0)
	if offer["billing_model"] == "FREE" or amount_cents <= 0:
		return None

	# Deterministic idempotency key per offer, no timestamp — a re-run
	# reuses it rather than creating a second Product/Price.
	idem = f"entitlement-price|{offer['offer_code']}"

	product = client.products.create(
		params={"name": offer["display_name"], "metadata": {"offer_code": offer["offer_code"]}},
		options={"idempotency_key": f"{idem}|product"},
	)
	price_params = {
		"product": product.id,
		"currency": "usd",
		"unit_amount": amount_cents,
		"metadata": {"offer_code": offer["offer_code"]},
	}
	if offer["billing_model"] in _RECURRING_MODELS:
		price_params["recurring"] = {"interval": "month"}
	price = client.prices.create(params=price_params, options={"idempotency_key": f"{idem}|price"})
	return price.id
