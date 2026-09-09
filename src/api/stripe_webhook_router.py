"""Stripe webhook receiver (Subtask 1.2.1). Handles the asynchronous half
of ACH SetupIntent verification — Stripe's microdeposit/instant
verification can leave a SetupIntent `processing` for minutes after the
synchronous confirm request returns, so `payment_auth_completed` is only
ever written once both the card and ACH SetupIntents are independently
confirmed `succeeded`, which for ACH usually happens here rather than in
the synchronous /confirm endpoint.

Every inbound event is signature-verified (stripe.Webhook.construct_event)
before anything is read from it — same fail-closed posture as the GHL
webhook's shared-secret header check in src/services/ghl_webhook.py.
Every event id is recorded in stripe_webhook_events BEFORE processing, so
a redelivered event (Stripe explicitly does this on timeout) is a no-op,
not a duplicate payment_auth_completed write.
"""

from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from config.settings import get_settings
from src.core.database import get_db_context
from src.services.payment_auth import (
	SetupIntentInvalid,
	SetupIntentNotReady,
	cancel_auth_hold,
	create_dollar_auth_hold,
	record_payment_auth_completed,
	record_payment_auth_failed,
	verify_setup_intent_server_side,
)

router = APIRouter(prefix="/api/v1/webhooks", tags=["stripe-webhooks"])
logger = logging.getLogger(__name__)


def _mark_event_seen(session, stripe_event_id: str, event_type: str) -> bool:
	"""Returns True if this is the first time this event id has been seen
	(safe to process), False if it's a redelivery (already recorded)."""
	try:
		session.execute(
			text(
				"INSERT INTO stripe_webhook_events (stripe_event_id, event_type) "
				"VALUES (:id, :event_type)"
			),
			{"id": stripe_event_id, "event_type": event_type},
		)
		return True
	except IntegrityError:
		session.rollback()
		return False


