"""Regression tests for two payment-auth $1-hold bugs found in PR #27
review, fixed in src/api/stripe_webhook_router.py:

1. A definite ACH/card setup_intent.setup_failed left an already-placed
   $1 hold open forever — nothing ever cancelled it.
2. Stripe can deliver both setup_intent.succeeded events before the
   frontend's synchronous /confirm request ever reaches the API. In that
   race, /confirm's own hold placement never runs, so payment auth could
   complete having never actually placed or verified the required $1
   card-authorization hold.

No live DB, no real Stripe calls — mirrors tests/test_payment_auth_router.py's
approach of monkeypatching the module-level DB/Stripe functions directly and
driving the router through FastAPI's TestClient.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from config.settings import get_settings
from src.api import stripe_webhook_router
from src.api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
	get_settings.cache_clear()
	monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fake")
	monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
	yield
	get_settings.cache_clear()


@pytest.fixture
def _db_session(monkeypatch):
	fake_session = MagicMock()
	fake_ctx = MagicMock()
	fake_ctx.__enter__.return_value = fake_session
	fake_ctx.__exit__.return_value = False
	monkeypatch.setattr(stripe_webhook_router, "get_db_context", lambda client_id=None: fake_ctx)
	return fake_session


def _post_event(event: dict):
	return client.post(
		"/api/v1/webhooks/stripe",
		content=b"{}",
		headers={"stripe-signature": "sig"},
	), event


def _fire(monkeypatch, event: dict):
	monkeypatch.setattr(stripe_webhook_router.stripe.Webhook, "construct_event", lambda *a, **k: event)
	return client.post("/api/v1/webhooks/stripe", content=b"{}", headers={"stripe-signature": "sig"})


def _metadata_event(event_type: str) -> dict:
	return {
		"id": "evt_1",
		"type": event_type,
		"data": {
			"object": {
				"metadata": {
					"client_id": "acme_pm",
					"company_id": "co_1",
					"offer_code": "OWNER_GROWTH",
					"card_setup_intent_id": "seti_card",
					"ach_setup_intent_id": "seti_ach",
				},
				"last_setup_error": {"code": "card_declined", "message": "declined"},
			}
		},
	}


# ── Bug 1: setup_failed must cancel an already-placed hold ──────────────


def test_setup_failed_cancels_existing_open_hold(monkeypatch, _db_session):
	monkeypatch.setattr(stripe_webhook_router, "record_payment_auth_failed", MagicMock())
	cancel_mock = MagicMock()
	monkeypatch.setattr(stripe_webhook_router, "cancel_auth_hold", cancel_mock)

	insert_result = MagicMock()  # _mark_event_seen's INSERT — no exception raised
	hold_select_result = MagicMock()
	hold_select_result.first.return_value = SimpleNamespace(
		payment_auth_hold_payment_intent_id="pi_open_hold", payment_auth_completed_at=None
	)
	update_result = MagicMock()
	_db_session.execute.side_effect = [insert_result, hold_select_result, update_result]

	resp = _fire(monkeypatch, _metadata_event("setup_intent.setup_failed"))

	assert resp.status_code == 200
	assert resp.json()["status"] == "recorded_failed"
	cancel_mock.assert_called_once_with("pi_open_hold")


def test_setup_failed_does_not_cancel_when_no_hold_or_already_completed(monkeypatch, _db_session):
	monkeypatch.setattr(stripe_webhook_router, "record_payment_auth_failed", MagicMock())
	cancel_mock = MagicMock()
	monkeypatch.setattr(stripe_webhook_router, "cancel_auth_hold", cancel_mock)

	insert_result = MagicMock()
	hold_select_result = MagicMock()
	hold_select_result.first.return_value = SimpleNamespace(
		payment_auth_hold_payment_intent_id=None, payment_auth_completed_at=None
	)
	update_result = MagicMock()
	_db_session.execute.side_effect = [insert_result, hold_select_result, update_result]

	resp = _fire(monkeypatch, _metadata_event("setup_intent.setup_failed"))

	assert resp.status_code == 200
	cancel_mock.assert_not_called()


# ── Bug 2: webhook-races-/confirm must still place+verify+cancel a hold ──


def test_succeeded_race_places_and_cancels_hold_when_confirm_never_ran(monkeypatch, _db_session):
	card_intent = SimpleNamespace(payment_method=SimpleNamespace(id="pm_card"), mandate=None)
	ach_intent = SimpleNamespace(payment_method=SimpleNamespace(id="pm_ach"), mandate="mandate_1")

	monkeypatch.setattr(
		stripe_webhook_router, "verify_setup_intent_server_side",
		lambda customer_id, seti_id, expected_type: card_intent if expected_type == "card" else ach_intent,
	)
	create_hold_mock = MagicMock(return_value=SimpleNamespace(id="pi_new_hold"))
	monkeypatch.setattr(stripe_webhook_router, "create_dollar_auth_hold", create_hold_mock)
	cancel_mock = MagicMock()
	monkeypatch.setattr(stripe_webhook_router, "cancel_auth_hold", cancel_mock)
	record_completed_mock = MagicMock()
	monkeypatch.setattr(stripe_webhook_router, "record_payment_auth_completed", record_completed_mock)

	insert_result = MagicMock()
	row_select_result = MagicMock()
	row_select_result.first.return_value = SimpleNamespace(
		stripe_customer_id="cus_1",
		payment_auth_hold_payment_intent_id=None,  # /confirm never ran — no hold placed yet
		payment_auth_completed_at=None,
	)
	hold_update_result = MagicMock()
	final_update_result = MagicMock()
	_db_session.execute.side_effect = [insert_result, row_select_result, hold_update_result, final_update_result]

	resp = _fire(monkeypatch, _metadata_event("setup_intent.succeeded"))

	assert resp.status_code == 200
	assert resp.json()["status"] == "completed"
	create_hold_mock.assert_called_once_with("cus_1", "pm_card", company_id="co_1")
	cancel_mock.assert_called_once_with("pi_new_hold")
	record_completed_mock.assert_called_once()


def test_succeeded_reuses_existing_hold_placed_by_confirm(monkeypatch, _db_session):
	card_intent = SimpleNamespace(payment_method=SimpleNamespace(id="pm_card"), mandate=None)
	ach_intent = SimpleNamespace(payment_method=SimpleNamespace(id="pm_ach"), mandate="mandate_1")

	monkeypatch.setattr(
		stripe_webhook_router, "verify_setup_intent_server_side",
		lambda customer_id, seti_id, expected_type: card_intent if expected_type == "card" else ach_intent,
	)
	create_hold_mock = MagicMock()
	monkeypatch.setattr(stripe_webhook_router, "create_dollar_auth_hold", create_hold_mock)
	cancel_mock = MagicMock()
	monkeypatch.setattr(stripe_webhook_router, "cancel_auth_hold", cancel_mock)
	monkeypatch.setattr(stripe_webhook_router, "record_payment_auth_completed", MagicMock())

	insert_result = MagicMock()
	row_select_result = MagicMock()
	row_select_result.first.return_value = SimpleNamespace(
		stripe_customer_id="cus_1",
		payment_auth_hold_payment_intent_id="pi_from_confirm",  # /confirm already placed one
		payment_auth_completed_at=None,
	)
	final_update_result = MagicMock()
	_db_session.execute.side_effect = [insert_result, row_select_result, final_update_result]

	resp = _fire(monkeypatch, _metadata_event("setup_intent.succeeded"))

	assert resp.status_code == 200
	create_hold_mock.assert_not_called()
	cancel_mock.assert_called_once_with("pi_from_confirm")
