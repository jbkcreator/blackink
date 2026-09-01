"""Staging quarantine-gate predicates for raw_prospect_companies/_contacts.

Distinct from src/services/compliance_gate.py: that gate governs whether an
already-promoted contact is eligible for a SEND on a given campaign attempt
(PASS/FAIL/ABSTAIN, per-requesting-client). This module governs whether a
freshly-ingested Akrash row is eligible for PROMOTION into the canonical
companies/contacts tables at all (pending/cleared/quarantined/rejected, no
requesting client yet — ownership is assigned later via county_allocations).
The two share the DNC/email-verification provider interfaces to avoid
duplicate vendor-integration code, since the underlying question (is this
contact real and legal to contact) is the same one asked at two different
lifecycle stages.

Reject-reason-code vocabulary (Dev 1 plan): EMAIL_INVALID_SYNTAX,
EMAIL_HARD_BOUNCE, EMAIL_VERIFY_TIMEOUT (quarantine, retryable), DNC_LISTED,
DNC_VERIFY_TIMEOUT (quarantine, retryable), GLOBAL_OPT_OUT,
NONPOACH_CONFLICT, MISSING_REQUIRED_FIELD:<field>, DUPLICATE_COMPANY
(quarantine — fuzzy name match on a different domain, needs human review).
Nothing is dropped silently: every row that doesn't reach 'cleared' carries
exactly one reason code.
"""

from dataclasses import dataclass
from typing import Optional

from email_validator import validate_email, EmailNotValidError
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.compliance_gate import DncProvider, EmailVerificationProvider, StubDncProvider, StubEmailVerificationProvider

CLEARED = "cleared"
QUARANTINED = "quarantined"
REJECTED = "rejected"


@dataclass(frozen=True)
class QuarantineVerdict:
	status: str  # cleared | quarantined | rejected
	reason_code: Optional[str] = None


def evaluate_contact(
	email: Optional[str],
	phone: Optional[str],
	session: Session,
	dnc_provider: Optional[DncProvider] = None,
	email_provider: Optional[EmailVerificationProvider] = None,
) -> QuarantineVerdict:
	"""Predicates 1 (email verification), 2 (DNC clearance), 3 (global
	opt-out) for a single contact row."""
	dnc_provider = dnc_provider or StubDncProvider()
	email_provider = email_provider or StubEmailVerificationProvider()

	if not email:
		return QuarantineVerdict(REJECTED, "MISSING_REQUIRED_FIELD:email")

	try:
		validate_email(email, check_deliverability=False)
	except EmailNotValidError:
		return QuarantineVerdict(REJECTED, "EMAIL_INVALID_SYNTAX")

	verdict = email_provider.verify(email)
	if verdict == "invalid":
		return QuarantineVerdict(REJECTED, "EMAIL_HARD_BOUNCE")
	if verdict == "unknown":
		return QuarantineVerdict(QUARANTINED, "EMAIL_VERIFY_TIMEOUT")

	if phone:
		dnc_result = dnc_provider.check(phone)
		if dnc_result is True:
			return QuarantineVerdict(REJECTED, "DNC_LISTED")
		if dnc_result is None:
			return QuarantineVerdict(QUARANTINED, "DNC_VERIFY_TIMEOUT")

	existing = session.execute(
		text(
			"SELECT 1 FROM contacts WHERE (email = :email OR (phone IS NOT NULL AND phone = :phone)) "
			"AND (is_opted_out OR suppression_state) LIMIT 1"
		),
		{"email": email, "phone": phone},
	).first()
	if existing:
		return QuarantineVerdict(REJECTED, "GLOBAL_OPT_OUT")

	return QuarantineVerdict(CLEARED)


def evaluate_company_non_poach(session: Session, domain: str) -> QuarantineVerdict:
	"""Predicate 4: a domain already on ANY client's PM book is already a
	managed relationship elsewhere and must never re-enter the prospect
	pool — no requesting-client concept at staging time (ownership is
	assigned later at promotion via county_allocations)."""
	claimed = session.execute(
		text("SELECT 1 FROM client_pm_books WHERE owner_domain = :domain LIMIT 1"), {"domain": domain}
	).first()
	if claimed:
		return QuarantineVerdict(REJECTED, "NONPOACH_CONFLICT")
	return QuarantineVerdict(CLEARED)
