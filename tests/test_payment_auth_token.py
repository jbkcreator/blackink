"""Unit tests for the signed, expiring onboarding token that stands in for
the not-yet-built authenticated onboarding portal (Subtask 1.2.1)."""

from datetime import timedelta

import jwt
import pytest

from config.settings import get_settings
from src.services.payment_auth_token import (
	PaymentAuthTokenError,
	decode_payment_auth_onboarding_token,
	mint_payment_auth_onboarding_token,
)


@pytest.fixture(autouse=True)
def _token_secret(monkeypatch):
	get_settings.cache_clear()
	monkeypatch.setenv("PAYMENT_AUTH_ONBOARDING_TOKEN_SECRET", "test-secret-do-not-use-in-prod")
	yield
	get_settings.cache_clear()


def test_mint_and_decode_round_trips_claims():
	token = mint_payment_auth_onboarding_token(client_id="acme_pm", company_id="co_1", offer_code="OWNER_GROWTH")
	claims = decode_payment_auth_onboarding_token(token)
	assert claims.client_id == "acme_pm"
	assert claims.company_id == "co_1"
	assert claims.offer_code == "OWNER_GROWTH"


def test_decode_rejects_garbage_token():
	with pytest.raises(PaymentAuthTokenError):
		decode_payment_auth_onboarding_token("not-a-real-token")


def test_decode_rejects_expired_token():
	token = mint_payment_auth_onboarding_token(
		client_id="acme_pm", company_id="co_1", offer_code="OWNER_GROWTH", expires_minutes=-1
	)
	with pytest.raises(PaymentAuthTokenError):
		decode_payment_auth_onboarding_token(token)


def test_decode_rejects_token_signed_with_wrong_secret():
	bad_payload = {"client_id": "acme_pm", "company_id": "co_1", "offer_code": "OWNER_GROWTH"}
	forged = jwt.encode(bad_payload, "some-other-secret", algorithm="HS256")
	with pytest.raises(PaymentAuthTokenError):
		decode_payment_auth_onboarding_token(forged)


def test_decode_rejects_token_missing_required_claim():
	get_settings()  # ensure settings loaded before manual jwt.encode below
	secret = get_settings().payment_auth_onboarding_token_secret.get_secret_value()
	incomplete = jwt.encode({"client_id": "acme_pm"}, secret, algorithm="HS256")
	with pytest.raises(PaymentAuthTokenError):
		decode_payment_auth_onboarding_token(incomplete)


def test_offer_code_is_bound_into_the_signed_token_not_a_separate_claim_a_caller_supplies():
	"""A request can never claim a DIFFERENT offer_code than the one the
	token was minted for — the router reads offer_code exclusively from
	the decoded token, never from a request field."""
	token = mint_payment_auth_onboarding_token(client_id="acme_pm", company_id="co_1", offer_code="RESPOND_SELF_SERVE")
	claims = decode_payment_auth_onboarding_token(token)
	assert claims.offer_code == "RESPOND_SELF_SERVE"
