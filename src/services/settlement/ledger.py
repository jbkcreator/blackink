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
	docstring).

	No production caller exists yet for this function (no PMS webhook/API
	route feeds a real termination event into it — the same documented gap
	as StubPmsProvider). The sweep's own claim_terminated_installment_2_for_void()
	(this module) plus src/services/settlement/clawback.py's
	void_terminated_installment_2_batch() are what ACTUALLY reach
	VOIDED_CLAWBACK in production today, reading pms_agreements.terminated_at
	however it got set (this function, a direct UPDATE, or a future real
	integration) — this function does not need to duplicate that logic
	itself; it is not the mechanism, the sweep is."""
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
	"""PR #37 review finding #1: the inner SELECT excludes any transaction
	whose agreement is not ACTIVE. Without this, a single terminated
	agreement in an otherwise-healthy batch makes the trigger's "installment
	1 requires the agreement still be ACTIVE" guard raise on entry into
	CHARGING — since this is one bulk UPDATE covering every claimed row, that
	exception aborts the ENTIRE statement, so no row in the batch (not just
	the terminated one) gets claimed. A terminated-before-ever-charged
	agreement simply never reaches installment 1 (consistent with the
	trigger's own ACTIVE requirement — this rule was already correct, only
	unreachable for the rest of the batch alongside it)."""
	rows = session.execute(
		text(
			f"""
			UPDATE settlement_transactions
			SET installment_1_status = 'CHARGING', inst1_attempts = inst1_attempts + 1,
				inst1_claimed_at = :claim_time
			WHERE transaction_id IN (
				SELECT t.transaction_id FROM settlement_transactions t
				JOIN pms_agreements a ON a.pms_agreement_id = t.pms_agreement_id
				WHERE a.status = 'ACTIVE'
					AND (
						(t.installment_1_status = 'PENDING'
							AND (t.inst1_next_retry_at IS NULL OR t.inst1_next_retry_at <= :claim_time))
						OR (t.installment_1_status = 'FAILED' AND t.inst1_next_retry_at <= :claim_time)
						OR (t.installment_1_status = 'CHARGING'
							AND t.inst1_claimed_at < :claim_time - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes')
						OR (t.installment_1_status = 'BLOCKED'
							AND t.inst1_blocked_reason = ANY(:retryable_reasons)
							AND t.inst1_next_retry_at IS NOT NULL AND t.inst1_next_retry_at <= :claim_time)
					)
				ORDER BY t.transaction_id
				FOR UPDATE OF t SKIP LOCKED
				LIMIT :limit
			)
			RETURNING transaction_id
			"""
		),
		{"claim_time": claim_time, "limit": limit, "retryable_reasons": list(_RETRYABLE_BLOCKED_REASONS)},
	).all()
	return [r.transaction_id for r in rows]


def claim_installment_2(session: Session, *, claim_time: datetime, limit: int = 20) -> list[int]:
	"""PR #37 review finding #1: the inner SELECT excludes any transaction
	whose agreement terminated inside its own offer's clawback window — the
	same "one bad row aborts the whole bulk UPDATE" failure mode as
	claim_installment_1, this time against the trigger's "installment 2
	requires the agreement not to have terminated inside the clawback
	window" guard. Such a row must never enter CHARGING at all; it belongs
	to claim_terminated_installment_2_for_void() instead, which the sweep
	runs BEFORE this claim (this predicate is the defense-in-depth backstop
	for a termination landing between those two steps in the same tick, not
	the primary path)."""
	rows = session.execute(
		text(
			f"""
			UPDATE settlement_transactions
			SET installment_2_status = 'CHARGING', inst2_attempts = inst2_attempts + 1,
				inst2_claimed_at = :claim_time
			WHERE transaction_id IN (
				SELECT t.transaction_id FROM settlement_transactions t
				JOIN pms_agreements a ON a.pms_agreement_id = t.pms_agreement_id
				JOIN settlement_offer_config o ON o.offer_code = t.offer_code
				WHERE NOT (
						a.terminated_at IS NOT NULL AND o.clawback_window_days IS NOT NULL
						AND a.terminated_at < t.door_signed_at + (o.clawback_window_days || ' days')::INTERVAL
					)
					AND (
						((t.installment_2_status = 'SCHEDULED' AND t.installment_2_scheduled_for <= :claim_time)
							AND (t.inst2_next_retry_at IS NULL OR t.inst2_next_retry_at <= :claim_time))
						OR (t.installment_2_status = 'FAILED' AND t.inst2_next_retry_at <= :claim_time)
						OR (t.installment_2_status = 'CHARGING'
							AND t.inst2_claimed_at < :claim_time - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes')
						OR (t.installment_2_status = 'BLOCKED'
							AND t.inst2_blocked_reason = ANY(:retryable_reasons)
							AND t.installment_2_scheduled_for <= :claim_time
							AND t.inst2_next_retry_at IS NOT NULL AND t.inst2_next_retry_at <= :claim_time)
					)
				ORDER BY t.transaction_id
				FOR UPDATE OF t SKIP LOCKED
				LIMIT :limit
			)
			RETURNING transaction_id
			"""
		),
		{"claim_time": claim_time, "limit": limit, "retryable_reasons": list(_RETRYABLE_BLOCKED_REASONS)},
	).all()
	return [r.transaction_id for r in rows]


def claim_terminated_installment_2_for_void(session: Session, *, limit: int = 20) -> list[dict]:
	"""PR #37 review finding #1 — the other half of claim_installment_2's
	exclusion: a transaction excluded from that claim because its agreement
	terminated inside the clawback window must still actually REACH
	VOIDED_CLAWBACK, not sit excluded forever. Locks (FOR UPDATE SKIP LOCKED)
	without transitioning status yet — the trigger permits a direct
	transition into VOIDED_CLAWBACK from any prior state (only entry into
	CHARGING/SETTLING/CHARGED is guarded), so the caller
	(clawback.void_terminated_installment_2_batch) writes that terminal
	status itself, after voiding any Stripe invoice that may already exist
	for the row."""
	rows = session.execute(
		text(
			"""
			SELECT t.transaction_id, t.client_id, t.installment_2_cents, t.inst2_stripe_invoice_id,
				t.door_signed_at, a.terminated_at
			FROM settlement_transactions t
			JOIN pms_agreements a ON a.pms_agreement_id = t.pms_agreement_id
			JOIN settlement_offer_config o ON o.offer_code = t.offer_code
			WHERE t.installment_2_status IN ('SCHEDULED', 'FAILED', 'BLOCKED')
				AND a.terminated_at IS NOT NULL
				AND o.clawback_window_days IS NOT NULL
				AND a.terminated_at < t.door_signed_at + (o.clawback_window_days || ' days')::INTERVAL
			ORDER BY t.transaction_id
			FOR UPDATE OF t SKIP LOCKED
			LIMIT :limit
			"""
		),
		{"limit": limit},
	).all()
	return [
		{
			"transaction_id": r.transaction_id, "client_id": r.client_id,
			"installment_2_cents": r.installment_2_cents,
			"inst2_stripe_invoice_id": r.inst2_stripe_invoice_id,
			"door_signed_at": r.door_signed_at, "terminated_at": r.terminated_at,
		}
		for r in rows
	]


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


def mark_installment_failed(
	session: Session, transaction_id: int, installment: int, attempts: int, error: str,
	*, stripe_invoice_id: Optional[str] = None,
) -> bool:
	"""Bounded retry, same convention as self_serve_audit_worker.py: 2**attempts
	minutes backoff until _MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT, then a
	terminal status excluded from the claim query forever.

	Distinct from defer_installment() below: this path is for a genuine FAILED
	charge attempt (Stripe declined, transport error mid-charge) where giving
	up after bounded retries is correct. Never use this for a BLOCKED-for-a-
	transient-reason row (a Stripe Files outage, a day-60 PMS timeout) — those
	retry indefinitely, because forfeiting 50% of a bounty is strictly worse
	than a row retrying quietly (PR #30 review findings 1 & 2).

	stripe_invoice_id (PR #37 review finding #2) is threaded through even on
	a FAILED/FAILED_PERMANENT decline — the invoice was already created and
	finalized by the time a pay_invoice call declines, and without recording
	it here a retry had no way to know that and would call create_invoice
	again under charge.py's OBJECT-creation idempotency key (which does
	protect against Stripe seeing two calls within its own ~24h retention
	window, but this stored id is what lets charge_installment reuse the
	SAME invoice deliberately, by choice, not by hoping the key cache is
	still warm).

	Returns True exactly once — the specific call that FIRST transitions this
	installment into FAILED_PERMANENT (a compare-and-swap on
	inst{N}_alerted_permanent_at, so a duplicate/racing call for the same
	already-FAILED_PERMANENT row never returns True twice) — the caller
	(charge.py) emits settlement_charge_failed_permanent only on that edge,
	giving "exactly one incident per terminal failure" a real guarantee
	rather than an per-call assumption."""
	from datetime import timedelta, timezone

	if attempts >= _MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT:
		mark_installment(
			session, transaction_id, installment, "FAILED_PERMANENT", error=error, stripe_invoice_id=stripe_invoice_id,
		)
		prefix = "inst1" if installment == 1 else "inst2"
		alerted = session.execute(
			text(
				f"UPDATE settlement_transactions SET {prefix}_alerted_permanent_at = NOW() "
				f"WHERE transaction_id = :tid AND {prefix}_alerted_permanent_at IS NULL "
				f"RETURNING transaction_id"
			),
			{"tid": transaction_id},
		).first()
		return alerted is not None
	else:
		next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=2 ** attempts)
		mark_installment(
			session, transaction_id, installment, "FAILED", error=error, next_retry_at=next_retry_at,
			stripe_invoice_id=stripe_invoice_id,
		)
		return False


def reopen_failed_permanent_installment(
	session: Session, *, transaction_id: int, installment: int, reason: str, as_of: datetime,
) -> bool:
	"""Operator-triggered recovery for a FAILED_PERMANENT installment — e.g.
	the client fixed a declined payment method after all 3 automatic attempts
	were exhausted (PR #37 review finding #2's "controlled manual reopening").
	Deliberately never automatic: a FAILED_PERMANENT row is never re-selected
	by claim_installment_1/2 on its own, by design (this repo's own accepted
	failure direction is under-billing, never an unbounded auto-retry loop).

	Resets the attempt counter to 0 (a fresh 3-attempt budget — the SAME
	pinned object-creation idempotency keys in charge.py, but the payment
	ATTEMPT keys are attempt-numbered, so this genuinely tries again rather
	than replaying the original decline) and clears the alert guard so a
	LATER FAILED_PERMANENT can alert again. No-op (returns False, no event
	logged) unless the row is genuinely FAILED_PERMANENT for this installment
	right now — never silently reopens an already-charged or already-voided
	row."""
	prefix = "inst1" if installment == 1 else "inst2"
	status_col = f"installment_{installment}_status"
	row = session.execute(
		text(
			f"UPDATE settlement_transactions SET {status_col} = 'FAILED', "
			f"  {prefix}_attempts = 0, {prefix}_next_retry_at = :as_of, "
			f"  {prefix}_alerted_permanent_at = NULL, {prefix}_last_error = NULL, updated_at = NOW() "
			f"WHERE transaction_id = :tid AND {status_col} = 'FAILED_PERMANENT' "
			f"RETURNING client_id"
		),
		{"tid": transaction_id, "as_of": as_of},
	).first()
	if row is None:
		return False
	log_event(
		row.client_id, "settlement_installment_reopened", entity_type="settlement_transaction",
		entity_id=str(transaction_id),
		payload={"transaction_id": transaction_id, "installment": installment, "reason": reason},
		session=session,
	)
	return True


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
