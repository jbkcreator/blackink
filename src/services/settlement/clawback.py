"""Day-60 clawback decision + execution (Subtask 1.2.2).

process_installment_2() re-verifies via a PmsProvider, applies the pure
decide_installment_2() rule, and either charges, voids-and-logs, or defers.
A provider None is never read as a definite answer and is never cached —
same tri-state discipline as src/services/compliance_gate.py.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.events import log_event
from src.services.pms_sync import PmsProvider, StubPmsProvider
from src.services.settlement.charge import ChargeOutcome, charge_installment
from src.services.settlement.gateway import StripeGateway
from src.services.settlement.ledger import mark_installment
from src.services.settlement.split import CHARGE, DEFER_UNVERIFIED, VOID_CLAWBACK, decide_installment_2
from src.services.settlement.store import EvidencePacketStore

logger = logging.getLogger(__name__)


def process_installment_2(
	session: Session,
	transaction_id: int,
	*,
	as_of: datetime,
	pms: Optional[PmsProvider] = None,
	gateway: Optional[StripeGateway] = None,
	store: Optional[EvidencePacketStore] = None,
) -> str:
	pms = pms or StubPmsProvider()

	row = session.execute(
		text(
			"SELECT t.transaction_id, t.client_id, t.installment_2_cents, t.inst2_stripe_invoice_id, "
			"       a.pms_agreement_id, a.pms_property_ref, a.door_signed_at, a.terminated_at, "
			"       o.clawback_window_days "
			"FROM settlement_transactions t "
			"JOIN pms_agreements a ON a.pms_agreement_id = t.pms_agreement_id "
			"JOIN settlement_offer_config o ON o.offer_code = t.offer_code "
			"WHERE t.transaction_id = :transaction_id"
		),
		{"transaction_id": transaction_id},
	).one()

	still_active = pms.is_agreement_active(row.pms_property_ref or str(row.pms_agreement_id))

	decision = decide_installment_2(
		door_signed_at=row.door_signed_at,
		clawback_window_days=row.clawback_window_days,
		terminated_at=row.terminated_at,
		agreement_still_active=still_active,
		as_of=as_of,
	)

	if decision == CHARGE:
		outcome: ChargeOutcome = charge_installment(
			session, transaction_id, 2, as_of=as_of, gateway=gateway, store=store
		)
		return outcome.status

	if decision == VOID_CLAWBACK:
		if row.inst2_stripe_invoice_id and gateway is not None:
			# Void whatever Stripe object exists — never pay it.
			gateway.void_or_delete_invoice(stripe_invoice_id=row.inst2_stripe_invoice_id, invoice_status="open")
		mark_installment(session, transaction_id, 2, "VOIDED_CLAWBACK")
		days_since_signed = (row.terminated_at - row.door_signed_at).days if row.terminated_at else None
		log_event(
			row.client_id, "settlement_clawback_executed", entity_type="settlement_transaction",
			entity_id=str(transaction_id),
			payload={
				"transaction_id": transaction_id,
				"installment_2_cents": row.installment_2_cents,
				"terminated_at": row.terminated_at.isoformat() if row.terminated_at else None,
				"days_since_signed": days_since_signed,
			},
			session=session,
		)
		return "VOIDED_CLAWBACK"

	# DEFER_UNVERIFIED — a None from the provider must never be read as
	# "still active" (would charge) or "terminated" (would void).
	mark_installment(session, transaction_id, 2, "BLOCKED", error="PMS verification unavailable")
	return "BLOCKED"
