"""Rule 4 — dispute credit on flagging, not resolution (Subtask 1.2.3).

"A disputed appointment sit is credited at the moment the dispute is flagged
(flagged_at), not when it is resolved. The dispute window is 48 hours from
the scheduled meeting time." (docs/Sept04_New_Items_Triage.md item 10) —
which explicitly overrides the blueprint's superseded "5-business-day
window" (Tasks/...v2.md line 1024).

appointment_disputes.flagged_at and outcome DEFAULT 'CREDITED_AUTOMATIC'
already exist (migrations/apply_appointment_ops.py, Subtask 1.1.1); this
module is the deferred "Week-2 billing logic" that migration's own docstring
named. The credit's amount is the sit's own ACTUAL charge amount — read from
appointments.billed_amount_cents (stamped by
src.services.billing.sit_billing.resolve_sit_charge at charge time), so a
disputed $0 first-sit never manufactures a $99 credit. This was a real bug
in an earlier version of this module: re-deriving "what would this sit have
cost" from entitlement_offers at dispute time always returned appt_standard's
price, because there is no reliable way to retroactively tell whether THIS
appointment was the free first sit — first_sit_consumed may have already
flipped for an unrelated later appointment by the time a dispute is
credited. Recording the real charge at billing time is the only correct
fix.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.billing.credits import issue_credit

logger = logging.getLogger(__name__)

DISPUTE_WINDOW_HOURS = 48


class DisputeWindowExpiredError(RuntimeError):
	"""The dispute was flagged more than 48 hours after the appointment's
	scheduled_for — rejected, not silently accepted."""


def credit_dispute_on_flag(session: Session, *, dispute_id: str, as_of: datetime) -> bool:
	"""Validates the 48-hour window against appointments.scheduled_for, then
	issues (or no-ops on a duplicate) a DISPUTE_CREDIT for the sit's own
	charge amount, keyed on source_id = dispute_id — so a later update to
	appointment_disputes.resolved_at can never write a second credit
	(billing_credits' own UNIQUE constraint is what makes that structural,
	not this function's control flow)."""
	row = session.execute(
		text(
			"SELECT d.client_id, d.appointment_id, d.flagged_at, a.scheduled_for, a.billed_amount_cents "
			"FROM appointment_disputes d "
			"JOIN appointments a ON a.client_id = d.client_id AND a.appointment_id = d.appointment_id "
			"WHERE d.dispute_id = :dispute_id"
		),
		{"dispute_id": dispute_id},
	).first()
	if row is None:
		raise ValueError(f"no such dispute {dispute_id!r}")

	window_deadline = row.scheduled_for + timedelta(hours=DISPUTE_WINDOW_HOURS)
	if row.flagged_at > window_deadline:
		session.execute(
			text("UPDATE appointment_disputes SET credit_status = 'EXPIRED' WHERE dispute_id = :dispute_id"),
			{"dispute_id": dispute_id},
		)
		raise DisputeWindowExpiredError(
			f"dispute {dispute_id!r} flagged at {row.flagged_at.isoformat()} is outside the "
			f"{DISPUTE_WINDOW_HOURS}h window from scheduled_for={row.scheduled_for.isoformat()}"
		)

	credit_amount_cents = _resolve_disputed_sit_amount_cents(
		session, client_id=row.client_id, appointment_id=row.appointment_id, billed_amount_cents=row.billed_amount_cents,
	)
	if credit_amount_cents <= 0:
		# A disputed FREE first sit (billed $0) has nothing to credit —
		# billing_credits.amount_cents is CHECK > 0, so there is no row to
		# write. Still mark the dispute CREDITED (not PENDING forever) since
		# the rule ("credited on flagging") was correctly evaluated — there
		# was simply nothing owed.
		session.execute(
			text("UPDATE appointment_disputes SET credit_status = 'CREDITED' WHERE dispute_id = :dispute_id"),
			{"dispute_id": dispute_id},
		)
		return False

	billing_period = date(row.flagged_at.year, row.flagged_at.month, 1)
	credited = issue_credit(
		session,
		client_id=row.client_id,
		credit_type="DISPUTE_CREDIT",
		amount_cents=credit_amount_cents,
		source_table="appointment_disputes",
		source_id=str(dispute_id),
		issued_at=row.flagged_at,
		billing_period=billing_period,
	)
	session.execute(
		text("UPDATE appointment_disputes SET credit_status = 'CREDITED' WHERE dispute_id = :dispute_id"),
		{"dispute_id": dispute_id},
	)
	if credited:
		logger.info("billing.dispute_credit: issued credit (client=%s dispute=%s amount_cents=%d)", row.client_id, dispute_id, credit_amount_cents)
	return credited


def _resolve_disputed_sit_amount_cents(session: Session, *, client_id: str, appointment_id: str, billed_amount_cents) -> int:
	"""The disputed sit's credit amount is what it was ACTUALLY billed —
	appointments.billed_amount_cents, stamped at charge time by
	sit_billing.resolve_sit_charge(). Falls back to appt_standard's current
	price only for a legacy row that predates that column being stamped (should
	not happen for any appointment billed after this fix landed) — logged as a
	warning since it's a real gap, not a silent default."""
	if billed_amount_cents is not None:
		return billed_amount_cents
	logger.warning(
		"billing.dispute_credit: appointment %s has no billed_amount_cents recorded — "
		"falling back to appt_standard's current price (client=%s)", appointment_id, client_id,
	)
	offer = session.execute(
		text("SELECT price_cents FROM entitlement_offers WHERE offer_code = 'appt_standard'"),
	).one()
	return offer.price_cents
