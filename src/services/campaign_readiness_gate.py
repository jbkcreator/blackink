"""Checks 3+4 of the literal DoD compliance gate (Week 1 Subtask 1.2.2):
DNC registry scrub, quiet hours, and the warm-channel waterfall.

Orchestrates on top of evaluate_campaign_readiness() (Checks 1+2, SQL,
Subtask 1.2.1) rather than extending that function directly — the DoD's
"DNC cache miss falls back to a live API query, cached for subsequent
calls" requirement cannot be satisfied inside a plpgsql function, so
Checks 3+4 run here in Python where the live-API call and cache write are
normal, mockable code. Reuses DncProvider/StubDncProvider from
compliance_gate.py rather than redefining them.

Per the master blueprint (Project Blackink — Complete Implementation
Blueprint, §3.1.2's waterfall diagram and §3.0.4's CI-enforced predicate),
NOT the Week1_Tasks_Dev_Split.md DoD checklist's more ambiguous wording:
a DNC hit is a partial channel restriction ("CHANNEL SUPPRESSED" — strips
phone/SMS, email unaffected), not a full block like Checks 1/2. The warm/
cold signal is the literal predicate from §3.0.4:
inbound_sms_count > 0 OR booked_appointment_id IS NOT NULL.

Does not commit — caller's session_scope() owns the transaction, same
convention as evaluate_compliance_gate().
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.services.compliance_gate import DncProvider, StubDncProvider

OPT_OUT = "OPT_OUT"
NON_POACH = "NON_POACH"
COLD_EMAIL_ONLY = "COLD_EMAIL_ONLY"
ENGAGED_SMS_ELIGIBLE = "ENGAGED_SMS_ELIGIBLE"
ENGAGED_QUIET_HOURS_SMS_WITHHELD = "ENGAGED_QUIET_HOURS_SMS_WITHHELD"
ENGAGED_DNC_SMS_WITHHELD = "ENGAGED_DNC_SMS_WITHHELD"
ENGAGED_DNC_UNKNOWN_SMS_WITHHELD = "ENGAGED_DNC_UNKNOWN_SMS_WITHHELD"

_SQL_GATE_REASON = {
	"PERMANENTLY_BLOCKED": OPT_OUT,
	"SUPPRESSED": NON_POACH,
}

_QUIET_HOURS_START = 21  # 9 PM
_QUIET_HOURS_END = 8  # 8 AM


@dataclass(frozen=True)
class FullReadinessResult:
	contact_id: int
	readiness: bool
	compliance_eligibility: str
	reason_code: str


def _resolve_dnc_listed(
	session: Session, contact_id: int, phone: Optional[str], dnc_provider: DncProvider
) -> Optional[bool]:
	"""Cache-first DNC check reusing contacts.dnc_clean/dnc_checked_at (built
	in Task 1.1 for exactly this). Cache hit within dnc_recheck_days reuses
	the cached value; miss (or no phone) queries the live provider and
	writes the result back so the next evaluation hits cache.

	Tri-state: True (listed), False (clear), or None (couldn't determine —
	no phone, or the provider itself returned None, e.g. the default
	StubDncProvider with no vendor contracted, or a live lookup failure). A
	None result is never written to the cache — caching a guess as clean
	would let a later evaluation trust a value nothing ever actually
	verified, and extend that false confidence for the whole recheck
	window."""
	row = session.execute(
		text("SELECT dnc_clean, dnc_checked_at FROM contacts WHERE contact_id = :contact_id"),
		{"contact_id": contact_id},
	).one()

	if row.dnc_checked_at is not None:
		age = datetime.now(timezone.utc) - row.dnc_checked_at
		if age <= timedelta(days=get_settings().dnc_recheck_days) and row.dnc_clean is not None:
			return not row.dnc_clean

	if not phone:
		return None

	listed = dnc_provider.check(phone)
	if listed is None:
		return None

	session.execute(
		text(
			"UPDATE contacts SET dnc_clean = :clean, dnc_checked_at = NOW() "
			"WHERE contact_id = :contact_id"
		),
		{"clean": not listed, "contact_id": contact_id},
	)
	return listed


def is_engaged(inbound_sms_count: int, booked_appointment_id: Optional[str]) -> bool:
	"""Literal predicate from the master blueprint §3.0.4. Public — also
	reused by src/services/sms_dispatch.py's application-layer cold-SMS
	linter (Subtask 1.2.3), so the rule has one definition, not two."""
	return inbound_sms_count > 0 or booked_appointment_id is not None


def _decide_channel(engaged: bool, dnc_listed: Optional[bool], quiet_hours_active: bool) -> tuple[str, str]:
	"""Pure warm-channel waterfall decision (Check 4), factored out for unit
	testing without a DB. Never returns TRANSACTIONAL_SMS_ONLY unless
	engaged, confirmed NOT DNC-listed (dnc_listed is False, not just
	falsy — None must not slip through the same as False), and outside
	quiet hours — the CI-enforced predicate from the master blueprint
	§3.0.4."""
	if not engaged:
		return "EMAIL_COLD_ELIGIBLE", COLD_EMAIL_ONLY
	if dnc_listed is None:
		return "EMAIL_COLD_ELIGIBLE", ENGAGED_DNC_UNKNOWN_SMS_WITHHELD
	if dnc_listed:
		return "EMAIL_COLD_ELIGIBLE", ENGAGED_DNC_SMS_WITHHELD
	if quiet_hours_active:
		return "EMAIL_COLD_ELIGIBLE", ENGAGED_QUIET_HOURS_SMS_WITHHELD
	return "TRANSACTIONAL_SMS_ONLY", ENGAGED_SMS_ELIGIBLE


def _local_hour(tz_name: str) -> int:
	"""Isolated for monkeypatching in tests — real callers just want
	"what hour is it right now in this timezone"."""
	return datetime.now(ZoneInfo(tz_name)).hour


def _in_quiet_hours(session: Session, phone: Optional[str]) -> bool:
	"""9pm-8am recipient local time, derived from the US area code. Fails
	closed (treated as quiet hours) when the phone is missing, non-US, or
	the area code has no timezone data — matches the project's existing
	ABSTAIN-blocks philosophy rather than assuming it's safe to send."""
	if not phone or not phone.startswith("+1") or len(phone) < 5:
		return True

	area_code = phone[2:5]
	tz_name = session.execute(
		text("SELECT iana_timezone FROM us_area_code_timezones WHERE area_code = :area_code"),
		{"area_code": area_code},
	).scalar()
	if tz_name is None:
		return True

	local_hour = _local_hour(tz_name)
	return local_hour >= _QUIET_HOURS_START or local_hour < _QUIET_HOURS_END


