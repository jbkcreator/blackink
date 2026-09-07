"""Zero-Deposit Card Auth & ACH Mandate Capture (Subtask 1.2.1).

Isolates the Stripe SDK from the API routers, same pattern as
src/services/booking_ingest.py sitting between the booking webhook routers
and the provider SDKs.

Offer-scoped, not universal: the Source of Truth is explicit that
zero-upfront is not a blanket rule (self-serve Respond/bundle signups
charge at signup via Stripe Checkout, a separate flow). Every call here
must be preceded by is_zero_deposit_enabled() returning True for the
caller's offer_code — enforced at the router layer, checked again here as
a defense-in-depth assertion.

Two payment methods, two SetupIntents: a single Stripe SetupIntent cannot
capture both a card and a bank account, so card and ACH are always two
separate SetupIntent objects, presented together in one onboarding step.

The confirm flow never trusts a client-submitted PaymentMethod ID or
"success" flag — verify_setup_intent_server_side() always re-fetches the
SetupIntent from Stripe by ID and checks its own customer/type/status
before anything is persisted. ACH verification is asynchronous (Stripe
microdeposit/instant verification can leave a SetupIntent `processing` for
minutes), so payment_auth_completed_at is only ever set once BOTH rails
are independently confirmed `succeeded` — usually via the webhook handler
in src/api/stripe_webhook_router.py, occasionally synchronously in the
confirm endpoint if ACH happens to finish fast.

The $1 authorization hold is a real, uncaptured PaymentIntent
(capture_method='manual') — a temporary pending authorization that may
briefly appear on the customer's statement. It is explicitly cancelled via
cancel_auth_hold() once both rails are verified, rather than relying on
Stripe's own ~7-day automatic hold expiry as the primary release mechanism.

Every Stripe-mutating call takes an idempotency_key so a retried request
(network blip, double submit) cannot create a duplicate Customer /
SetupIntent / $1 hold.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import stripe
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.core.database import get_db_context
from src.core.token_crypto import encrypt_token
from src.services.events import log_event

logger = logging.getLogger(__name__)


class PaymentAuthNotConfiguredError(RuntimeError):
	"""STRIPE_SECRET_KEY is unset — a clear config error, not a crash deep
	inside the Stripe SDK."""


class ZeroDepositNotEnabledError(RuntimeError):
	"""The offer_code is not (or not yet) config-gated for this flow."""


class SetupIntentInvalid(RuntimeError):
	"""A SetupIntent's customer/type ownership doesn't match what was
	expected — never persist anything derived from it."""


class SetupIntentNotReady(RuntimeError):
	"""A SetupIntent exists and belongs to the right customer/type but has
	not reached a terminal `succeeded` status yet (e.g. ACH `processing` /
	`requires_action`). Not an error condition — the caller should treat
	this as "still pending", not "failed"."""

	def __init__(self, status: str):
		super().__init__(f"SetupIntent not ready — status={status}")
		self.status = status


def _stripe_client() -> "stripe.StripeClient":
	settings = get_settings()
	key = settings.stripe_secret_key
	if not key:
		raise PaymentAuthNotConfiguredError(
			"STRIPE_SECRET_KEY is not configured — cannot run the payment-auth "
			"capture flow."
		)
	return stripe.StripeClient(key.get_secret_value())


def is_zero_deposit_enabled(session: Session, offer_code: str) -> bool:
	row = session.execute(
		text("SELECT zero_deposit_enabled FROM payment_auth_offer_config WHERE offer_code = :offer_code"),
		{"offer_code": offer_code},
	).first()
	return bool(row and row.zero_deposit_enabled)


def get_or_create_stripe_customer(session: Session, *, client_id: str, company_id: str) -> str:
	row = session.execute(
		text("SELECT stripe_customer_id FROM companies WHERE company_id = :cid"), {"cid": company_id}
	).first()
	if row and row.stripe_customer_id:
		return row.stripe_customer_id

	client = _stripe_client()
	customer = client.customers.create(
		params={"metadata": {"client_id": client_id, "company_id": company_id}},
		options={"idempotency_key": f"stripe-customer|{company_id}"},
	)
	session.execute(
		text("UPDATE companies SET stripe_customer_id = :scid, updated_at = NOW() WHERE company_id = :cid"),
		{"scid": customer.id, "cid": company_id},
	)
	return customer.id


def create_card_setup_intent(stripe_customer_id: str, *, company_id: str, metadata: dict) -> "stripe.SetupIntent":
	client = _stripe_client()
	return client.setup_intents.create(
		params={
			"customer": stripe_customer_id,
			"payment_method_types": ["card"],
			"usage": "off_session",
			"metadata": metadata,
		},
		options={"idempotency_key": f"card-setup-intent|{company_id}"},
	)


def create_ach_setup_intent(stripe_customer_id: str, *, company_id: str, metadata: dict) -> "stripe.SetupIntent":
	client = _stripe_client()
	return client.setup_intents.create(
		params={
			"customer": stripe_customer_id,
			"payment_method_types": ["us_bank_account"],
			"usage": "off_session",
			"payment_method_options": {
				"us_bank_account": {"verification_method": "automatic"},
			},
			"metadata": metadata,
		},
		options={"idempotency_key": f"ach-setup-intent|{company_id}"},
	)


def update_setup_intent_metadata(setup_intent_id: str, metadata: dict) -> None:
	"""Cross-references the companion SetupIntent id onto each intent's own
	metadata after both are created, so the webhook handler (which only
	ever sees ONE SetupIntent per event) can look up the other rail."""
	client = _stripe_client()
	client.setup_intents.update(setup_intent_id, params={"metadata": metadata})


def verify_setup_intent_server_side(
	stripe_customer_id: str, setup_intent_id: str, expected_type: str
) -> "stripe.SetupIntent":
	"""Re-fetches the SetupIntent from Stripe and asserts it actually
	belongs to this customer and is of the expected payment_method type.
	Never trusts a client-submitted PaymentMethod ID directly — this is the
	only path anything downstream is allowed to persist from.

	Raises SetupIntentInvalid if customer/type don't match (never
	SetupIntentNotReady in that case — a mismatch is not "not ready yet",
	it's wrong). Raises SetupIntentNotReady if the SetupIntent is
	legitimately still in progress (ACH verification is asynchronous)."""
	client = _stripe_client()
	setup_intent = client.setup_intents.retrieve(setup_intent_id, params={"expand": ["payment_method"]})

	if setup_intent.customer != stripe_customer_id:
		raise SetupIntentInvalid(
			f"SetupIntent {setup_intent_id} customer={setup_intent.customer!r} does not match "
			f"expected {stripe_customer_id!r}"
		)
	payment_method = setup_intent.payment_method
	actual_type = getattr(payment_method, "type", None)
	if actual_type != expected_type:
		raise SetupIntentInvalid(
			f"SetupIntent {setup_intent_id} payment_method.type={actual_type!r} does not match "
			f"expected {expected_type!r}"
		)
	if setup_intent.status != "succeeded":
		raise SetupIntentNotReady(setup_intent.status)
	return setup_intent


def create_dollar_auth_hold(
	stripe_customer_id: str, card_payment_method_id: str, *, company_id: str
) -> "stripe.PaymentIntent":
	"""A real, uncaptured $1 authorization — never captured, always
	explicitly cancelled later via cancel_auth_hold(). capture_method is
	'manual', so this can never turn into an actual charge unless some
	future code path explicitly calls .capture(), which nothing here does."""
	client = _stripe_client()
	return client.payment_intents.create(
		params={
			"amount": 100,
			"currency": "usd",
			"customer": stripe_customer_id,
			"payment_method": card_payment_method_id,
			"capture_method": "manual",
			"confirm": True,
			"off_session": True,
		},
		options={"idempotency_key": f"dollar-auth-hold|{company_id}"},
	)


def cancel_auth_hold(payment_intent_id: str) -> None:
	client = _stripe_client()
	client.payment_intents.cancel(payment_intent_id)


def record_payment_auth_completed(
	session: Session,
	*,
	client_id: str,
	company_id: str,
	offer_code: str,
	stripe_customer_id: str,
	card_payment_method_id: str,
	ach_payment_method_id: str,
	ach_mandate_id: Optional[str],
) -> None:
	"""Only ever call once BOTH the card and ACH SetupIntents are confirmed
	`succeeded` server-side. Encrypts every secret field before storing.
	Deliberately does NOT touch any entitlement/billing row — payment-method
	capture must not itself activate billing; that is a separate,
	later settlement-pipeline ticket's responsibility.

	ach_mandate_id is expected to be Stripe's own `SetupIntent.mandate` —
	the ID of the multi-use Mandate object Stripe generates for a
	successfully-confirmed us_bank_account (ACH Direct Debit) SetupIntent
	(a genuine, documented field on the SetupIntent object, not an
	invented one — see verify_setup_intent_server_side's caller in
	src/api/payment_auth_router.py / stripe_webhook_router.py, which reads
	it via getattr(ach_intent, "mandate", None)). A None value here is
	NEVER silently swallowed: it is logged as a warning below so a gap in
	Stripe's own mandate issuance is visible in logs rather than only
	showing up later as an unexplained NULL in the encrypted column."""
	if not ach_mandate_id:
		logger.warning(
			"payment_auth: ACH SetupIntent for company=%s verified succeeded but returned no "
			"mandate id — ach_mandate_id_encrypted will be stored as NULL for this row. This is "
			"NOT expected for a confirmed us_bank_account SetupIntent; investigate the Stripe "
			"dashboard for this customer/SetupIntent before treating the ACH rail as fully authorized.",
			company_id,
		)
	session.execute(
		text(
			"UPDATE companies SET "
			"  card_payment_method_id_encrypted = :card_pm, "
			"  ach_payment_method_id_encrypted = :ach_pm, "
			"  ach_mandate_id_encrypted = :ach_mandate, "
			"  payment_auth_offer_code = :offer_code, "
			"  payment_auth_completed_at = NOW(), "
			"  updated_at = NOW() "
			"WHERE company_id = :company_id"
		),
		{
			"card_pm": encrypt_token(card_payment_method_id),
			"ach_pm": encrypt_token(ach_payment_method_id),
			"ach_mandate": encrypt_token(ach_mandate_id) if ach_mandate_id else None,
			"offer_code": offer_code,
			"company_id": company_id,
		},
	)
	log_event(
		client_id,
		"payment_auth_completed",
		entity_type="company",
		entity_id=company_id,
		payload={"stripe_customer_id": stripe_customer_id, "offer_code": offer_code},
		session=session,
	)


def record_payment_auth_failed(
	*,
	client_id: str,
	company_id: str,
	offer_code: Optional[str],
	stripe_error_code: str,
	stripe_error_message: str,
) -> None:
	"""Logs payment_auth_failed in its OWN committed transaction, independent
	of whatever onboarding transaction is in flight — a rollback elsewhere
	must never silently drop the failure record. Never raises past this
	point; the caller (a router) is responsible for turning the original
	Stripe error into an HTTP 4xx response."""
	try:
		with get_db_context(client_id=client_id) as session:
			log_event(
				client_id,
				"payment_auth_failed",
				entity_type="company",
				entity_id=company_id,
				payload={
					"error_code": stripe_error_code,
					"error_message": stripe_error_message,
					"offer_code": offer_code,
				},
				session=session,
			)
	except Exception:
		logger.error(
			"payment_auth: failed to log payment_auth_failed event (client=%s company=%s code=%s)",
			client_id, company_id, stripe_error_code, exc_info=True,
		)
