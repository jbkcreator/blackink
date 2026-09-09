"""Rules 2 ("first sit free") and 5 ("no monthly ceiling") — Subtask 1.2.3.

Deliberately contains no billable-sit counting cap of any kind, and no
per-invoice override parameter — both are Definition-of-Done lines that would
otherwise only be "confirmed via code review". tests/test_billing_structural.py
turns both into real assertions (the same technique
tests/test_no_upfront_charge_paths.py uses for the settlement engine's
zero-upfront rule), rather than leaving them as an unverified code-review
claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

APPT_FIRST_OFFER_CODE = "appt_first"
APPT_STANDARD_OFFER_CODE = "appt_standard"


class NoActiveEntitlementError(RuntimeError):
	"""The client has no ACTIVE client_entitlements row for a billable plan —
	nothing to charge against. Never silently defaults to a price."""


@dataclass(frozen=True)
class SitCharge:
	offer_code: str
	amount_cents: int
	first_sit: bool


def _stamp_billed(session: Session, *, client_id: str, appointment_id: str, charge: SitCharge) -> SitCharge:
	"""Records what this appointment was ACTUALLY billed as, on the
	appointment row itself — read back by
	src.services.billing.dispute_credit so a disputed FREE first sit never
	manufactures a $99 credit (a real bug found by review: re-deriving "what
	would this have cost" at dispute time is unreliable, since
	first_sit_consumed may have flipped for an unrelated LATER appointment by
	then)."""
	session.execute(
		text(
			"UPDATE appointments SET billed_offer_code = :offer_code, billed_amount_cents = :amount_cents "
			"WHERE client_id = :client_id AND appointment_id = :appointment_id"
		),
		{"offer_code": charge.offer_code, "amount_cents": charge.amount_cents, "client_id": client_id, "appointment_id": appointment_id},
	)
	return charge


def resolve_sit_charge(session: Session, *, client_id: str, appointment_id: str, as_of: datetime) -> SitCharge:
	"""Reads appointments.is_billable for appointment_id (raises if the
	appointment isn't billable — a caller must gate on is_billable before
	calling this, same discipline as the settlement engine's own
	is_billable-gated charge path). If the client's plan entitlement has
	first_sit_consumed = FALSE, flips the flag and returns the $0 appt_first
	charge IN THE SAME TRANSACTION as the read — a crash between the two
	could otherwise consume the free sit without billing $0, or bill $0
	twice. Otherwise returns the flat appt_standard charge with no cap check
	of any kind, however many sits the client has already been charged for
	this month. Every return path stamps appointments.billed_offer_code/
	billed_amount_cents so a later dispute credit reads the real charge.

	IDEMPOTENT per appointment (PR #37 review finding): once
	appointments.billed_offer_code is already set, this function returns
	THAT recorded charge unconditionally on every subsequent call — it never
	re-evaluates first_sit_consumed or re-derives a price. Without this, a
	retried call for the SAME appointment (a sweep re-claiming a row after a
	crash mid-invoice, or any other caller re-invoking this function) could
	observe first_sit_consumed already flipped TRUE by its own prior call and
	silently rebill the client's free first sit as a $99 standard sit."""
	appointment = session.execute(
		text(
			"SELECT is_billable, billed_offer_code, billed_amount_cents FROM appointments "
			"WHERE client_id = :client_id AND appointment_id = :appointment_id"
		),
		{"client_id": client_id, "appointment_id": appointment_id},
	).first()
	if appointment is None:
		raise ValueError(f"no such appointment {appointment_id!r} for client {client_id!r}")
	if appointment.billed_offer_code is not None:
		return SitCharge(
			offer_code=appointment.billed_offer_code,
			amount_cents=appointment.billed_amount_cents,
			first_sit=appointment.billed_offer_code == APPT_FIRST_OFFER_CODE,
		)
	if not appointment.is_billable:
		raise ValueError(f"appointment {appointment_id!r} is not billable — cannot resolve a sit charge")

	entitlement = session.execute(
		text(
			"SELECT entitlement_id, first_sit_consumed FROM client_entitlements "
			"WHERE client_id = :client_id AND status = 'ACTIVE' "
			"  AND offer_code NOT IN (:appt_first, :appt_standard) "
			"ORDER BY activated_at LIMIT 1"
		),
		{"client_id": client_id, "appt_first": APPT_FIRST_OFFER_CODE, "appt_standard": APPT_STANDARD_OFFER_CODE},
	).first()
	if entitlement is None:
		raise NoActiveEntitlementError(f"client {client_id!r} has no ACTIVE billable plan entitlement")

	appt_standard_offer = session.execute(
		text("SELECT price_cents FROM entitlement_offers WHERE offer_code = :offer_code"),
		{"offer_code": APPT_STANDARD_OFFER_CODE},
	).one()

	if not entitlement.first_sit_consumed:
		with session.begin_nested():
			updated = session.execute(
				text(
					"UPDATE client_entitlements SET first_sit_consumed = TRUE, updated_at = :as_of "
					"WHERE entitlement_id = :entitlement_id AND first_sit_consumed = FALSE"
				),
				{"as_of": as_of, "entitlement_id": entitlement.entitlement_id},
			)
			if updated.rowcount == 0:
				# Lost a race against a concurrent claim — the free sit was
				# already consumed between our SELECT and this UPDATE. Fall
				# through to the standard charge rather than double-granting
				# the free sit.
				return _stamp_billed(
					session, client_id=client_id, appointment_id=appointment_id,
					charge=SitCharge(offer_code=APPT_STANDARD_OFFER_CODE, amount_cents=appt_standard_offer.price_cents, first_sit=False),
				)
		from src.services.events import log_event

		log_event(
			client_id, "first_sit_consumed", entity_type="appointment", entity_id=str(appointment_id),
			payload={"client_id": client_id, "appointment_id": str(appointment_id), "offer_code": APPT_FIRST_OFFER_CODE},
			session=session,
		)
		return _stamp_billed(
			session, client_id=client_id, appointment_id=appointment_id,
			charge=SitCharge(offer_code=APPT_FIRST_OFFER_CODE, amount_cents=0, first_sit=True),
		)

	return _stamp_billed(
		session, client_id=client_id, appointment_id=appointment_id,
		charge=SitCharge(offer_code=APPT_STANDARD_OFFER_CODE, amount_cents=appt_standard_offer.price_cents, first_sit=False),
	)
