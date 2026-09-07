"""HTTP-contract tests for the payment-auth onboarding endpoints
(Subtask 1.2.1) — no live DB, no real Stripe calls. Mirrors
tests/test_ghl_webhook.py's use of FastAPI's TestClient for literal
status-code/body contract assertions, with the DB and Stripe layers
monkeypatched at the module level.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from config.settings import get_settings
from src.api import payment_auth_router
from src.api.main import app
from src.services.payment_auth_token import mint_payment_auth_onboarding_token

client = TestClient(app)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
	get_settings.cache_clear()
	monkeypatch.setenv("PAYMENT_AUTH_ONBOARDING_TOKEN_SECRET", "test-secret")
	monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
	monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test_fake")
	yield
	get_settings.cache_clear()


def _token(offer_code="OWNER_GROWTH", **overrides):
	kwargs = {"client_id": "acme_pm", "company_id": "co_1", "offer_code": offer_code}
	kwargs.update(overrides)
	return mint_payment_auth_onboarding_token(**kwargs)


@pytest.fixture
def _db_session(monkeypatch):
	"""Patches get_db_context used inside the router module so no real
	Postgres connection is opened."""
	fake_session = MagicMock()
	fake_ctx = MagicMock()
	fake_ctx.__enter__.return_value = fake_session
	fake_ctx.__exit__.return_value = False
	monkeypatch.setattr(payment_auth_router, "get_db_context", lambda client_id=None: fake_ctx)
	return fake_session


# ── setup-intents ─────────────────────────────────────────────────────────


def test_setup_intents_rejects_invalid_token():
	resp = client.post("/api/v1/onboarding/payment-auth/setup-intents", json={"onboarding_token": "garbage"})
	assert resp.status_code == 401


def test_setup_intents_rejects_missing_body_field():
	resp = client.post("/api/v1/onboarding/payment-auth/setup-intents", json={})
	assert resp.status_code == 422


def test_setup_intents_rejects_disabled_offer(monkeypatch, _db_session):
	monkeypatch.setattr(payment_auth_router, "is_zero_deposit_enabled", lambda session, offer_code: False)
	resp = client.post("/api/v1/onboarding/payment-auth/setup-intents", json={"onboarding_token": _token()})
	assert resp.status_code == 403


def test_setup_intents_returns_contract_shaped_response(monkeypatch, _db_session):
	monkeypatch.setattr(payment_auth_router, "is_zero_deposit_enabled", lambda session, offer_code: True)
	monkeypatch.setattr(payment_auth_router, "get_or_create_stripe_customer", lambda session, **k: "cus_1")
	monkeypatch.setattr(
		payment_auth_router, "create_card_setup_intent",
		lambda *a, **k: SimpleNamespace(id="seti_card", client_secret="seti_card_secret_x"),
	)
	monkeypatch.setattr(
		payment_auth_router, "create_ach_setup_intent",
		lambda *a, **k: SimpleNamespace(id="seti_ach", client_secret="seti_ach_secret_x"),
	)
	monkeypatch.setattr(payment_auth_router, "update_setup_intent_metadata", lambda *a, **k: None)

	resp = client.post("/api/v1/onboarding/payment-auth/setup-intents", json={"onboarding_token": _token()})
	assert resp.status_code == 200
	body = resp.json()
	assert body["card_client_secret"] == "seti_card_secret_x"
	assert body["ach_client_secret"] == "seti_ach_secret_x"
	assert body["publishable_key"] == "pk_test_fake"


def test_setup_intents_503_when_stripe_not_configured(monkeypatch):
	monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
	get_settings.cache_clear()
	resp = client.post("/api/v1/onboarding/payment-auth/setup-intents", json={"onboarding_token": _token()})
	assert resp.status_code == 503


# ── confirm ───────────────────────────────────────────────────────────────


def test_confirm_rejects_invalid_token():
	resp = client.post(
		"/api/v1/onboarding/payment-auth/confirm",
		json={"onboarding_token": "garbage", "card_setup_intent_id": "seti_1", "ach_setup_intent_id": "seti_2"},
	)
	assert resp.status_code == 401


def test_confirm_404_for_unknown_company(monkeypatch, _db_session):
	monkeypatch.setattr(payment_auth_router, "is_zero_deposit_enabled", lambda session, offer_code: True)
	_db_session.execute.return_value.first.return_value = None
	resp = client.post(
		"/api/v1/onboarding/payment-auth/confirm",
		json={"onboarding_token": _token(), "card_setup_intent_id": "seti_1", "ach_setup_intent_id": "seti_2"},
	)
	assert resp.status_code == 404


def test_confirm_returns_already_completed_without_re_verifying_stripe(monkeypatch, _db_session):
	monkeypatch.setattr(payment_auth_router, "is_zero_deposit_enabled", lambda session, offer_code: True)
	_db_session.execute.return_value.first.return_value = SimpleNamespace(
		stripe_customer_id="cus_1", payment_auth_completed_at="2026-01-01T00:00:00Z"
	)
	verify_mock = MagicMock()
	monkeypatch.setattr(payment_auth_router, "verify_setup_intent_server_side", verify_mock)

	resp = client.post(
		"/api/v1/onboarding/payment-auth/confirm",
		json={"onboarding_token": _token(), "card_setup_intent_id": "seti_1", "ach_setup_intent_id": "seti_2"},
	)
	assert resp.status_code == 200
	assert resp.json()["status"] == "already_completed"
	verify_mock.assert_not_called()


# ── status ────────────────────────────────────────────────────────────────


def test_status_rejects_invalid_token():
	resp = client.get("/api/v1/onboarding/payment-auth/status", params={"onboarding_token": "garbage"})
	assert resp.status_code == 401


def test_status_404_for_unknown_company(monkeypatch, _db_session):
	_db_session.execute.return_value.first.return_value = None
	resp = client.get("/api/v1/onboarding/payment-auth/status", params={"onboarding_token": _token()})
	assert resp.status_code == 404


def test_status_reports_completion_flags(monkeypatch, _db_session):
	_db_session.execute.return_value.first.return_value = SimpleNamespace(
		payment_auth_completed_at="2026-01-01T00:00:00Z",
		card_payment_method_id_encrypted="enc_card",
		ach_payment_method_id_encrypted=None,
	)
	resp = client.get("/api/v1/onboarding/payment-auth/status", params={"onboarding_token": _token()})
	assert resp.status_code == 200
	body = resp.json()
	assert body["payment_auth_completed"] is True
	assert body["has_card_payment_method"] is True
	assert body["has_ach_payment_method"] is False


# ── test harness gating ─────────────────────────────────────────────────


def test_test_harness_page_404s_in_production(monkeypatch):
	monkeypatch.setenv("ENVIRONMENT", "production")
	get_settings.cache_clear()
	resp = client.get("/api/v1/onboarding/payment-auth/test-harness", params={"token": "whatever"})
	assert resp.status_code == 404


def test_test_harness_page_served_and_labeled_outside_production(monkeypatch):
	monkeypatch.setenv("ENVIRONMENT", "development")
	get_settings.cache_clear()
	resp = client.get("/api/v1/onboarding/payment-auth/test-harness", params={"token": "whatever"})
	assert resp.status_code == 200
	assert "NOT PRODUCTION" in resp.text
