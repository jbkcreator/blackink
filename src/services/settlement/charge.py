"""The sole money-moving module for the settlement engine (Subtask 1.2.2).

This is the ONLY file in the repo permitted to call a Stripe API that
finalizes/pays an invoice or creates an invoice item —
tests/test_no_upfront_charge_paths.py asserts that structurally by
grepping src/. charge_installment() re-reads the settlement row and its
agreement from the DB and re-derives the amount and every precondition
from there; it never accepts an amount, a customer id, or a payment-method
id as an argument, which is what makes the zero-upfront rule un-bypassable
from any call site (see migrations/apply_settlement_ledger.py's docstring
for the other five layers of this same property).

Two preflight refusals happen before any Stripe object is created, both
returning a BLOCKED outcome rather than raising:
  - EVIDENCE_PACKET_UNPUBLISHED — the packet must be compiled AND published
    first. If store.publish() returns None (no store configured) or raises
    EvidencePacketPublishError (a genuine Stripe Files failure), nothing is
    invoiced. This BLOCKED is TRANSIENT: the row is deferred via
    ledger.defer_installment() with exponential backoff and stays reclaimable
    by claim_installment_1/2 — it used to be marked terminally BLOCKED,
    which meant a Stripe Files outage (or the store simply misconfigured,
    see PR #30 review finding 1) forfeited the charge forever.
  - SYNTHETIC_AGREEMENT_IN_LIVE_MODE — a non-PMS_SYNC agreement (i.e. the
    DoD's synthetic door_signed test path) may only back a charge when the
    configured Stripe secret key is a sk_test_ key. In live mode this
    refuses outright, so a synthetic agreement is structurally unable to
    bill a real client in production. This BLOCKED is STRUCTURAL, not
    transient — it stays terminal and is never reclaimed (see
    ledger._RETRYABLE_BLOCKED_REASONS's own comment for why).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.token_crypto import decrypt_token
from src.services.events import log_event
from src.services.settlement.evidence import assemble_evidence_packet
from src.services.settlement.evidence_pdf import compile_packet
from src.services.settlement.gateway import LiveStripeGateway, StripeGateway
from src.services.settlement.ledger import defer_installment, mark_installment, mark_installment_failed
from src.services.settlement.store import (
	EvidencePacketPublishError,
	EvidencePacketStore,
	get_evidence_packet_store,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChargeOutcome:
	status: str  # 'CHARGED' | 'SETTLING' | 'UNCERTAIN' | 'FAILED' | 'FAILED_PERMANENT' | 'BLOCKED'
	rail: Optional[str] = None
	stripe_invoice_id: Optional[str] = None
	reason: Optional[str] = None


def _is_stripe_test_mode() -> bool:
	from config.settings import get_settings

	settings = get_settings()
	key = settings.stripe_secret_key
	if not key:
		return False
	return key.get_secret_value().startswith("sk_test_")


def _load_row(session: Session, transaction_id: int):
	return session.execute(
		text(
			"SELECT t.transaction_id, t.client_id, t.company_id, t.opportunity_id, t.door_count, "
			"       t.installment_1_cents, t.installment_2_cents, t.inst1_attempts, t.inst2_attempts, "
			"       t.inst1_reopen_count, t.inst2_reopen_count, "
			"       t.inst1_stripe_invoice_id, t.inst2_stripe_invoice_id, "
			"       t.evidence_packet_url, t.door_signed_at, "
			"       a.pms_agreement_id, a.agreement_source, a.status AS agreement_status, "
			"       c.stripe_customer_id, c.ach_payment_method_id_encrypted, c.card_payment_method_id_encrypted "
			"FROM settlement_transactions t "
			"JOIN pms_agreements a ON a.pms_agreement_id = t.pms_agreement_id "
			"LEFT JOIN companies c ON c.company_id = t.company_id "
			"WHERE t.transaction_id = :transaction_id"
		),
		{"transaction_id": transaction_id},
	).first()


def _publish_evidence_packet(
	session: Session, row, installment: int, store: EvidencePacketStore
) -> Optional[str]:
	"""Compiles + publishes the packet, storing sha256/bytes/url on the row
	BEFORE any Stripe object is created. Returns the URL, or None if the
	store simply isn't configured (StubEvidencePacketStore).

	A genuine transport/API failure (EvidencePacketPublishError) is recorded
	on the row the same as a None, then RE-RAISED — the caller
	(charge_installment) needs the actual Stripe error message to put in
	instN_last_error, and defer_installment() (not this function) decides the
	retry schedule. Swallowing it into a bare None here (the pre-fix
	behavior) is what made every real Stripe Files failure indistinguishable
	from "no store configured", and silently permanent (PR #30 review
	finding 1)."""
	packet = assemble_evidence_packet(session, transaction_id=row.transaction_id, installment=installment)
	pdf_bytes = compile_packet(packet)
	sha256 = hashlib.sha256(pdf_bytes).hexdigest()

	publish_exc: Optional[EvidencePacketPublishError] = None
	try:
		url = store.publish(transaction_id=row.transaction_id, installment=installment, pdf_bytes=pdf_bytes)
	except EvidencePacketPublishError as exc:
		logger.exception(
			"settlement.charge: evidence packet publish failed (transaction=%s installment=%s): %s",
			row.transaction_id, installment, exc,
		)
		url = None
		publish_exc = exc

	session.execute(
		text(
			"UPDATE settlement_transactions SET "
			"  evidence_packet_sha256 = :sha256, evidence_packet_bytes = :nbytes, "
			"  evidence_packet_url = COALESCE(:url, evidence_packet_url), "
			"  evidence_packet_status = :status, updated_at = NOW() "
			"WHERE transaction_id = :transaction_id"
		),
		{
			"sha256": sha256, "nbytes": len(pdf_bytes),
			"url": url, "status": "PUBLISHED" if url else "BLOCKED",
			"transaction_id": row.transaction_id,
		},
	)
	log_event(
		row.client_id, "evidence_packet_compiled", entity_type="settlement_transaction",
		entity_id=str(row.transaction_id),
		payload={
			"transaction_id": row.transaction_id, "sha256": sha256, "bytes": len(pdf_bytes),
			"sections_with_gaps": packet.sections_with_gaps,
		},
		session=session,
	)
	if publish_exc is not None:
		raise publish_exc
	return url


def charge_installment(
	session: Session,
	transaction_id: int,
	installment: int,
	*,
	as_of: datetime,
	gateway: Optional[StripeGateway] = None,
	store: Optional[EvidencePacketStore] = None,
) -> ChargeOutcome:
	row = _load_row(session, transaction_id)
	if row is None:
		return ChargeOutcome(status="FAILED_PERMANENT", reason="no such settlement_transaction")

	attempts = row.inst1_attempts if installment == 1 else row.inst2_attempts
	reopen_count = row.inst1_reopen_count if installment == 1 else row.inst2_reopen_count

	# ── Preflight refusal: synthetic agreement outside Stripe test mode ──
	allow_synthetic = row.agreement_source == "PMS_SYNC" or _is_stripe_test_mode()
	if not allow_synthetic:
		# Structural, not transient — blocked_reason is intentionally set to a
		# value NOT in ledger._RETRYABLE_BLOCKED_REASONS, so this row is never
		# reclaimed no matter how long it sits (see that constant's comment).
		mark_installment(
			session, transaction_id, installment, "BLOCKED",
			error="SYNTHETIC_AGREEMENT_IN_LIVE_MODE", blocked_reason="SYNTHETIC_AGREEMENT_IN_LIVE_MODE",
		)
		return ChargeOutcome(status="BLOCKED", reason="SYNTHETIC_AGREEMENT_IN_LIVE_MODE")
	if row.agreement_source != "PMS_SYNC":
		session.execute(
			text("UPDATE settlement_transactions SET allow_synthetic_charge = TRUE WHERE transaction_id = :tid"),
			{"tid": transaction_id},
		)

	# ── Preflight refusal: evidence packet must be published first ──
	# BLOCKED here is TRANSIENT (a Stripe Files outage, or no store configured
	# yet) — defer_installment() keeps the row reclaimable with backoff rather
	# than the old terminal mark_installment(), which silently forfeited every
	# charge behind SETTLEMENT_EVIDENCE_PACKET_STORE=stripe_files forever
	# (PR #30 review finding 1).
	url = row.evidence_packet_url
	publish_error = None
	if url is None:
		try:
			url = _publish_evidence_packet(session, row, installment, store or get_evidence_packet_store())
		except EvidencePacketPublishError as exc:
			publish_error = str(exc)
	if url is None:
		reason = publish_error or "no evidence packet store configured"
		crossed_alert_threshold = defer_installment(
			session, transaction_id, installment,
			reason="EVIDENCE_PACKET_UNPUBLISHED", error=reason, as_of=as_of, attempts=attempts,
		)
		if crossed_alert_threshold:
			log_event(
				row.client_id, "settlement_charge_failed", entity_type="settlement_transaction",
				entity_id=str(transaction_id),
				payload={
					"transaction_id": transaction_id, "installment": installment,
					"error_code": "EVIDENCE_PACKET_UNPUBLISHED", "error_message": reason,
					"attempts": attempts,
				},
				session=session,
			)
		return ChargeOutcome(status="BLOCKED", reason="EVIDENCE_PACKET_UNPUBLISHED")

	if not row.stripe_customer_id:
		mark_installment_failed(session, transaction_id, installment, attempts, "no stripe_customer_id on company")
		return ChargeOutcome(status="FAILED", reason="no stripe_customer_id")

	ach_pm = decrypt_token(row.ach_payment_method_id_encrypted) if row.ach_payment_method_id_encrypted else None
	card_pm = decrypt_token(row.card_payment_method_id_encrypted) if row.card_payment_method_id_encrypted else None
	if not ach_pm and not card_pm:
		mark_installment_failed(session, transaction_id, installment, attempts, "no payment method on file")
		return ChargeOutcome(status="FAILED", reason="no payment method on file")

	gw = gateway or LiveStripeGateway()
	amount_cents = row.installment_1_cents if installment == 1 else row.installment_2_cents
	key_prefix = f"settlement|{transaction_id}|{installment}"
	existing_invoice_id = row.inst1_stripe_invoice_id if installment == 1 else row.inst2_stripe_invoice_id

	if existing_invoice_id:
		# PR #37 review finding #8-adjacent recovery: this installment already
		# has a Stripe invoice from an earlier attempt (persisted immediately
		# below the moment create_invoice() first succeeded). Reuse it rather
		# than calling create_invoice again — the OBJECT-creation idempotency
		# keys below would return the same invoice anyway within Stripe's
		# ~24h key retention, but recording and reusing our own id makes that
		# protection not depend on that window.
		stripe_invoice_id = existing_invoice_id
		logger.info(
			"settlement.charge: transaction=%s installment=%s resuming existing invoice=%s",
			transaction_id, installment, stripe_invoice_id,
		)
	else:
		metadata = {
			"client_id": row.client_id, "transaction_id": str(transaction_id),
			"installment": str(installment), "opportunity_id": str(row.opportunity_id),
			"pms_agreement_id": str(row.pms_agreement_id),
		}
		# Reconciliation, not creation: a prior attempt may have had Stripe
		# accept create_invoice and then this process crashed (SIGKILL, not a
		# raised exception) before the savepoint below ever committed to disk
		# — the one gap neither the savepoint nor Stripe's own idempotency-key
		# retention covers (PR #37 second review finding). find_invoice_by_metadata
		# lists this customer's invoices and matches on the SAME
		# (transaction_id, installment) pair stamped into every invoice's own
		# metadata, so a genuinely already-created invoice is found and reused
		# here instead of a second one being created.
		existing = gw.find_invoice_by_metadata(
			stripe_customer_id=row.stripe_customer_id,
			metadata_filter={"transaction_id": str(transaction_id), "installment": str(installment)},
		)
		if existing is not None:
			stripe_invoice_id = existing.stripe_invoice_id
			logger.info(
				"settlement.charge: transaction=%s installment=%s reconciled existing invoice=%s via metadata scan",
				transaction_id, installment, stripe_invoice_id,
			)
		else:
			invoice = gw.create_invoice(
				stripe_customer_id=row.stripe_customer_id,
				default_payment_method_id=ach_pm or card_pm,
				metadata=metadata,
				idempotency_key=f"settlement-invoice|{key_prefix}",
			)
			stripe_invoice_id = invoice.stripe_invoice_id
		# Persisted in its own savepoint immediately after create_invoice
		# succeeds — BEFORE add_invoice_item/finalize below — mirroring
		# src/services/billing/sit_invoice.py's identical checkpoint (code
		# review finding: this used to sit AFTER finalize_invoice, which left
		# NO checkpoint at all if add_invoice_item/finalize itself raised). A
		# LATER exception in this same call now rolls back only the later
		# work, not this write, so the next claim/retry sees the invoice
		# already recorded and reuses it via the `existing_invoice_id` branch
		# above instead of risking a second Stripe invoice past Stripe's own
		# ~24h idempotency-key retention window.
		prefix = "inst1" if installment == 1 else "inst2"
		with session.begin_nested():
			session.execute(
				text(f"UPDATE settlement_transactions SET {prefix}_stripe_invoice_id = :invoice_id WHERE transaction_id = :tid"),
				{"invoice_id": stripe_invoice_id, "tid": transaction_id},
			)

	# add_invoice_item/finalize run on EVERY entry (resume or fresh), never
	# only on the fresh-create path — their idempotency keys are pinned to
	# (transaction_id, installment) alone, so re-running them against an
	# already-itemized/already-finalized invoice is a safe no-op (Stripe
	# returns the cached object/result), while a resumed invoice that never
	# got this far the first time actually gets finished here instead of
	# jumping straight to an unfinalized pay attempt.
	gw.add_invoice_item(
		stripe_invoice_id=stripe_invoice_id,
		stripe_customer_id=row.stripe_customer_id,
		amount_cents=amount_cents,
		description=f"Blackink verified door signed — installment {installment} of 2 ({row.door_count} door(s))",
		idempotency_key=f"settlement-item|{key_prefix}",
	)
	gw.update_invoice_metadata(
		stripe_invoice_id=stripe_invoice_id,
		metadata={"evidence_packet_url": url},
	)
	gw.finalize_invoice(stripe_invoice_id=stripe_invoice_id, idempotency_key=f"settlement-finalize|{key_prefix}")

	# PR #37 review finding #2: the object-creation keys above stay pinned to
	# (transaction_id, installment) alone — that is what guarantees ONE
	# invoice/item/finalize per installment no matter how many times this
	# function is entered. The PAYMENT-ATTEMPT keys below instead carry the
	# already-incremented, DB-persisted `attempts` counter (set by
	# claim_installment_1/2's own claim UPDATE, never a timestamp), so retry
	# N always makes a genuinely new pay_invoice request to Stripe — a client
	# who fixes their payment method between attempts is actually retried,
	# instead of Stripe returning the ORIGINAL cached decline for a repeated
	# identical key.
	#
	# `reopen_count` is folded in too (PR #37 second review): reopen_failed_permanent_installment()
	# resets `attempts` back to 0, so without this an operator-triggered retry
	# after reopen would rebuild the EXACT SAME "|a1" key as the original
	# first attempt — inside Stripe's ~24h idempotency-key retention window,
	# that replays the cached ORIGINAL decline instead of making a real charge
	# attempt against the client's now-fixed payment method.
	pay_key_suffix = f"|r{reopen_count}a{attempts}"

	# Always pass the CURRENTLY-ON-FILE payment method explicitly, never rely
	# on the invoice's own default_payment_method — that was frozen at
	# create_invoice() time (or at whatever earlier attempt actually created
	# this invoice) and does not reflect a payment method the client fixed
	# after a decline (PR #37 second review finding #1). ach_pm/card_pm above
	# are re-read from `companies` fresh on every call.
	rail = "ACH" if ach_pm else "CARD"
	outcome = gw.pay_invoice(
		stripe_invoice_id=stripe_invoice_id,
		payment_method_id=ach_pm or card_pm,
		idempotency_key=f"settlement-pay-ach|{key_prefix}{pay_key_suffix}",
	)

	if outcome.status == "paid":
		mark_installment(
			session, transaction_id, installment, "CHARGED",
			charged_at=as_of, stripe_invoice_id=stripe_invoice_id, rail=rail,
		)
		log_event(
			row.client_id, f"settlement_installment_{installment}_charged",
			entity_type="settlement_transaction", entity_id=str(transaction_id),
			payload={
				"transaction_id": transaction_id, "amount_cents": amount_cents,
				"rail": rail, "stripe_invoice_id": stripe_invoice_id,
			},
			session=session,
		)
		return ChargeOutcome(status="CHARGED", rail=rail, stripe_invoice_id=stripe_invoice_id)

	if outcome.is_transport_error:
		# Indeterminate — NEVER attempt the card fallback on an indeterminate
		# ACH result, and never mark FAILED (that would allow a retry to
		# double-charge if the first attempt actually succeeded at Stripe).
		mark_installment(session, transaction_id, installment, "UNCERTAIN", error=outcome.error_message, stripe_invoice_id=stripe_invoice_id)
		return ChargeOutcome(status="UNCERTAIN", reason=outcome.error_message)

	if outcome.status == "processing":
		mark_installment(session, transaction_id, installment, "SETTLING", stripe_invoice_id=stripe_invoice_id)
		return ChargeOutcome(status="SETTLING", stripe_invoice_id=stripe_invoice_id)

	# A definite ACH decline: attempt the card fallback, only here.
	#
	# Re-review verification note: a real Stripe ACH payment never reaches
	# this branch — Stripe delivers every ACH failure asynchronously via the
	# invoice.payment_failed webhook (stripe_webhook_router.py's
	# _handle_settlement_event -> mark_installment_failed), never as a
	# synchronous decline from pay_invoice() (confirmed against a real
	# Stripe test-mode ACH charge; see gateway.py's pay_invoice for the
	# fix and the fuller explanation of what a pre-fix "open, no exception"
	# result actually meant). This branch is DEFENSE-IN-DEPTH for a
	# PayOutcome a test or a future gateway change could still construct
	# directly (outcome.status not in {"paid","processing"} with no
	# exception) — kept because ChargeOutcome/PayOutcome are a real
	# interface other StripeGateway implementations could satisfy
	# differently, not because live Stripe is expected to hit it today.
	if card_pm and ach_pm:
		card_outcome = gw.pay_invoice(
			stripe_invoice_id=stripe_invoice_id, payment_method_id=card_pm,
			idempotency_key=f"settlement-pay-card|{key_prefix}{pay_key_suffix}",
		)
		if card_outcome.status == "paid":
			mark_installment(
				session, transaction_id, installment, "CHARGED",
				charged_at=as_of, stripe_invoice_id=stripe_invoice_id, rail="CARD",
			)
			log_event(
				row.client_id, f"settlement_installment_{installment}_charged",
				entity_type="settlement_transaction", entity_id=str(transaction_id),
				payload={
					"transaction_id": transaction_id, "amount_cents": amount_cents,
					"rail": "CARD", "stripe_invoice_id": stripe_invoice_id,
				},
				session=session,
			)
			return ChargeOutcome(status="CHARGED", rail="CARD", stripe_invoice_id=stripe_invoice_id)
		if card_outcome.is_transport_error:
			mark_installment(session, transaction_id, installment, "UNCERTAIN", error=card_outcome.error_message, stripe_invoice_id=stripe_invoice_id)
			return ChargeOutcome(status="UNCERTAIN", reason=card_outcome.error_message)
		outcome = card_outcome

	error = f"{outcome.error_code}: {outcome.error_message}"
	log_event(
		row.client_id, "settlement_charge_failed", entity_type="settlement_transaction",
		entity_id=str(transaction_id),
		payload={
			"transaction_id": transaction_id, "installment": installment,
			"error_code": outcome.error_code, "error_message": outcome.error_message,
		},
		session=session,
	)
	became_failed_permanent = mark_installment_failed(
		session, transaction_id, installment, attempts, error, stripe_invoice_id=stripe_invoice_id,
	)
	if became_failed_permanent:
		# PR #37 review finding #2: exactly one incident per terminal
		# failure, supporting a human deciding to call
		# ledger.reopen_failed_permanent_installment() (wired to
		# POST /api/v1/settlement/installments/reopen) once the client's
		# payment method is fixed.
		log_event(
			row.client_id, "settlement_charge_failed_permanent", entity_type="settlement_transaction",
			entity_id=str(transaction_id),
			payload={
				"transaction_id": transaction_id, "installment": installment,
				"amount_cents": amount_cents, "attempts": attempts + 1,
				"error_code": outcome.error_code, "error_message": outcome.error_message,
				"stripe_invoice_id": stripe_invoice_id,
			},
			session=session,
		)
	return ChargeOutcome(status="FAILED", reason=error)
