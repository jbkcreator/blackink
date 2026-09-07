"""Zero-Deposit Card Auth & ACH Mandate Capture (Subtask 1.2.1) — backend
API contract for the frontend Stripe Elements integration.

╔══════════════════════════════════════════════════════════════════════╗
║ API CONTRACT (for the frontend team — also rendered in FastAPI's own ║
║ OpenAPI docs at /docs since every request/response below is a typed  ║
║ Pydantic model, not a bare dict):                                    ║
║                                                                        ║
║ 1. POST /api/v1/onboarding/payment-auth/setup-intents                ║
║    Request:  {"onboarding_token": "<jwt>"}                            ║
║    Response: {"card_client_secret": "seti_..._secret_...",           ║
║               "ach_client_secret": "seti_..._secret_...",            ║
║               "publishable_key": "pk_test_..."}                      ║
║    Frontend: Stripe(publishable_key).elements() → mount a `card`      ║
║    Element and confirm it against card_client_secret via              ║
║    stripe.confirmCardSetup(); mount/confirm the ACH rail against      ║
║    ach_client_secret via stripe.confirmUsBankAccountSetup(). Both in  ║
║    ONE onboarding step, side by side.                                 ║
║                                                                        ║
║ 2. POST /api/v1/onboarding/payment-auth/confirm                      ║
║    Request:  {"onboarding_token": "<jwt>",                            ║
║               "card_setup_intent_id": "seti_...",                     ║
║               "ach_setup_intent_id": "seti_..."}                      ║
║    Response: {"status": "completed" | "ach_pending" | "already_completed"} ║
║    Frontend sends the two `setup_intent.id` values Stripe.js returns  ║
║    from confirmCardSetup()/confirmUsBankAccountSetup() — NEVER any    ║
║    PaymentMethod id or a client-asserted success flag; the server     ║
║    re-verifies both against Stripe itself. "ach_pending" means ACH    ║
║    microdeposit/instant verification is still in progress — poll      ║
║    endpoint 3 below, or wait for your own server-side notification    ║
║    once the webhook (endpoint 4) completes it.                        ║
║                                                                        ║
║ 3. GET /api/v1/onboarding/payment-auth/status?onboarding_token=<jwt>  ║
║    Response: {"payment_auth_completed": bool,                         ║
║               "has_card_payment_method": bool,                        ║
║               "has_ach_payment_method": bool,                         ║
║               "offer_code": "OWNER_GROWTH"}                           ║
║    Frontend polls this after an "ach_pending" confirm response to     ║
║    learn when the webhook has finished the ACH rail.                  ║
║                                                                        ║
║ 4. POST /api/v1/webhooks/stripe — Stripe-called only, not by the      ║
║    frontend. See src/api/stripe_webhook_router.py.                    ║
╚══════════════════════════════════════════════════════════════════════╝

No client-portal login/session system exists anywhere in this repo yet
(no users/session table). Every endpoint above therefore authenticates
via a signed, expiring onboarding_token (src/services/payment_auth_token.py)
instead of trusting a bare client_id/company_id/offer_code supplied in the
request — client_id, company_id, and offer_code are NEVER read from an
untrusted request field; they come exclusively from the token's own
signed claims. This token is explicitly temporary integration-testing
scaffolding: the future authenticated onboarding portal is expected to
supply tenant context of its own (a real session, not this token) once it
exists — see the token module's docstring.

Never applies zero-upfront billing behavior unconditionally — every
request first checks payment_auth_offer_config via
is_zero_deposit_enabled(); an offer not explicitly flagged there (every
offer ships disabled by default) is rejected before any Stripe call is
made.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

import stripe
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_db_context
from src.services.payment_auth import (
	SetupIntentInvalid,
	SetupIntentNotReady,
	cancel_auth_hold,
	create_ach_setup_intent,
	create_card_setup_intent,
	create_dollar_auth_hold,
	get_or_create_stripe_customer,
	is_zero_deposit_enabled,
	record_payment_auth_completed,
	record_payment_auth_failed,
	update_setup_intent_metadata,
	verify_setup_intent_server_side,
)
from src.services.payment_auth_token import (
	PaymentAuthTokenClaims,
	PaymentAuthTokenError,
	decode_payment_auth_onboarding_token,
)

router = APIRouter(prefix="/api/v1/onboarding/payment-auth", tags=["payment-auth"])
logger = logging.getLogger(__name__)


def _decode_token(onboarding_token: str) -> PaymentAuthTokenClaims:
	try:
		return decode_payment_auth_onboarding_token(onboarding_token)
	except PaymentAuthTokenError as exc:
		raise HTTPException(status_code=401, detail="Invalid or expired onboarding token") from exc


# ── Request/response models — the frontend contract, see module docstring ──


class SetupIntentsRequest(BaseModel):
	onboarding_token: str = Field(..., description="Signed, short-lived onboarding token (see module docstring).")


class SetupIntentsResponse(BaseModel):
	card_client_secret: str = Field(..., description="Pass to stripe.confirmCardSetup().")
	ach_client_secret: str = Field(..., description="Pass to stripe.confirmUsBankAccountSetup().")
	publishable_key: str = Field(..., description="Pass to Stripe(publishable_key).")


class ConfirmRequest(BaseModel):
	onboarding_token: str = Field(..., description="Same token used for setup-intents.")
	card_setup_intent_id: str = Field(..., description="The `setup_intent.id` Stripe.js returned for the card rail.")
	ach_setup_intent_id: str = Field(..., description="The `setup_intent.id` Stripe.js returned for the ACH rail.")


class ConfirmResponse(BaseModel):
	status: Literal["completed", "ach_pending", "already_completed"]


class StatusResponse(BaseModel):
	payment_auth_completed: bool
	has_card_payment_method: bool
	has_ach_payment_method: bool
	offer_code: str


@router.post("/setup-intents", response_model=SetupIntentsResponse)
def create_setup_intents(body: SetupIntentsRequest) -> SetupIntentsResponse:
	settings = get_settings()
	if not settings.stripe_secret_key or not settings.stripe_publishable_key:
		raise HTTPException(status_code=503, detail="Stripe is not configured")

	claims = _decode_token(body.onboarding_token)

	with get_db_context(client_id=claims.client_id) as session:
		if not is_zero_deposit_enabled(session, claims.offer_code):
			raise HTTPException(
				status_code=403,
				detail=f"offer_code={claims.offer_code!r} is not enabled for zero-deposit payment auth",
			)

		try:
			stripe_customer_id = get_or_create_stripe_customer(
				session, client_id=claims.client_id, company_id=claims.company_id
			)
			base_metadata = {
				"client_id": claims.client_id,
				"company_id": claims.company_id,
				"offer_code": claims.offer_code,
			}
			card_intent = create_card_setup_intent(
				stripe_customer_id, company_id=claims.company_id, metadata=base_metadata
			)
			ach_intent = create_ach_setup_intent(
				stripe_customer_id, company_id=claims.company_id, metadata=base_metadata
			)
			# Cross-reference each intent onto the other so the webhook
			# handler (which only ever receives ONE SetupIntent per event)
			# can look up its companion rail.
			update_setup_intent_metadata(
				card_intent.id, {**base_metadata, "ach_setup_intent_id": ach_intent.id, "card_setup_intent_id": card_intent.id}
			)
			update_setup_intent_metadata(
				ach_intent.id, {**base_metadata, "card_setup_intent_id": card_intent.id, "ach_setup_intent_id": ach_intent.id}
			)
		except stripe.StripeError as exc:
			record_payment_auth_failed(
				client_id=claims.client_id, company_id=claims.company_id, offer_code=claims.offer_code,
				stripe_error_code=getattr(exc, "code", None) or "stripe_error", stripe_error_message=str(exc),
			)
			raise HTTPException(status_code=502, detail="Failed to initialize Stripe payment setup") from exc

	return SetupIntentsResponse(
		card_client_secret=card_intent.client_secret,
		ach_client_secret=ach_intent.client_secret,
		publishable_key=settings.stripe_publishable_key,
	)


@router.post("/confirm", response_model=ConfirmResponse)
def confirm_payment_auth(body: ConfirmRequest) -> ConfirmResponse:
	"""Never trusts a client-asserted PaymentMethod id or success flag —
	both SetupIntent ids are re-verified server-side against Stripe before
	anything is persisted. ACH verification is asynchronous: if it hasn't
	reached `succeeded` yet, this returns {status: "ach_pending"} and the
	webhook handler (src/api/stripe_webhook_router.py) finishes the job
	once Stripe delivers setup_intent.succeeded for the ACH rail — poll
	GET .../status in the meantime."""
	claims = _decode_token(body.onboarding_token)

	with get_db_context(client_id=claims.client_id) as session:
		if not is_zero_deposit_enabled(session, claims.offer_code):
			raise HTTPException(status_code=403, detail=f"offer_code={claims.offer_code!r} is not enabled")

		row = session.execute(
			text("SELECT stripe_customer_id, payment_auth_completed_at FROM companies WHERE company_id = :cid"),
			{"cid": claims.company_id},
		).first()
		if row is None or not row.stripe_customer_id:
			raise HTTPException(status_code=404, detail="No Stripe customer on file for this company")
		if row.payment_auth_completed_at is not None:
			return ConfirmResponse(status="already_completed")
		stripe_customer_id = row.stripe_customer_id

		try:
			card_intent = verify_setup_intent_server_side(stripe_customer_id, body.card_setup_intent_id, "card")
		except SetupIntentInvalid as exc:
			raise HTTPException(status_code=400, detail=str(exc)) from exc
		except SetupIntentNotReady as exc:
			raise HTTPException(status_code=409, detail=f"Card setup not ready: {exc.status}") from exc
		except stripe.StripeError as exc:
			record_payment_auth_failed(
				client_id=claims.client_id, company_id=claims.company_id, offer_code=claims.offer_code,
				stripe_error_code=getattr(exc, "code", None) or "stripe_error", stripe_error_message=str(exc),
			)
			raise HTTPException(status_code=502, detail="Failed to verify card setup") from exc

		# The $1 hold is created (and its id stored) the moment the card
		# rail is confirmed, BEFORE we know whether ACH is done yet — so it
		# can always be explicitly cancelled later regardless of how ACH
		# verification resolves.
		try:
			hold = create_dollar_auth_hold(
				stripe_customer_id, card_intent.payment_method.id, company_id=claims.company_id
			)
		except stripe.CardError as exc:
			record_payment_auth_failed(
				client_id=claims.client_id, company_id=claims.company_id, offer_code=claims.offer_code,
				stripe_error_code=exc.code or "card_declined", stripe_error_message=str(exc),
			)
			raise HTTPException(status_code=402, detail={"error_code": exc.code, "message": str(exc)}) from exc
		except stripe.StripeError as exc:
			record_payment_auth_failed(
				client_id=claims.client_id, company_id=claims.company_id, offer_code=claims.offer_code,
				stripe_error_code=getattr(exc, "code", None) or "stripe_error", stripe_error_message=str(exc),
			)
			raise HTTPException(status_code=502, detail="Failed to place $1 authorization hold") from exc

		session.execute(
			text(
				"UPDATE companies SET payment_auth_hold_payment_intent_id = :pid, updated_at = NOW() "
				"WHERE company_id = :cid"
			),
			{"pid": hold.id, "cid": claims.company_id},
		)

		try:
			ach_intent = verify_setup_intent_server_side(stripe_customer_id, body.ach_setup_intent_id, "us_bank_account")
		except SetupIntentNotReady:
			# ACH is genuinely still in progress — the webhook finishes this
			# once Stripe delivers setup_intent.succeeded for it. The $1 hold
			# stays open (not yet cancelled) until both rails verify.
			return ConfirmResponse(status="ach_pending")
		except SetupIntentInvalid as exc:
			# ACH will never complete for this SetupIntent id — the card hold
			# already placed above must not be left open waiting for a rail
			# that just failed verification.
			try:
				cancel_auth_hold(hold.id)
			except Exception:
				logger.error(
					"payment_auth: failed to cancel $1 auth hold %s for company %s after ACH "
					"verification failure", hold.id, claims.company_id, exc_info=True,
				)
			raise HTTPException(status_code=400, detail=str(exc)) from exc

		# Both rails already succeeded synchronously (ACH can sometimes
		# complete fast enough for this) — finish here instead of waiting
		# for the webhook.
		#
		# ach_intent.mandate is Stripe's own documented SetupIntent field —
		# "ID of the multi use Mandate generated by the SetupIntent" — the
		# actual reusable ACH authorization evidence, not the PaymentMethod
		# id and not an invented attribute. record_payment_auth_completed
		# logs a warning (never silently NULLs) if Stripe returns none for
		# a succeeded us_bank_account SetupIntent.
		record_payment_auth_completed(
			session,
			client_id=claims.client_id,
			company_id=claims.company_id,
			offer_code=claims.offer_code,
			stripe_customer_id=stripe_customer_id,
			card_payment_method_id=card_intent.payment_method.id,
			ach_payment_method_id=ach_intent.payment_method.id,
			ach_mandate_id=getattr(ach_intent, "mandate", None),
		)

	try:
		cancel_auth_hold(hold.id)
	except Exception:
		logger.error(
			"payment_auth: failed to cancel $1 auth hold %s for company %s", hold.id, claims.company_id, exc_info=True
		)

	return ConfirmResponse(status="completed")


@router.get("/status", response_model=StatusResponse)
def payment_auth_status(onboarding_token: str = Query(...)) -> StatusResponse:
	"""Secure status-check endpoint — same onboarding_token auth as the
	other two endpoints. Lets the frontend poll after an "ach_pending"
	confirm response to learn when the webhook has finished the ACH rail,
	without re-deriving state itself."""
	claims = _decode_token(onboarding_token)
	with get_db_context(client_id=claims.client_id) as session:
		row = session.execute(
			text(
				"SELECT payment_auth_completed_at, card_payment_method_id_encrypted, "
				"       ach_payment_method_id_encrypted "
				"FROM companies WHERE company_id = :cid"
			),
			{"cid": claims.company_id},
		).first()
	if row is None:
		raise HTTPException(status_code=404, detail="Unknown company")
	return StatusResponse(
		payment_auth_completed=row.payment_auth_completed_at is not None,
		has_card_payment_method=row.card_payment_method_id_encrypted is not None,
		has_ach_payment_method=row.ach_payment_method_id_encrypted is not None,
		offer_code=claims.offer_code,
	)


# ── Non-production test harness ─────────────────────────────────────────
#
# NOT PRODUCTION CODE. This is a standalone HTML page for manually
# exercising the three endpoints above during Stripe test-mode
# verification (see scripts/dev_mint_payment_auth_token.py, which mints
# the onboarding_token this page needs). It is not part of the real
# onboarding-portal frontend — building that is a separate frontend task,
# and the actual embedded Stripe Elements experience shipped there is
# NOT considered complete by this backend work. Available ONLY in local/
# dev/test environments (settings.is_production is False) — returns a
# plain 404 whenever settings.environment is "production" (or anything
# else is_production treats as production), so it can never be
# accidentally reached in a real deployment.

_TEST_HARNESS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>[TEST HARNESS — NOT PRODUCTION] Payment Auth</title>
<script src="https://js.stripe.com/v3/"></script>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 2rem 1.25rem; background: #2a0000; color: #1a1a1a; }
  .banner { max-width: 480px; margin: 0 auto 1rem; background: #ffdddd; border: 2px solid #c00; border-radius: 8px; padding: .75rem 1rem; font-weight: 700; color: #900; text-align: center; }
  .card { max-width: 480px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 2rem 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  h1 { font-size: 1.3rem; margin: 0 0 .5rem; }
  h2 { font-size: 1rem; margin: 1.25rem 0 .5rem; }
  p.note { color: #555; line-height: 1.5; font-size: .9rem; }
  .elements-mount { border: 1px solid #ccc; border-radius: 8px; padding: .75rem; margin-bottom: .75rem; }
  button { width: 100%; margin-top: 1rem; padding: .8rem; border: none; border-radius: 8px; background: #1a1a1a; color: #fff; font-size: 1rem; cursor: pointer; }
  button:disabled { opacity: .6; cursor: not-allowed; }
  #status { margin-top: 1rem; font-size: .9rem; }
</style>
</head>
<body>
<div class="banner">TEST HARNESS — NOT PRODUCTION CODE<br>For Stripe test-mode verification only. Requires a token from scripts/dev_mint_payment_auth_token.py.</div>
<div class="card">
  <h1>Payment authorization (test harness)</h1>
  <p class="note">A temporary $1 authorization may briefly appear as pending on your statement — it is never charged.</p>
  <h2>Card</h2>
  <div id="card-element" class="elements-mount"></div>
  <h2>Bank account (ACH)</h2>
  <div id="ach-element" class="elements-mount"></div>
  <button id="submit-btn" type="button" disabled>Authorize payment methods</button>
  <div id="status"></div>
</div>
<script>
(function () {
  var params = new URLSearchParams(window.location.search);
  var onboardingToken = params.get('token');
  var statusEl = document.getElementById('status');
  var submitBtn = document.getElementById('submit-btn');
  var stripe, cardElement, elements;

  if (!onboardingToken) {
    statusEl.textContent = 'Missing ?token=... (mint one with scripts/dev_mint_payment_auth_token.py).';
    return;
  }

  fetch('/api/v1/onboarding/payment-auth/setup-intents', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ onboarding_token: onboardingToken })
  }).then(function (r) { return r.json().then(function (data) { return { httpOk: r.ok, data: data }; }); })
    .then(function (result) {
      if (!result.httpOk) {
        statusEl.textContent = (result.data && result.data.detail) || 'Unable to start payment setup.';
        return;
      }
      var data = result.data;
      stripe = Stripe(data.publishable_key);
      elements = stripe.elements();
      cardElement = elements.create('card');
      cardElement.mount('#card-element');

      submitBtn.dataset.cardSecret = data.card_client_secret;
      submitBtn.dataset.achSecret = data.ach_client_secret;
      submitBtn.disabled = false;
    }).catch(function () {
      statusEl.textContent = 'Something went wrong loading payment setup.';
    });

  submitBtn.addEventListener('click', function () {
    submitBtn.disabled = true;
    statusEl.textContent = 'Verifying card...';
    stripe.confirmCardSetup(submitBtn.dataset.cardSecret, {
      payment_method: { card: cardElement },
    }).then(function (cardResult) {
      if (cardResult.error) {
        statusEl.textContent = cardResult.error.message;
        submitBtn.disabled = false;
        return;
      }
      statusEl.textContent = 'Verifying bank account...';
      return stripe.confirmUsBankAccountSetup(submitBtn.dataset.achSecret).then(function (achResult) {
        if (achResult.error) {
          statusEl.textContent = achResult.error.message;
          submitBtn.disabled = false;
          return;
        }
        statusEl.textContent = 'Confirming...';
        return fetch('/api/v1/onboarding/payment-auth/confirm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            onboarding_token: onboardingToken,
            card_setup_intent_id: cardResult.setupIntent.id,
            ach_setup_intent_id: achResult.setupIntent.id,
          }),
        }).then(function (r) { return r.json().then(function (data) { return { httpOk: r.ok, data: data }; }); })
          .then(function (confirmResult) {
            if (!confirmResult.httpOk) {
              statusEl.textContent = (confirmResult.data && confirmResult.data.detail) || 'Payment authorization failed.';
              submitBtn.disabled = false;
              return;
            }
            if (confirmResult.data.status === 'ach_pending') {
              statusEl.textContent = "Verifying your bank account — we'll confirm shortly.";
            } else {
              statusEl.textContent = 'Payment authorization complete.';
            }
          });
      });
    }).catch(function () {
      statusEl.textContent = 'Something went wrong confirming payment setup.';
      submitBtn.disabled = false;
    });
  });
})();
</script>
</body>
</html>
"""


@router.get("/test-harness", response_class=HTMLResponse)
def payment_auth_test_harness_page() -> HTMLResponse:
	"""NOT PRODUCTION CODE — see the section banner above. Available only
	in local/dev/test environments; returns 404 in production so this can
	never be accidentally reached in a production deployment."""
	if get_settings().is_production:
		raise HTTPException(status_code=404)
	return HTMLResponse(content=_TEST_HARNESS_HTML)
