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
    first. If store.publish() returns None or raises, nothing is invoiced.
  - SYNTHETIC_AGREEMENT_IN_LIVE_MODE — a non-PMS_SYNC agreement (i.e. the
    DoD's synthetic door_signed test path) may only back a charge when the
    configured Stripe secret key is a sk_test_ key. In live mode this
    refuses outright, so a synthetic agreement is structurally unable to
    bill a real client in production.
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
from src.services.settlement.ledger import mark_installment, mark_installment_failed
from src.services.settlement.store import EvidencePacketStore, get_evidence_packet_store

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
	BEFORE any Stripe object is created. Returns the URL, or None if
	publishing failed/was refused."""
	packet = assemble_evidence_packet(session, transaction_id=row.transaction_id, installment=installment)
	pdf_bytes = compile_packet(packet)
	sha256 = hashlib.sha256(pdf_bytes).hexdigest()

	try:
		url = store.publish(transaction_id=row.transaction_id, installment=installment, pdf_bytes=pdf_bytes)
	except Exception:
		logger.exception(
			"settlement.charge: evidence packet publish raised (transaction=%s installment=%s)",
			row.transaction_id, installment,
		)
		url = None

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

	# ── Preflight refusal: synthetic agreement outside Stripe test mode ──
	allow_synthetic = row.agreement_source == "PMS_SYNC" or _is_stripe_test_mode()
	if not allow_synthetic:
		mark_installment(session, transaction_id, installment, "BLOCKED", error="SYNTHETIC_AGREEMENT_IN_LIVE_MODE")
		return ChargeOutcome(status="BLOCKED", reason="SYNTHETIC_AGREEMENT_IN_LIVE_MODE")
	if row.agreement_source != "PMS_SYNC":
		session.execute(
			text("UPDATE settlement_transactions SET allow_synthetic_charge = TRUE WHERE transaction_id = :tid"),
			{"tid": transaction_id},
		)

	# ── Preflight refusal: evidence packet must be published first ──
	url = row.evidence_packet_url
	if url is None:
		url = _publish_evidence_packet(session, row, installment, store or get_evidence_packet_store())
	if url is None:
		mark_installment(session, transaction_id, installment, "BLOCKED", error="EVIDENCE_PACKET_UNPUBLISHED")
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

	invoice = gw.create_invoice(
		stripe_customer_id=row.stripe_customer_id,
		default_payment_method_id=ach_pm or card_pm,
		metadata={
			"client_id": row.client_id, "transaction_id": str(transaction_id),
			"installment": str(installment), "opportunity_id": str(row.opportunity_id),
			"pms_agreement_id": str(row.pms_agreement_id),
		},
		idempotency_key=f"settlement-invoice|{key_prefix}",
	)
	gw.add_invoice_item(
		stripe_invoice_id=invoice.stripe_invoice_id,
		stripe_customer_id=row.stripe_customer_id,
		amount_cents=amount_cents,
		description=f"Blackink verified door signed — installment {installment} of 2 ({row.door_count} door(s))",
		idempotency_key=f"settlement-item|{key_prefix}",
	)
	gw.update_invoice_metadata(
		stripe_invoice_id=invoice.stripe_invoice_id,
		metadata={"evidence_packet_url": url},
	)
	invoice = gw.finalize_invoice(
		stripe_invoice_id=invoice.stripe_invoice_id, idempotency_key=f"settlement-finalize|{key_prefix}"
	)

	rail = "ACH" if ach_pm else "CARD"
	outcome = gw.pay_invoice(
		stripe_invoice_id=invoice.stripe_invoice_id,
		payment_method_id=None,  # uses the invoice's default_payment_method (ACH)
		idempotency_key=f"settlement-pay-ach|{key_prefix}",
	)

	if outcome.status == "paid":
		mark_installment(
			session, transaction_id, installment, "CHARGED",
			charged_at=as_of, stripe_invoice_id=invoice.stripe_invoice_id, rail=rail,
		)
		log_event(
			row.client_id, f"settlement_installment_{installment}_charged",
			entity_type="settlement_transaction", entity_id=str(transaction_id),
			payload={
				"transaction_id": transaction_id, "amount_cents": amount_cents,
				"rail": rail, "stripe_invoice_id": invoice.stripe_invoice_id,
			},
			session=session,
		)
		return ChargeOutcome(status="CHARGED", rail=rail, stripe_invoice_id=invoice.stripe_invoice_id)

	if outcome.is_transport_error:
		# Indeterminate — NEVER attempt the card fallback on an indeterminate
		# ACH result, and never mark FAILED (that would allow a retry to
		# double-charge if the first attempt actually succeeded at Stripe).
		mark_installment(session, transaction_id, installment, "UNCERTAIN", error=outcome.error_message)
		return ChargeOutcome(status="UNCERTAIN", reason=outcome.error_message)

	if outcome.status == "processing":
		mark_installment(session, transaction_id, installment, "SETTLING", stripe_invoice_id=invoice.stripe_invoice_id)
		return ChargeOutcome(status="SETTLING", stripe_invoice_id=invoice.stripe_invoice_id)

	# A definite ACH decline: attempt the card fallback, only here.
	if card_pm and ach_pm:
		card_outcome = gw.pay_invoice(
			stripe_invoice_id=invoice.stripe_invoice_id, payment_method_id=card_pm,
			idempotency_key=f"settlement-pay-card|{key_prefix}",
		)
		if card_outcome.status == "paid":
			mark_installment(
				session, transaction_id, installment, "CHARGED",
				charged_at=as_of, stripe_invoice_id=invoice.stripe_invoice_id, rail="CARD",
			)
			log_event(
				row.client_id, f"settlement_installment_{installment}_charged",
				entity_type="settlement_transaction", entity_id=str(transaction_id),
				payload={
					"transaction_id": transaction_id, "amount_cents": amount_cents,
					"rail": "CARD", "stripe_invoice_id": invoice.stripe_invoice_id,
				},
				session=session,
			)
			return ChargeOutcome(status="CHARGED", rail="CARD", stripe_invoice_id=invoice.stripe_invoice_id)
		if card_outcome.is_transport_error:
			mark_installment(session, transaction_id, installment, "UNCERTAIN", error=card_outcome.error_message)
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
	mark_installment_failed(session, transaction_id, installment, attempts, error)
	return ChargeOutcome(status="FAILED", reason=error)
