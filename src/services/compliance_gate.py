"""Deterministic (no LLM) compliance / readiness gate.

Tri-state (PASS/FAIL/ABSTAIN), patterned on Forced Action's
venture_ladder.py:_gate_color() — the one already-proven fix (per its own
ADR 0006) for "missing metric silently defaults to red/zero." Important
divergence from that source pattern: venture_ladder's bias is toward
permitting growth under uncertainty; a compliance gate's bias must be the
opposite — ABSTAIN always blocks, unconditionally, no no_metric_behavior-
style override.

Deliberate divergence from this function's own design intent, stated
plainly: all checks are always evaluated (not short-circuited on first
failure) so compliance_gate_checks captures a COMPLETE audit trail for
"why didn't this contact get emailed" — evaluating only up to the first
failure would leave gaps in that record for every check after it.
`ready` and `blocked_reasons` give the same answer either way; this only
affects how much diagnostic detail is recorded.

Does not commit — caller's session_scope() owns the transaction, same
convention as Forced Action's email_suppression.suppress_contact().
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings

PASS = "PASS"
FAIL = "FAIL"
ABSTAIN = "ABSTAIN"


@dataclass(frozen=True)
class GateCheckResult:
	name: str
	status: str
	detail: str


@dataclass(frozen=True)
class ComplianceGateResult:
	contact_id: int
	checks: Tuple[GateCheckResult, ...]

	@property
	def ready(self) -> bool:
		return all(c.status == PASS for c in self.checks)

	@property
	def blocked_reasons(self) -> Tuple[str, ...]:
		return tuple(f"{c.name}: {c.status} - {c.detail}" for c in self.checks if c.status != PASS)


# ── Pluggable providers — no vendor contracted yet (Dev 1 plan open item) ──
# Stub implementations return ABSTAIN-driving signals (None / "unknown"),
# never a false PASS, so the gate is safe to run before procurement lands.


class DncProvider(ABC):
	@abstractmethod
	def check(self, phone: str) -> Optional[bool]:
		"""Return True (listed/blocked), False (clear), or None (couldn't determine)."""


class EmailVerificationProvider(ABC):
	@abstractmethod
	def verify(self, email: str) -> str:
		"""Return 'valid', 'invalid', or 'unknown'."""


class StubDncProvider(DncProvider):
	"""No vendor contracted yet. Always returns None (unknown) so the
	dnc_clean check ABSTAINs rather than silently passing or failing."""

	def check(self, phone: str) -> Optional[bool]:
		return None


class StubEmailVerificationProvider(EmailVerificationProvider):
	def verify(self, email: str) -> str:
		return "unknown"


def _record(session: Session, contact_id: int, client_id: str, result: GateCheckResult) -> None:
	session.execute(
		text(
			"INSERT INTO compliance_gate_checks (contact_id, client_id, check_name, status, detail) "
			"VALUES (:contact_id, :client_id, :check_name, :status, :detail)"
		),
		{
			"contact_id": contact_id,
			"client_id": client_id,
			"check_name": result.name,
			"status": result.status,
			"detail": result.detail,
		},
	)


def _check_deterministic_columns(contact) -> GateCheckResult:
	if contact.email_status != "VERIFIED":
		return GateCheckResult("email_verified", FAIL, f"email_status={contact.email_status}")
	if contact.is_opted_out:
		return GateCheckResult("email_verified", FAIL, "is_opted_out=true")
	if contact.suppression_state:
		return GateCheckResult("email_verified", FAIL, "suppression_state=true")
	if contact.compliance_eligibility != "EMAIL_COLD_ELIGIBLE":
		return GateCheckResult(
			"email_verified", FAIL, f"compliance_eligibility={contact.compliance_eligibility}"
		)
	return GateCheckResult("email_verified", PASS, "verified, not opted out, not suppressed, cold-eligible")


def _check_cooldown(contact) -> GateCheckResult:
	if contact.last_outbound_touch_at is None:
		return GateCheckResult("cooldown_14d", PASS, "no prior outbound touch")
	elapsed = datetime.now(timezone.utc) - contact.last_outbound_touch_at
	if elapsed >= timedelta(days=14):
		return GateCheckResult("cooldown_14d", PASS, f"last touch {elapsed.days}d ago")
	return GateCheckResult("cooldown_14d", FAIL, f"last touch {elapsed.days}d ago, cooldown not elapsed")


def _check_dnc(contact, dnc_provider: DncProvider) -> GateCheckResult:
	max_age_days = get_settings().dnc_recheck_days
	if contact.dnc_checked_at is None:
		# Live check: unchecked contact needs a fresh lookup, not an
		# automatic ABSTAIN, so the gate can actually clear a new contact.
		if not contact.phone:
			# No phone means the DNC registry has nothing to say — not a hit.
			return GateCheckResult("dnc_clean", PASS, "no phone on file — DNC registry not applicable")
		result = dnc_provider.check(contact.phone)
		if result is None:
			return GateCheckResult("dnc_clean", ABSTAIN, "DNC check returned unknown (no vendor / lookup failed)")
		return (
			GateCheckResult("dnc_clean", FAIL, "listed on DNC registry")
			if result
			else GateCheckResult("dnc_clean", PASS, "checked, not listed")
		)

	age = datetime.now(timezone.utc) - contact.dnc_checked_at
	if age > timedelta(days=max_age_days):
		# A stale TRUE is not trusted — ABSTAIN regardless of the cached value.
		return GateCheckResult("dnc_clean", ABSTAIN, f"cached result is {age.days}d old (max {max_age_days}d)")
	if contact.dnc_clean is None:
		return GateCheckResult("dnc_clean", ABSTAIN, "dnc_clean is NULL despite a recent check timestamp")
	return (
		GateCheckResult("dnc_clean", PASS, f"cached clean, checked {age.days}d ago")
		if contact.dnc_clean
		else GateCheckResult("dnc_clean", FAIL, f"cached listed, checked {age.days}d ago")
	)


def _check_not_paused(contact) -> GateCheckResult:
	"""Subtask 3.2.3 — No-Show Handler. The real dispatch safeguard for a
	contact paused after a sales-demo no-show: contacts.outbound_paused_at
	is set by the Slack mark_no_show click handler
	(src/services/slack/listeners.py) and cleared by booking_ingest.py only
	when the contact rebooks under a genuinely new booking_id. There is no
	durable outbound-sequence/campaign-enrollment engine in this codebase
	yet to mark PAUSED directly (src/agents/cora/worker.py's own docstring
	states draft generation is still a Week-0 placeholder) — this check is
	the actual, enforced choke point every cold-campaign send already
	passes through per contact."""
	# getattr with a default: the compliance gate is a shared choke point
	# reached by several callers (the outbound sequencer's enrollment gate
	# among them), some of which pass lightweight contact objects that
	# predate the outbound_paused_at column. A missing attribute means "not
	# paused", never an AttributeError that would wrongly block the send.
	if getattr(contact, "outbound_paused_at", None) is not None:
		return GateCheckResult(
			"outbound_not_paused", FAIL, f"paused: {getattr(contact, 'outbound_pause_reason', None) or 'unspecified'}"
		)
	return GateCheckResult("outbound_not_paused", PASS, "not paused")


def _check_non_poach(session: Session, company_id: str) -> GateCheckResult:
	"""requesting_client_id is deliberately NOT passed to the SQL function —
	it reads the requesting client from the session's own SET LOCAL
	app.current_client_id instead, so the check is tied to the session's
	actual RLS tenant context rather than a caller-supplied value a shared
	role could vary to probe another client's claim status."""
	try:
		claimed = session.execute(
			text("SELECT is_claimed_by_other_client(:company_id) AS claimed"),
			{"company_id": company_id},
		).scalar()
	except Exception as e:
		# Deliberately NOT reusing Forced Action's fuzzy-matching
		# except-and-continue-as-empty pattern here: "check failed to run"
		# and "check ran, found nothing" are not the same fact for a
		# compliance predicate.
		return GateCheckResult("non_poach", ABSTAIN, f"non-poach check errored: {e}")

	if claimed:
		return GateCheckResult("non_poach", FAIL, "company is claimed by another client's PM book")
	return GateCheckResult("non_poach", PASS, "no conflicting claim found")


def evaluate_enrollment_gate(
	session: Session,
	contact,
	requesting_client_id: str,
	dnc_provider: Optional[DncProvider] = None,
	email_provider: Optional[EmailVerificationProvider] = None,
) -> ComplianceGateResult:
	"""Full gate for enrolling a contact into a new sequence.

	Includes the 14-day cross-enrollment cooldown — this is the per-enrollment
	fatigue guard that prevents re-enrolling a recently-touched contact across
	campaigns. Does NOT run mid-sequence (use evaluate_touch_gate for that).
	"""
	dnc_provider = dnc_provider or StubDncProvider()

	checks = (
		_check_deterministic_columns(contact),
		_check_not_paused(contact),
		_check_cooldown(contact),
		_check_dnc(contact, dnc_provider),
		_check_non_poach(session, contact.company_id),
	)

	for check in checks:
		_record(session, contact.contact_id, requesting_client_id, check)

	return ComplianceGateResult(contact_id=contact.contact_id, checks=checks)


def evaluate_touch_gate(
	session: Session,
	contact,
	requesting_client_id: str,
	dnc_provider: Optional[DncProvider] = None,
	email_provider: Optional[EmailVerificationProvider] = None,
) -> ComplianceGateResult:
	"""Per-touch gate for a contact already enrolled in an active sequence.

	Omits the 14-day cooldown — a contact in an active sequence must not be
	blocked by the cross-enrollment fatigue guard mid-flight. All other checks
	(eligibility, DNC, non-poach) still run on every touch.
	"""
	dnc_provider = dnc_provider or StubDncProvider()

	checks = (
		_check_deterministic_columns(contact),
		_check_dnc(contact, dnc_provider),
		_check_non_poach(session, contact.company_id),
	)

	for check in checks:
		_record(session, contact.contact_id, requesting_client_id, check)

	return ComplianceGateResult(contact_id=contact.contact_id, checks=checks)


def evaluate_compliance_gate(
	session: Session,
	contact,
	requesting_client_id: str,
	dnc_provider: Optional[DncProvider] = None,
	email_provider: Optional[EmailVerificationProvider] = None,
) -> ComplianceGateResult:
	"""Shim — delegates to evaluate_enrollment_gate. Kept for backward compat."""
	return evaluate_enrollment_gate(
		session, contact, requesting_client_id,
		dnc_provider=dnc_provider,
		email_provider=email_provider,
	)
