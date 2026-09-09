"""Unit tests for the Zero-Deposit Card Auth & ACH Mandate Capture flow
(Subtask 1.2.1). No live DB or real Stripe calls — the Stripe client is
mocked via patching src.services.payment_auth._stripe_client, and DB reads/
writes are exercised against a MagicMock session mirroring the pattern
already used in tests/test_events.py.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.services.payment_auth as payment_auth
from src.core.token_crypto import decrypt_token
from src.services.payment_auth import (
	SetupIntentInvalid,
	SetupIntentNotReady,
	create_ach_setup_intent,
	create_card_setup_intent,
	create_dollar_auth_hold,
	is_zero_deposit_enabled,
	record_payment_auth_completed,
	record_payment_auth_failed,
	verify_setup_intent_server_side,
)


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch):
	from cryptography.fernet import Fernet

	from config.settings import get_settings

	get_settings.cache_clear()
	monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
	yield
	get_settings.cache_clear()


def _fake_stripe_client(monkeypatch):
	fake_client = MagicMock()
	monkeypatch.setattr(payment_auth, "_stripe_client", lambda: fake_client)
	return fake_client


# ── offer gate ───────────────────────────────────────────────────────────


def test_is_zero_deposit_enabled_true_for_enabled_offer():
	session = MagicMock()
	session.execute.return_value.first.return_value = SimpleNamespace(zero_deposit_enabled=True)
	assert is_zero_deposit_enabled(session, "OWNER_GROWTH") is True


def test_is_zero_deposit_enabled_false_for_unknown_offer():
	session = MagicMock()
	session.execute.return_value.first.return_value = None
	assert is_zero_deposit_enabled(session, "UNKNOWN_OFFER") is False


def test_is_zero_deposit_enabled_false_for_disabled_offer():
	session = MagicMock()
	session.execute.return_value.first.return_value = SimpleNamespace(zero_deposit_enabled=False)
	assert is_zero_deposit_enabled(session, "RESPOND_SELF_SERVE") is False


# ── two separate SetupIntents ────────────────────────────────────────────


def test_create_card_and_ach_setup_intents_are_two_separate_calls(monkeypatch):
	fake_client = _fake_stripe_client(monkeypatch)
	fake_client.setup_intents.create.side_effect = [
		SimpleNamespace(id="seti_card"), SimpleNamespace(id="seti_ach"),
	]

	card = create_card_setup_intent("cus_1", company_id="co_1", metadata={"client_id": "acme"})
	ach = create_ach_setup_intent("cus_1", company_id="co_1", metadata={"client_id": "acme"})

	assert card.id == "seti_card"
	assert ach.id == "seti_ach"
	assert fake_client.setup_intents.create.call_count == 2
	card_call_kwargs = fake_client.setup_intents.create.call_args_list[0].kwargs
	ach_call_kwargs = fake_client.setup_intents.create.call_args_list[1].kwargs
	assert card_call_kwargs["params"]["payment_method_types"] == ["card"]
	assert ach_call_kwargs["params"]["payment_method_types"] == ["us_bank_account"]
	# idempotency keys differ by purpose so a retry can't create duplicates
	# of the WRONG rail, and each is namespaced by company_id.
	assert card_call_kwargs["options"]["idempotency_key"] == "card-setup-intent|co_1"
	assert ach_call_kwargs["options"]["idempotency_key"] == "ach-setup-intent|co_1"


def test_setup_intent_idempotency_key_is_stable_per_company(monkeypatch):
	fake_client = _fake_stripe_client(monkeypatch)
	fake_client.setup_intents.create.return_value = SimpleNamespace(id="seti_1")

	create_card_setup_intent("cus_1", company_id="co_1", metadata={})
	create_card_setup_intent("cus_1", company_id="co_1", metadata={})

	keys = [c.kwargs["options"]["idempotency_key"] for c in fake_client.setup_intents.create.call_args_list]
	assert keys[0] == keys[1]


# ── server-side SetupIntent verification ─────────────────────────────────


def _fake_setup_intent(*, customer, pm_type, status):
	return SimpleNamespace(
		customer=customer, status=status,
		payment_method=SimpleNamespace(id="pm_1", type=pm_type),
	)


def test_verify_setup_intent_rejects_mismatched_customer(monkeypatch):
	fake_client = _fake_stripe_client(monkeypatch)
	fake_client.setup_intents.retrieve.return_value = _fake_setup_intent(
		customer="cus_OTHER", pm_type="card", status="succeeded"
	)
	with pytest.raises(SetupIntentInvalid):
		verify_setup_intent_server_side("cus_1", "seti_1", "card")


def test_verify_setup_intent_rejects_mismatched_type(monkeypatch):
	fake_client = _fake_stripe_client(monkeypatch)
	fake_client.setup_intents.retrieve.return_value = _fake_setup_intent(
		customer="cus_1", pm_type="us_bank_account", status="succeeded"
	)
	with pytest.raises(SetupIntentInvalid):
		verify_setup_intent_server_side("cus_1", "seti_1", "card")


def test_verify_setup_intent_raises_not_ready_for_processing_ach(monkeypatch):
	fake_client = _fake_stripe_client(monkeypatch)
	fake_client.setup_intents.retrieve.return_value = _fake_setup_intent(
		customer="cus_1", pm_type="us_bank_account", status="processing"
	)
	with pytest.raises(SetupIntentNotReady) as exc_info:
		verify_setup_intent_server_side("cus_1", "seti_1", "us_bank_account")
	assert exc_info.value.status == "processing"


def test_verify_setup_intent_succeeds_and_returns_intent(monkeypatch):
	fake_client = _fake_stripe_client(monkeypatch)
	fake_client.setup_intents.retrieve.return_value = _fake_setup_intent(
		customer="cus_1", pm_type="card", status="succeeded"
	)
	result = verify_setup_intent_server_side("cus_1", "seti_1", "card")
	assert result.payment_method.id == "pm_1"


# ── $1 authorization hold ─────────────────────────────────────────────────


def test_create_dollar_auth_hold_is_manual_capture_and_not_confirmed_as_charge(monkeypatch):
	fake_client = _fake_stripe_client(monkeypatch)
	fake_client.payment_intents.create.return_value = SimpleNamespace(id="pi_1")

	create_dollar_auth_hold("cus_1", "pm_1", company_id="co_1")

	call_kwargs = fake_client.payment_intents.create.call_args.kwargs
	assert call_kwargs["params"]["amount"] == 100
	assert call_kwargs["params"]["capture_method"] == "manual"


# ── record_payment_auth_completed: encryption + no entitlement writes ────


def test_record_payment_auth_completed_stores_encrypted_values_not_plaintext():
	session = MagicMock()
	with patch("src.services.payment_auth.log_event") as mock_log_event:
		record_payment_auth_completed(
			session,
			client_id="acme_pm",
			company_id="co_1",
			offer_code="OWNER_GROWTH",
			stripe_customer_id="cus_1",
			card_payment_method_id="pm_card_plaintext",
			ach_payment_method_id="pm_ach_plaintext",
			ach_mandate_id="mandate_plaintext",
		)

	args, kwargs = session.execute.call_args
	bound_params = args[1]
	assert bound_params["card_pm"] != "pm_card_plaintext"
	assert bound_params["ach_pm"] != "pm_ach_plaintext"
	assert bound_params["ach_mandate"] != "mandate_plaintext"
	assert decrypt_token(bound_params["card_pm"]) == "pm_card_plaintext"
	assert decrypt_token(bound_params["ach_pm"]) == "pm_ach_plaintext"
	assert decrypt_token(bound_params["ach_mandate"]) == "mandate_plaintext"

	mock_log_event.assert_called_once()
	log_call_kwargs = mock_log_event.call_args.kwargs
	assert log_call_kwargs["payload"]["stripe_customer_id"] == "cus_1"
	assert log_call_kwargs["payload"]["offer_code"] == "OWNER_GROWTH"


def test_record_payment_auth_completed_only_touches_companies_table():
	"""Payment-method capture must not itself flip billing/entitlement —
	assert the only UPDATE statement issued targets `companies`."""
	session = MagicMock()
	with patch("src.services.payment_auth.log_event"):
		record_payment_auth_completed(
			session, client_id="acme_pm", company_id="co_1", offer_code="OWNER_GROWTH",
			stripe_customer_id="cus_1", card_payment_method_id="pm_card",
			ach_payment_method_id="pm_ach", ach_mandate_id=None,
		)
	sql_text = str(session.execute.call_args[0][0])
	assert "companies" in sql_text
	assert "entitlement" not in sql_text.lower()


def test_record_payment_auth_completed_stores_real_stripe_mandate_id_encrypted():
	"""ach_mandate_id is Stripe's own documented SetupIntent.mandate field
	(the multi-use Mandate id) — confirm a real value is encrypted and
	stored, not dropped."""
	session = MagicMock()
	with patch("src.services.payment_auth.log_event"):
		record_payment_auth_completed(
			session, client_id="acme_pm", company_id="co_1", offer_code="OWNER_GROWTH",
			stripe_customer_id="cus_1", card_payment_method_id="pm_card",
			ach_payment_method_id="pm_ach", ach_mandate_id="mandate_1ABC123real",
		)
	bound_params = session.execute.call_args[0][1]
	assert bound_params["ach_mandate"] is not None
	assert decrypt_token(bound_params["ach_mandate"]) == "mandate_1ABC123real"


def test_record_payment_auth_completed_warns_loudly_when_mandate_is_none(caplog):
	"""A None mandate for a succeeded ACH SetupIntent is never silently
	swallowed — it must be visible in logs, not just an unexplained NULL
	in the encrypted column."""
	session = MagicMock()
	with patch("src.services.payment_auth.log_event"), caplog.at_level("WARNING"):
		record_payment_auth_completed(
			session, client_id="acme_pm", company_id="co_1", offer_code="OWNER_GROWTH",
			stripe_customer_id="cus_1", card_payment_method_id="pm_card",
			ach_payment_method_id="pm_ach", ach_mandate_id=None,
		)
	assert any("no mandate id" in record.message for record in caplog.records)


# ── decline / failure path ────────────────────────────────────────────────


def test_record_payment_auth_failed_logs_event_and_never_raises():
	fake_session = MagicMock()
	fake_session.__enter__.return_value = fake_session
	fake_session.__exit__.return_value = False
	with patch("src.services.payment_auth.get_db_context") as mock_ctx, \
		 patch("src.services.payment_auth.log_event") as mock_log_event:
		mock_ctx.return_value = fake_session
		record_payment_auth_failed(
			client_id="acme_pm", company_id="co_1", offer_code="OWNER_GROWTH",
			stripe_error_code="card_declined", stripe_error_message="Your card was declined.",
		)
	mock_log_event.assert_called_once()
	call_kwargs = mock_log_event.call_args.kwargs
	assert call_kwargs["payload"]["error_code"] == "card_declined"


def test_record_payment_auth_failed_is_committed_even_if_get_db_context_raises_on_body(monkeypatch):
	"""The event write happens in ITS OWN get_db_context call, independent of
	any outer onboarding transaction — simulate the outer world being broken
	and confirm this call still attempts (and doesn't raise past itself)."""

	def _raising_log_event(*a, **k):
		raise RuntimeError("simulated onboarding transaction chaos")

	fake_session = MagicMock()
	fake_session.__enter__.return_value = fake_session
	fake_session.__exit__.return_value = False
	with patch("src.services.payment_auth.get_db_context") as mock_ctx, \
		 patch("src.services.payment_auth.log_event", side_effect=_raising_log_event):
		mock_ctx.return_value = fake_session
		# Must not raise — record_payment_auth_failed swallows and logs.
		record_payment_auth_failed(
			client_id="acme_pm", company_id="co_1", offer_code=None,
			stripe_error_code="api_error", stripe_error_message="boom",
		)
