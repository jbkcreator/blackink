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
from src.services.settlement.gateway import LiveStripeGateway, StripeGateway
from src.services.settlement.ledger import (
	claim_terminated_installment_2_for_void,
	defer_installment,
	mark_installment,
)
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
			"       t.inst2_attempts, "
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
	# "still active" (would charge) or "terminated" (would void). This
	# BLOCKED is TRANSIENT: defer_installment() keeps the row reclaimable
	# with backoff instead of the old terminal mark_installment(), which
	# permanently forfeited installment 2 the moment a PMS outage (or, with
	# the wired StubPmsProvider, EVERY row) hit this branch (PR #30 review
	# finding 2 — claim_installment_2() never re-selected BLOCKED rows).
	crossed_alert_threshold = defer_installment(
		session, transaction_id, 2,
		reason="PMS_VERIFICATION_UNAVAILABLE", error="PMS verification unavailable",
		as_of=as_of, attempts=row.inst2_attempts,
	)
	if crossed_alert_threshold:
		log_event(
			row.client_id, "settlement_charge_failed", entity_type="settlement_transaction",
			entity_id=str(transaction_id),
			payload={
				"transaction_id": transaction_id, "installment": 2,
				"error_code": "PMS_VERIFICATION_UNAVAILABLE", "error_message": "PMS verification unavailable",
				"attempts": row.inst2_attempts,
			},
			session=session,
		)
	return "BLOCKED"


def void_terminated_installment_2_batch(session: Session, *, limit: int = 20, gateway: Optional[StripeGateway] = None) -> int:
	"""PR #37 review finding #1: the production path that actually reaches
	VOIDED_CLAWBACK for a transaction whose agreement terminated inside the
	clawback window — claim_installment_2() now deliberately EXCLUDES these
	rows from its own claim (entering CHARGING for one would make the guard
	trigger raise and abort that entire bulk UPDATE), so this is the only
	route that still voids them. Must run BEFORE claim_installment_2() in
	the sweep so a terminated row is drained out first, not left to rely
	solely on claim_installment_2's own exclusion predicate as a race
	backstop.

	Transitions SCHEDULED/FAILED/BLOCKED directly to VOIDED_CLAWBACK — the
	guard trigger only restricts entry into CHARGING/SETTLING/CHARGED, so
	this transition needs no claim/CHARGING step at all. Voids whatever
	Stripe invoice already exists for the row first (mirrors
	process_installment_2's own VOID_CLAWBACK branch above) — never pays
	it.

	Each row is isolated by its own try/except (code review finding): this
	loop previously had none, so a single Stripe error voiding one row's
	invoice raised out of this function and — since the sweep doesn't wrap
	this call either — aborted the ENTIRE installment-2 tick before
	claim_installment_2 even ran, reintroducing the exact "one bad row
	aborts the batch" failure finding #1 exists to eliminate, just via this
	new path."""
	rows = claim_terminated_installment_2_for_void(session, limit=limit)
	voided = 0
	for row in rows:
		try:
			_void_one_terminated_installment_2(session, row, gateway=gateway)
		except Exception:  # noqa: BLE001 - one bad row must not discard the rest of the batch
			logger.exception(
				"settlement.clawback: failed to void terminated transaction=%s", row["transaction_id"],
			)
			continue
		voided += 1
	logger.info("settlement.clawback: voided %d terminated installment(s) via the mixed-batch-safe path", voided)
	return voided


def _void_one_terminated_installment_2(session: Session, row: dict, *, gateway: Optional[StripeGateway]) -> None:
	with session.begin_nested():
		if row["inst2_stripe_invoice_id"]:
			# Lazy construction — most terminated rows never reached CHARGING
			# at all (no invoice yet), so most sweep ticks never need a real
			# Stripe client (and never fail on an unconfigured one, same
			# posture as LiveStripeGateway()'s other lazy call sites in this
			# domain).
			gw = gateway or LiveStripeGateway()
			gw.void_or_delete_invoice(stripe_invoice_id=row["inst2_stripe_invoice_id"], invoice_status="open")
		mark_installment(session, row["transaction_id"], 2, "VOIDED_CLAWBACK")
		door_signed_at = row.get("door_signed_at")
		terminated_at = row["terminated_at"]
		days_since_signed = (terminated_at - door_signed_at).days if (terminated_at and door_signed_at) else None
		log_event(
			row["client_id"], "settlement_clawback_executed", entity_type="settlement_transaction",
			entity_id=str(row["transaction_id"]),
			payload={
				"transaction_id": row["transaction_id"],
				"installment_2_cents": row["installment_2_cents"],
				"terminated_at": terminated_at.isoformat() if terminated_at else None,
				"days_since_signed": days_since_signed,
			},
			session=session,
		)