@router.post("/stripe")
async def stripe_webhook(request: Request):
	settings = get_settings()
	if not settings.stripe_webhook_secret:
		raise HTTPException(status_code=503, detail="Stripe webhook receiving is not configured")

	payload = await request.body()
	sig_header = request.headers.get("stripe-signature", "")
	try:
		event = stripe.Webhook.construct_event(
			payload, sig_header, settings.stripe_webhook_secret.get_secret_value()
		)
	except (ValueError, stripe.SignatureVerificationError) as exc:
		raise HTTPException(status_code=401, detail="Invalid Stripe webhook signature") from exc

	event_type = event["type"]

	if event_type in (
		"invoice.paid", "invoice.payment_failed", "invoice.voided", "charge.dispute.created",
	):
		return _handle_settlement_event(event)

	if event_type not in ("setup_intent.succeeded", "setup_intent.setup_failed"):
		return {"status": "ignored"}

	setup_intent_obj = event["data"]["object"]
	client_id = (setup_intent_obj.get("metadata") or {}).get("client_id")
	company_id = (setup_intent_obj.get("metadata") or {}).get("company_id")
	offer_code = (setup_intent_obj.get("metadata") or {}).get("offer_code")
	if not client_id or not company_id:
		# Not one of ours (metadata is only set by create_card_setup_intent /
		# create_ach_setup_intent below) — accept and ignore rather than 400,
		# since Stripe will otherwise keep retrying an event we'll never claim.
		return {"status": "ignored_no_metadata"}

	with get_db_context(client_id=client_id) as session:
		if not _mark_event_seen(session, event["id"], event_type):
			return {"status": "duplicate_ignored"}

		if event_type == "setup_intent.setup_failed":
			last_error = setup_intent_obj.get("last_setup_error") or {}
			record_payment_auth_failed(
				client_id=client_id,
				company_id=company_id,
				offer_code=offer_code,
				stripe_error_code=last_error.get("code", "unknown"),
				stripe_error_message=last_error.get("message", "SetupIntent failed"),
			)
			# Either rail failing permanently means this onboarding attempt can
			# never reach payment_auth_completed — any $1 hold already placed
			# (by the synchronous /confirm endpoint, or by this same handler on
			# an earlier delivery) must not be left open indefinitely.
			hold_row = session.execute(
				text(
					"SELECT payment_auth_hold_payment_intent_id, payment_auth_completed_at "
					"FROM companies WHERE company_id = :cid"
				),
				{"cid": company_id},
			).first()
			if (
				hold_row is not None
				and hold_row.payment_auth_hold_payment_intent_id
				and hold_row.payment_auth_completed_at is None
			):
				try:
					cancel_auth_hold(hold_row.payment_auth_hold_payment_intent_id)
				except Exception:
					logger.error(
						"stripe_webhook: failed to cancel $1 auth hold %s for company %s after "
						"setup_intent.setup_failed",
						hold_row.payment_auth_hold_payment_intent_id, company_id, exc_info=True,
					)
			session.execute(
				text("UPDATE stripe_webhook_events SET processed_at = NOW() WHERE stripe_event_id = :id"),
				{"id": event["id"]},
			)
			return {"status": "recorded_failed"}

		# setup_intent.succeeded — check whether BOTH the card and ACH
		# SetupIntents for this company are now succeeded. Both PaymentMethod
		# ids are read from the stored companies row (set by the /confirm
		# endpoint the moment each SetupIntent's id was learned), never
		# trusted from the webhook payload's own PaymentMethod directly.
		row = session.execute(
			text(
				"SELECT stripe_customer_id, payment_auth_hold_payment_intent_id, "
				"       payment_auth_completed_at "
				"FROM companies WHERE company_id = :cid"
			),
			{"cid": company_id},
		).first()
		if row is None or row.payment_auth_completed_at is not None:
			# Already completed (or unknown company) — nothing further to do.
			session.execute(
				text("UPDATE stripe_webhook_events SET processed_at = NOW() WHERE stripe_event_id = :id"),
				{"id": event["id"]},
			)
			return {"status": "no_op"}

		card_setup_intent_id = (setup_intent_obj.get("metadata") or {}).get("card_setup_intent_id")
		ach_setup_intent_id = (setup_intent_obj.get("metadata") or {}).get("ach_setup_intent_id")
		stripe_customer_id = row.stripe_customer_id

		try:
			card_intent = verify_setup_intent_server_side(stripe_customer_id, card_setup_intent_id, "card")
			ach_intent = verify_setup_intent_server_side(stripe_customer_id, ach_setup_intent_id, "us_bank_account")
		except SetupIntentNotReady:
			# The other rail isn't done yet — this is expected; a later
			# webhook delivery for the other SetupIntent will complete it.
			session.execute(
				text("UPDATE stripe_webhook_events SET processed_at = NOW() WHERE stripe_event_id = :id"),
				{"id": event["id"]},
			)
			return {"status": "waiting_on_other_rail"}
		except SetupIntentInvalid as exc:
			logger.error("stripe_webhook: SetupIntent ownership mismatch: %s", exc)
			record_payment_auth_failed(
				client_id=client_id, company_id=company_id, offer_code=offer_code,
				stripe_error_code="setup_intent_ownership_mismatch", stripe_error_message=str(exc),
			)
			session.execute(
				text("UPDATE stripe_webhook_events SET processed_at = NOW() WHERE stripe_event_id = :id"),
				{"id": event["id"]},
			)
			return {"status": "rejected_mismatch"}

		# Both rails have independently verified succeeded here. Stripe can
		# deliver these two setup_intent.succeeded events before the
		# frontend's synchronous /confirm request ever reaches the API — in
		# that ordering /confirm's own $1-hold placement never runs, and
		# without this the flow would complete having never actually placed
		# or verified the required card authorization hold. So this handler
		# places one itself whenever /confirm hasn't already.
		hold_id = row.payment_auth_hold_payment_intent_id
		if not hold_id:
			try:
				hold = create_dollar_auth_hold(
					stripe_customer_id, card_intent.payment_method.id, company_id=company_id
				)
			except stripe.StripeError as exc:
				logger.error(
					"stripe_webhook: failed to place $1 auth hold for company %s before completing "
					"payment auth: %s", company_id, exc, exc_info=True,
				)
				record_payment_auth_failed(
					client_id=client_id, company_id=company_id, offer_code=offer_code,
					stripe_error_code=getattr(exc, "code", None) or "stripe_error",
					stripe_error_message=str(exc),
				)
				session.execute(
					text("UPDATE stripe_webhook_events SET processed_at = NOW() WHERE stripe_event_id = :id"),
					{"id": event["id"]},
				)
				return {"status": "hold_placement_failed"}
			hold_id = hold.id
			session.execute(
				text(
					"UPDATE companies SET payment_auth_hold_payment_intent_id = :pid, updated_at = NOW() "
					"WHERE company_id = :cid"
				),
				{"pid": hold_id, "cid": company_id},
			)

		# ach_intent.mandate is Stripe's own documented SetupIntent field —
		# "ID of the multi use Mandate generated by the SetupIntent" — the
		# actual reusable ACH authorization evidence, not the PaymentMethod
		# id. record_payment_auth_completed logs a warning (never silently
		# NULLs) if Stripe returns none for a succeeded SetupIntent.
		record_payment_auth_completed(
			session,
			client_id=client_id,
			company_id=company_id,
			offer_code=offer_code,
			stripe_customer_id=stripe_customer_id,
			card_payment_method_id=card_intent.payment_method.id,
			ach_payment_method_id=ach_intent.payment_method.id,
			ach_mandate_id=getattr(ach_intent, "mandate", None),
		)
		try:
			cancel_auth_hold(hold_id)
		except Exception:
			logger.error(
				"stripe_webhook: failed to cancel $1 auth hold %s for company %s",
				hold_id, company_id, exc_info=True,
			)
		session.execute(
			text("UPDATE stripe_webhook_events SET processed_at = NOW() WHERE stripe_event_id = :id"),
			{"id": event["id"]},
		)
		return {"status": "completed"}


