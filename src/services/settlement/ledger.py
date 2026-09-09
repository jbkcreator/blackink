"""Settlement ledger — DB writes only, no Stripe (Subtask 1.2.2).

src/services/settlement/charge.py is the only module that calls a
money-moving Stripe API; everything here writes pms_agreements /
settlement_transactions rows and follows the repo's established
SKIP LOCKED claim pattern (src/services/no_show_recovery_dispatch.py,
src/tasks/self_serve_audit_worker.py).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.services.events import log_event
from src.services.settlement.split import OfferTerms

logger = logging.getLogger(__name__)

_CLAIM_LEASE_MINUTES = 10
_MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT = 3

# BLOCKED reasons the claim queries will re-select once inst{N}_next_retry_at
# has passed — transient causes only (PR #30 review findings 1 & 2).
# SYNTHETIC_AGREEMENT_IN_LIVE_MODE is deliberately NOT here: it is a
# structural refusal, not a transient one, and trg_settlement_guard_transition
# (migrations/apply_settlement_ledger.py) rejects the CHARGING transition for
# that case outright — reclaiming it would make the claim UPDATE itself raise
# and take the sweep's per-row savepoint down with it.
_RETRYABLE_BLOCKED_REASONS = ("EVIDENCE_PACKET_UNPUBLISHED", "PMS_VERIFICATION_UNAVAILABLE")

# Deferred-BLOCKED backoff cap (minutes) — a day-60 PMS outage or a Stripe
# Files outage should retry with growing spacing, never instantly and never
# unboundedly far out.
_MAX_BLOCKED_BACKOFF_MINUTES = 360

# Emit exactly one settlement_charge_failed alert once a retryable-BLOCKED
# installment has failed this many consecutive attempts — visible in `events`
# without spamming one per sweep tick (PR #30 review: "five failures generate
# one alert without stopping retries").
_ALERT_AFTER_ATTEMPTS = 5


def load_offer_terms(session: Session, offer_code: str) -> Optional[OfferTerms]:
	row = session.execute(
		text(
			"SELECT offer_code, settlement_enabled, pricing_basis, per_door_bounty_cents, "
			"       flat_bounty_cents, installment_1_bps, clawback_window_days "
			"FROM settlement_offer_config WHERE offer_code = :offer_code"
		),
		{"offer_code": offer_code},
	).first()
	if row is None:
		return None
	return OfferTerms(
		offer_code=row.offer_code,
		settlement_enabled=row.settlement_enabled,
		pricing_basis=row.pricing_basis,
		per_door_bounty_cents=row.per_door_bounty_cents,
		flat_bounty_cents=row.flat_bounty_cents,
		installment_1_bps=row.installment_1_bps,
		clawback_window_days=row.clawback_window_days,
	)


def record_door_signed(
	session: Session,
	*,
	client_id: str,
	opportunity_id: str,
	door_signed_at: datetime,
	agreement_source: str,
	company_id: Optional[str] = None,
	owner_contact_id: Optional[int] = None,
	pms_property_ref: Optional[str] = None,
	door_count: int = 1,
	owner_domain: Optional[str] = None,
	owner_email: Optional[str] = None,
) -> Optional[int]:
	"""Write the pms_agreements row and, in the SAME transaction, upsert the
	owner claim into client_pm_books — a signed agreement genuinely
	establishes a permanent non-poach claim (see the migration's docstring
	for why this is NOT done by adding columns to client_pm_books itself).

	Returns the new pms_agreement_id, or None if this exact agreement
	(client_id, opportunity_id, door_signed_at) was already recorded — a
	duplicate ingest is a logged no-op, not an error the caller must
	handle.
	"""
	try:
		with session.begin_nested():
			row = session.execute(
				text(
					"INSERT INTO pms_agreements "
					"(client_id, company_id, owner_contact_id, opportunity_id, pms_property_ref, "
					" door_count, agreement_source, door_signed_at) "
					"VALUES (:client_id, :company_id, :owner_contact_id, :opportunity_id, :pms_property_ref, "
					"        :door_count, :agreement_source, :door_signed_at) "
					"RETURNING pms_agreement_id"
				),
				{
					"client_id": client_id,
					"company_id": company_id,
					"owner_contact_id": owner_contact_id,
					"opportunity_id": opportunity_id,
					"pms_property_ref": pms_property_ref,
					"door_count": door_count,
					"agreement_source": agreement_source,
					"door_signed_at": door_signed_at,
				},
			).one()
	except IntegrityError:
		# SAVEPOINT rollback (via begin_nested()), NOT session.rollback() — a
		# bare session.rollback() here would discard the caller's ENTIRE
		# transaction, including any prior successful work in the same
		# session (a real bug caught by
		# tests/test_settlement_live.py::test_exactly_once_billing_across_multiple_appointments_sharing_opportunity
		# during live-DB verification).
		logger.info(
			"settlement.record_door_signed: duplicate agreement (client=%s opportunity=%s door_signed_at=%s) — no-op",
			client_id, opportunity_id, door_signed_at,
		)
		return None

	if owner_domain or owner_email:
		exists = session.execute(
			text(
				"SELECT 1 FROM client_pm_books WHERE client_id = :client_id "
				"AND ((owner_domain IS NOT NULL AND owner_domain = :owner_domain) "
				"     OR (owner_email IS NOT NULL AND owner_email = :owner_email))"
			),
			{"client_id": client_id, "owner_domain": owner_domain, "owner_email": owner_email},
		).first()
		if not exists:
			session.execute(
				text(
					"INSERT INTO client_pm_books (client_id, owner_domain, owner_email) "
					"VALUES (:client_id, :owner_domain, :owner_email)"
				),
				{"client_id": client_id, "owner_domain": owner_domain, "owner_email": owner_email},
			)

	log_event(
		client_id, "door_signed", entity_type="pms_agreement", entity_id=str(row.pms_agreement_id),
		payload={
			"pms_agreement_id": row.pms_agreement_id, "opportunity_id": opportunity_id,
			"door_count": door_count, "agreement_source": agreement_source,
			"door_signed_at": door_signed_at.isoformat(),
		},
		session=session,
	)
	return row.pms_agreement_id


def record_agreement_terminated(
	session: Session, *, client_id: str, pms_agreement_id: int, terminated_at: datetime
) -> None:
	"""Writes ONLY pms_agreements.terminated_at/status — the client_pm_books
	claim row from record_door_signed() is left completely untouched, so
	is_claimed_by_other_client() semantics do not change (see the migration
	docstring). If a settlement row's installment 2 is still eligible for
	the clawback window, this flips it to VOIDED_CLAWBACK immediately —
	the sweep is the backstop for a missed termination event, not the
	mechanism."""
	session.execute(
		text(
			"UPDATE pms_agreements SET status = 'TERMINATED', terminated_at = :terminated_at, "
			"updated_at = NOW() WHERE client_id = :client_id AND pms_agreement_id = :pms_agreement_id"
		),
		{"client_id": client_id, "pms_agreement_id": pms_agreement_id, "terminated_at": terminated_at},
	)


def open_settlement(
	session: Session, *, client_id: str, pms_agreement_id: int, offer_code: str
) -> Optional[int]:
	"""Reads the agreement + offer terms, computes the split, and INSERTs
	the settlement_transactions row. Returns None (and logs) on a
	uq_settlement_opportunity / uq_settlement_agreement violation — a
	duplicate door_signed must never raise into the caller."""
	from src.services.settlement.split import compute_split  # local import: avoid a cycle with charge.py

	agreement = session.execute(
		text(
			"SELECT opportunity_id, company_id, door_count, door_signed_at "
			"FROM pms_agreements WHERE client_id = :client_id AND pms_agreement_id = :pms_agreement_id"
		),
		{"client_id": client_id, "pms_agreement_id": pms_agreement_id},
	).first()
	if agreement is None:
		logger.error(
			"settlement.open_settlement: no such pms_agreement (client=%s id=%s)", client_id, pms_agreement_id
		)
		return None

	terms = load_offer_terms(session, offer_code)
	if terms is None:
		logger.error("settlement.open_settlement: unknown offer_code=%r", offer_code)
		return None

	plan = compute_split(terms, door_count=agreement.door_count, door_signed_at=agreement.door_signed_at)

	try:
		with session.begin_nested():
			row = session.execute(
				text(
					"INSERT INTO settlement_transactions "
					"(client_id, pms_agreement_id, opportunity_id, company_id, offer_code, door_count, "
					" total_bounty_cents, installment_1_cents, installment_2_cents, "
					" installment_2_scheduled_for, door_signed_at) "
					"VALUES (:client_id, :pms_agreement_id, :opportunity_id, :company_id, :offer_code, :door_count, "
					"        :total_bounty_cents, :installment_1_cents, :installment_2_cents, "
					"        :installment_2_scheduled_for, :door_signed_at) "
					"RETURNING transaction_id"
				),
				{
					"client_id": client_id,
					"pms_agreement_id": pms_agreement_id,
					"opportunity_id": agreement.opportunity_id,
					"company_id": agreement.company_id,
					"offer_code": offer_code,
					"door_count": agreement.door_count,
					"total_bounty_cents": plan.total_bounty_cents,
					"installment_1_cents": plan.installment_1_cents,
					"installment_2_cents": plan.installment_2_cents,
					"installment_2_scheduled_for": plan.installment_2_scheduled_for,
					"door_signed_at": agreement.door_signed_at,
				},
			).one()
	except IntegrityError:
		# SAVEPOINT rollback via begin_nested() — see record_door_signed()'s
		# matching comment; a bare session.rollback() here would also
		# discard a prior successful open_settlement() call in the same
		# session/transaction (the exactly-once-billing test's real bug).
		logger.info(
			"settlement.open_settlement: duplicate settlement (client=%s opportunity=%s agreement=%s) — no-op",
			client_id, agreement.opportunity_id, pms_agreement_id,
		)
		return None

	log_event(
		client_id, "settlement_opened", entity_type="settlement_transaction", entity_id=str(row.transaction_id),
		payload={
			"transaction_id": row.transaction_id,
			"total_bounty_cents": plan.total_bounty_cents,
			"installment_1_cents": plan.installment_1_cents,
			"installment_2_cents": plan.installment_2_cents,
			"installment_2_scheduled_for": plan.installment_2_scheduled_for.isoformat(),
		},
		session=session,
	)
	return row.transaction_id


def claim_installment_1(session: Session, *, claim_time: datetime, limit: int = 20) -> list[int]:
	rows = session.execute(
		text(
			f"""
			UPDATE settlement_transactions
			SET installment_1_status = 'CHARGING', inst1_attempts = inst1_attempts + 1,
				inst1_claimed_at = :claim_time
			WHERE transaction_id IN (
				SELECT transaction_id FROM settlement_transactions
				WHERE (installment_1_status = 'PENDING'
						AND (inst1_next_retry_at IS NULL OR inst1_next_retry_at <= :claim_time))
					OR (installment_1_status = 'FAILED' AND inst1_next_retry_at <= :claim_time)
					OR (installment_1_status = 'CHARGING'
						AND inst1_claimed_at < :claim_time - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes')
					OR (installment_1_status = 'BLOCKED'
						AND inst1_blocked_reason = ANY(:retryable_reasons)
						AND inst1_next_retry_at IS NOT NULL AND inst1_next_retry_at <= :claim_time)
				ORDER BY transaction_id
				FOR UPDATE SKIP LOCKED
				LIMIT :limit
			)
			RETURNING transaction_id
			"""
		),
		{"claim_time": claim_time, "limit": limit, "retryable_reasons": list(_RETRYABLE_BLOCKED_REASONS)},
	).all()
	return [r.transaction_id for r in rows]


def claim_installment_2(session: Session, *, claim_time: datetime, limit: int = 20) -> list[int]:
	rows = session.execute(
		text(
			f"""
			UPDATE settlement_transactions
			SET installment_2_status = 'CHARGING', inst2_attempts = inst2_attempts + 1,
				inst2_claimed_at = :claim_time
			WHERE transaction_id IN (
				SELECT transaction_id FROM settlement_transactions
				WHERE ((installment_2_status = 'SCHEDULED' AND installment_2_scheduled_for <= :claim_time)
						AND (inst2_next_retry_at IS NULL OR inst2_next_retry_at <= :claim_time))
					OR (installment_2_status = 'FAILED' AND inst2_next_retry_at <= :claim_time)
					OR (installment_2_status = 'CHARGING'
						AND inst2_claimed_at < :claim_time - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes')
					OR (installment_2_status = 'BLOCKED'
						AND inst2_blocked_reason = ANY(:retryable_reasons)
						AND installment_2_scheduled_for <= :claim_time
						AND inst2_next_retry_at IS NOT NULL AND inst2_next_retry_at <= :claim_time)
				ORDER BY transaction_id
				FOR UPDATE SKIP LOCKED
				LIMIT :limit
			)
			RETURNING transaction_id
			"""
		),
		{"claim_time": claim_time, "limit": limit, "retryable_reasons": list(_RETRYABLE_BLOCKED_REASONS)},
	).all()
	return [r.transaction_id for r in rows]


def mark_installment(
	session: Session,
	transaction_id: int,
	installment: int,
	status: str,
	*,
	error: Optional[str] = None,
	next_retry_at: Optional[datetime] = None,
	charged_at: Optional[datetime] = None,
	stripe_invoice_id: Optional[str] = None,
	rail: Optional[str] = None,
	blocked_reason: Optional[str] = None,
) -> None:
	"""blocked_reason is only meaningful alongside status='BLOCKED' — any
	other status clears the column, so a stale reason can never survive a
	later, unrelated transition (e.g. BLOCKED -> CHARGING -> CHARGED must not
	leave an old EVIDENCE_PACKET_UNPUBLISHED reason sitting on a charged row)."""
	prefix = "inst1" if installment == 1 else "inst2"
	status_col = f"installment_{installment}_status"
	charged_col = f"installment_{installment}_charged_at"
	blocked_reason_col = f"{prefix}_blocked_reason"
	effective_blocked_reason = blocked_reason if status == "BLOCKED" else None
	session.execute(
		text(
			f"UPDATE settlement_transactions SET "
			f"  {status_col} = :status, "
			f"  {prefix}_last_error = :error, "
			f"  {prefix}_next_retry_at = :next_retry_at, "
			f"  {charged_col} = COALESCE(:charged_at, {charged_col}), "
			f"  {prefix}_stripe_invoice_id = COALESCE(:stripe_invoice_id, {prefix}_stripe_invoice_id), "
			f"  {prefix}_rail = COALESCE(:rail, {prefix}_rail), "
			f"  {blocked_reason_col} = :blocked_reason, "
			f"  updated_at = NOW() "
			f"WHERE transaction_id = :transaction_id"
		),
		{
			"status": status, "error": error, "next_retry_at": next_retry_at, "charged_at": charged_at,
			"stripe_invoice_id": stripe_invoice_id, "rail": rail, "transaction_id": transaction_id,
			"blocked_reason": effective_blocked_reason,
		},
	)


def mark_installment_failed(session: Session, transaction_id: int, installment: int, attempts: int, error: str) -> None:
	"""Bounded retry, same convention as self_serve_audit_worker.py: 2**attempts
	minutes backoff until _MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT, then a
	terminal status excluded from the claim query forever.

	Distinct from defer_installment() below: this path is for a genuine FAILED
	charge attempt (Stripe declined, transport error mid-charge) where giving
	up after bounded retries is correct. Never use this for a BLOCKED-for-a-
	transient-reason row (a Stripe Files outage, a day-60 PMS timeout) — those
	retry indefinitely, because forfeiting 50% of a bounty is strictly worse
	than a row retrying quietly (PR #30 review findings 1 & 2)."""
	from datetime import timedelta, timezone

	if attempts >= _MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT:
		mark_installment(session, transaction_id, installment, "FAILED_PERMANENT", error=error)
	else:
		next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=2 ** attempts)
		mark_installment(session, transaction_id, installment, "FAILED", error=error, next_retry_at=next_retry_at)


def defer_installment(
	session: Session,
	transaction_id: int,
	installment: int,
	*,
	reason: str,
	error: str,
	as_of: datetime,
	attempts: int,
) -> bool:
	"""Marks an installment BLOCKED for a TRANSIENT reason (must be one of
	_RETRYABLE_BLOCKED_REASONS, or claim_installment_1/2 will never re-select
	it) with an exponential-backoff next_retry_at, capped at
	_MAX_BLOCKED_BACKOFF_MINUTES. Unlike mark_installment_failed(), this NEVER
	escalates to FAILED_PERMANENT — a transient block, however long it
	persists, stays reclaimable (PR #30 review: forfeiting 50% of a bounty on
	a PMS/Stripe outage is unacceptable).

	as_of is an explicit parameter, not datetime.now() — same discipline as
	every other settlement entry point, so the backoff window is
	fast-forwardable in tests without clock mocking.

	Returns whether this call crossed the alert threshold (attempts ==
	_ALERT_AFTER_ATTEMPTS) — the caller emits settlement_charge_failed
	exactly once on that edge, not on every subsequent tick."""
	from datetime import timedelta

	backoff_minutes = min(2 ** attempts, _MAX_BLOCKED_BACKOFF_MINUTES)
	next_retry_at = as_of + timedelta(minutes=backoff_minutes)
	mark_installment(
		session, transaction_id, installment, "BLOCKED",
		error=error, next_retry_at=next_retry_at, blocked_reason=reason,
	)
	return attempts == _ALERT_AFTER_ATTEMPTS
