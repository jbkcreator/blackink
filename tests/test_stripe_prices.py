from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.services.billing import stripe_prices


class FakeDB:
	def __init__(self, rows):
		self.rows = rows
		self.commits = 0

	def execute(self, statement, params=None):
		if params:
			column = next(name for name in (
				"stripe_product_id", "stripe_base_price_id", "stripe_metered_price_id"
			) if f"{name} =" in str(statement))
			for row in self.rows:
				if row["offer_code"] == params["code"] and row.get(column) is None:
					row[column] = params["value"]
			return None
		return SimpleNamespace(mappings=lambda: SimpleNamespace(fetchall=lambda: self.rows))

	def commit(self):
		self.commits += 1


class FakeStripe:
	def __init__(self, fail_on_price_call=None):
		self.product_calls = []
		self.price_calls = []
		self.fail_on_price_call = fail_on_price_call

		class Products:
			def __init__(inner):
				inner.owner = self

			def create(inner, **kwargs):
				inner.owner.product_calls.append(kwargs)
				return SimpleNamespace(id=f"prod_{len(inner.owner.product_calls)}")

		class Prices:
			def __init__(inner):
				inner.owner = self

			def create(inner, **kwargs):
				inner.owner.price_calls.append(kwargs)
				if inner.owner.fail_on_price_call == len(inner.owner.price_calls):
					raise RuntimeError("Stripe unavailable")
				return SimpleNamespace(id=f"price_{len(inner.owner.price_calls)}")

		self.products = Products()
		self.prices = Prices()


def run_sync(monkeypatch, rows, stripe):
	db = FakeDB(rows)
	monkeypatch.setattr(stripe_prices, "get_system_db_context", lambda: contextmanager(lambda: (yield db))())
	monkeypatch.setattr("src.services.payment_auth._stripe_client", lambda: stripe)
	return stripe_prices.sync_offer_prices(catalog_confirmed=True), db


def row(code, price, per_sit, model):
	return {
		"offer_code": code,
		"display_name": code,
		"price_cents": price,
		"per_sit_cents": per_sit,
		"billing_model": model,
		"stripe_product_id": None,
		"stripe_base_price_id": None,
		"stripe_metered_price_id": None,
		"stripe_price_id": None,
	}


def test_growth_and_county_get_separate_base_and_metered_prices(monkeypatch):
	rows = [
		row("owner_growth", 74900, 9900, "SUBSCRIPTION_MONTHLY"),
		row("full_county", 119700, 9900, "SUBSCRIPTION_MONTHLY"),
	]
	stripe = FakeStripe()
	created, db = run_sync(monkeypatch, rows, stripe)

	assert set(created["owner_growth"]) == {"base", "metered"}
	assert set(created["full_county"]) == {"base", "metered"}
	assert [call["params"]["unit_amount"] for call in stripe.price_calls] == [74900, 9900, 119700, 9900]
	assert stripe.price_calls[0]["params"]["recurring"]["usage_type"] == "licensed"
	assert stripe.price_calls[1]["params"]["recurring"]["usage_type"] == "metered"
	assert rows[0]["stripe_base_price_id"] == "price_1"
	assert rows[1]["stripe_base_price_id"] == "price_3"
	assert db.commits == 6


def test_metered_event_uses_per_sit_amount_and_metered_recurring_price(monkeypatch):
	rows = [row("appt_standard", 9900, None, "METERED_EVENT")]
	stripe = FakeStripe()
	created, _ = run_sync(monkeypatch, rows, stripe)

	assert set(created["appt_standard"]) == {"metered"}
	assert rows[0]["stripe_base_price_id"] is None
	assert rows[0]["stripe_metered_price_id"] == "price_1"
	assert stripe.price_calls[0]["params"]["unit_amount"] == 9900
	assert stripe.price_calls[0]["params"]["recurring"]["usage_type"] == "metered"


def test_partial_failure_keeps_completed_offer_and_product_reference(monkeypatch):
	rows = [
		row("owner_growth", 74900, 9900, "SUBSCRIPTION_MONTHLY"),
		row("full_county", 119700, 9900, "SUBSCRIPTION_MONTHLY"),
	]
	stripe = FakeStripe(fail_on_price_call=3)

	with pytest.raises(RuntimeError, match="Stripe unavailable"):
		run_sync(monkeypatch, rows, stripe)

	assert rows[0]["stripe_product_id"] == "prod_1"
	assert rows[0]["stripe_base_price_id"] == "price_1"
	assert rows[1]["stripe_product_id"] == "prod_2"
	assert rows[1]["stripe_base_price_id"] is None