def _handle_settlement_event(event: dict) -> dict:
	"""Subtask 1.2.2 — invoice.paid/payment_failed/voided and
	charge.dispute.created for the settlement engine. Resolves
	(transaction_id, installment) from invoice metadata (written by
	src/services/settlement/charge.py) — NEVER by matching amount, since
	two installments could coincidentally share one. Dedups through the
	same stripe_webhook_events ledger before any state change."""
	from datetime import datetime, timezone

	from src.services.settlement.ledger import mark_installment, mark_installment_failed

	obj = event["data"]["object"]
	event_type = event["type"]

	if event_type == "charge.dispute.created":
		# Log only — appointment_disputes stays the dispute record of truth.
		logger.warning("stripe_webhook: charge.dispute.created for charge=%s", obj.get("charge"))
		return {"status": "logged_dispute"}

	metadata = obj.get("metadata") or {}
	client_id = metadata.get("client_id")
	transaction_id = metadata.get("transaction_id")
	installment = metadata.get("installment")
	if not client_id or not transaction_id or not installment:
		return {"status": "ignored_no_metadata"}

	transaction_id = int(transaction_id)
	installment = int(installment)

	with get_db_context(client_id=client_id) as session:
		if not _mark_event_seen(session, event["id"], event_type):
			return {"status": "duplicate_ignored"}

		if event_type == "invoice.paid":
			paid_at_raw = (obj.get("status_transitions") or {}).get("paid_at")
			charged_at = (
				datetime.fromtimestamp(paid_at_raw, tz=timezone.utc) if paid_at_raw else datetime.now(timezone.utc)
			)
			mark_installment(
				session, transaction_id, installment, "CHARGED",
				charged_at=charged_at, stripe_invoice_id=obj.get("id"),
			)
		elif event_type == "invoice.payment_failed":
			last_error = (obj.get("last_finalization_error") or {}).get("message", "invoice.payment_failed")
			mark_installment_failed(session, transaction_id, installment, attempts=0, error=last_error)
		elif event_type == "invoice.voided":
			row = session.execute(
				text(
					f"SELECT installment_{installment}_status AS status FROM settlement_transactions "
					f"WHERE transaction_id = :tid"
				),
				{"tid": transaction_id},
			).first()
			if row and row.status not in ("VOIDED", "VOIDED_CLAWBACK"):
				logger.warning(
					"stripe_webhook: invoice voided outside the clawback pipeline (transaction=%s installment=%s status=%s) — someone acted in the Stripe dashboard",
					transaction_id, installment, row.status,
				)
				mark_installment(session, transaction_id, installment, "VOIDED")

		session.execute(
			text("UPDATE stripe_webhook_events SET processed_at = NOW() WHERE stripe_event_id = :id"),
			{"id": event["id"]},
		)
	return {"status": "recorded"}