def _record_event(
	session: Session,
	contact_id: int,
	client_id: str,
	result: str,
	compliance_eligibility: str,
	reason_code: str,
) -> None:
	session.execute(
		text(
			"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
			"VALUES (:client_id, 'compliance_gate_evaluated', 'contact', :entity_id, "
			"jsonb_build_object('result', :result, 'compliance_eligibility', :eligibility, "
			"'reason_code', :reason_code))"
		),
		{
			"client_id": client_id,
			"entity_id": str(contact_id),
			"result": result,
			"eligibility": compliance_eligibility,
			"reason_code": reason_code,
		},
	)


def evaluate_full_readiness(
	session: Session,
	contact_id: int,
	requesting_client_id: str,
	dnc_provider: Optional[DncProvider] = None,
) -> FullReadinessResult:
	"""Evaluates all four checks for a contact: Checks 1+2 via the SQL
	evaluate_campaign_readiness() function (Subtask 1.2.1), then Checks 3+4
	here. Writes the final compliance_eligibility to contacts and logs a
	compliance_gate_evaluated event, then returns the aggregate result."""
	dnc_provider = dnc_provider or StubDncProvider()

	sql_result = session.execute(
		text("SELECT evaluate_campaign_readiness(:contact_id)"), {"contact_id": contact_id}
	).scalar()

	if sql_result in _SQL_GATE_REASON:
		reason_code = _SQL_GATE_REASON[sql_result]
		session.execute(
			text("UPDATE contacts SET compliance_eligibility = 'BLOCKED' WHERE contact_id = :contact_id"),
			{"contact_id": contact_id},
		)
		_record_event(session, contact_id, requesting_client_id, sql_result, "BLOCKED", reason_code)
		return FullReadinessResult(contact_id, False, "BLOCKED", reason_code)

	contact = session.execute(
		text(
			"SELECT phone, inbound_sms_count, booked_appointment_id "
			"FROM contacts WHERE contact_id = :contact_id"
		),
		{"contact_id": contact_id},
	).one()

	dnc_listed = _resolve_dnc_listed(session, contact_id, contact.phone, dnc_provider)
	engaged = is_engaged(contact.inbound_sms_count, contact.booked_appointment_id)
	quiet_hours_active = engaged and dnc_listed is False and _in_quiet_hours(session, contact.phone)

	eligibility, reason_code = _decide_channel(engaged, dnc_listed, quiet_hours_active)

	session.execute(
		text("UPDATE contacts SET compliance_eligibility = :eligibility WHERE contact_id = :contact_id"),
		{"eligibility": eligibility, "contact_id": contact_id},
	)
	_record_event(session, contact_id, requesting_client_id, "PASS", eligibility, reason_code)
	return FullReadinessResult(contact_id, True, eligibility, reason_code)
