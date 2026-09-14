"""Provision durable Stripe Products and component Prices for entitlement SKUs."""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text

from src.core.database import get_system_db_context

logger = logging.getLogger(__name__)


class CatalogNotConfirmedError(RuntimeError):
	"""Raised when sync_offer_prices() is called without catalog confirmation."""


def _components_for_offer(offer: dict[str, Any]) -> dict[str, int]:
	model = offer["billing_model"]
	base_cents = int(offer["price_cents"] or 0)
	per_sit_cents = int(offer["per_sit_cents"] or 0)
	if model == "FREE":
		return {}
	if model == "METERED_EVENT":
		amount = per_sit_cents or base_cents
		return {"metered": amount} if amount > 0 else {}
	components = {"base": base_cents} if base_cents > 0 else {}
	if per_sit_cents > 0:
		components["metered"] = per_sit_cents
	return components


def sync_offer_prices(*, catalog_confirmed: bool = False) -> dict[str, dict[str, str]]:
	"""Create and persist each offer's base and/or metered Stripe Price.

	Each product and price is committed immediately after creation, so a later
	failure cannot roll back completed offers or the product reference needed to
	resume the failed offer.
	"""
	if not catalog_confirmed:
		raise CatalogNotConfirmedError(
			"Refusing to create Stripe Price objects: the SKU/pricing catalog "
			"(Source of Truth O-02/O-03) is not confirmed. Pass catalog_confirmed=True "
			"only once the client has signed off on the final SKUs and prices."
		)

	from src.services.payment_auth import _stripe_client

	client = _stripe_client()
	created: dict[str, dict[str, str]] = {}
	with get_system_db_context() as db:
		rows = db.execute(
			text(
				"SELECT offer_code, display_name, price_cents, per_sit_cents, "
				"       billing_model, stripe_product_id, stripe_base_price_id, "
				"       stripe_metered_price_id, stripe_price_id "
				"FROM entitlement_offers WHERE is_enabled = TRUE ORDER BY offer_code"
			)
		).mappings().fetchall()

		for row in rows:
			offer = dict(row)
			components = _components_for_offer(offer)
			if not components:
				continue
			product_id = offer.get("stripe_product_id")
			if not product_id:
				product_id = _create_product(client, offer)
				_persist_id(db, "stripe_product_id", product_id, offer["offer_code"])
			for component, amount_cents in components.items():
				column = f"stripe_{component}_price_id"
				price_id = offer.get(column)
				# Legacy IDs may seed a base price, but never satisfy a metered offer.
				if not price_id and component == "base" and offer.get("stripe_price_id"):
					price_id = offer["stripe_price_id"]
					_persist_id(db, column, price_id, offer["offer_code"])
				if price_id:
					continue
				price_id = _create_price(client, offer, product_id, component, amount_cents)
				_persist_id(db, column, price_id, offer["offer_code"])
				created.setdefault(offer["offer_code"], {})[component] = price_id
				logger.info("stripe_prices: created %s price %s for offer %s", component, price_id, offer["offer_code"])

	return created


def _persist_id(db: Any, column: str, value: str, offer_code: str) -> None:
	db.execute(
		text(
			f"UPDATE entitlement_offers SET {column} = :value, updated_at = NOW() "
			"WHERE offer_code = :code AND " + column + " IS NULL"
		),
		{"value": value, "code": offer_code},
	)
	db.commit()


def _create_product(client: Any, offer: dict[str, Any]) -> str:
	return client.products.create(
		params={"name": offer["display_name"], "metadata": {"offer_code": offer["offer_code"]}},
		options={"idempotency_key": f"entitlement-price|{offer['offer_code']}|product"},
	).id


def _create_price(client: Any, offer: dict[str, Any], product_id: str, component: str, amount_cents: int) -> str:
	params: dict[str, Any] = {
		"product": product_id, "currency": "usd", "unit_amount": amount_cents,
		"metadata": {"offer_code": offer["offer_code"], "component": component},
	}
	if component == "metered":
		params["recurring"] = {"interval": "month", "usage_type": "metered"}
	elif offer["billing_model"] == "SUBSCRIPTION_MONTHLY":
		params["recurring"] = {"interval": "month", "usage_type": "licensed"}
	return client.prices.create(
		params=params,
		options={"idempotency_key": f"entitlement-price|{offer['offer_code']}|{component}|price"},
	).id


def _create_price_for_offer(client: Any, offer: dict[str, Any]) -> Optional[str]:
	"""Compatibility seam for callers that imported the old helper."""
	components = _components_for_offer(offer)
	if not components:
		return None
	component, amount_cents = next(iter(components.items()))
	product_id = _create_product(client, offer)
	return _create_price(client, offer, product_id, component, amount_cents)
