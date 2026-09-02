"""Promotion sweep — the async job that satisfies "rejected rows return
with a reason code, nothing dropped silently" (W1-1) against a direct
restricted-DB-write ingestion path (Dev 1 plan §Key decision 6).

Runs frequently (minutes, not nightly — see Dev 1 plan's build order).
Uses the BYPASSRLS system session, since promotion happens BEFORE any
client ownership is assigned (company.owning_client_id starts NULL,
populated later by county-allocation logic) — there is no tenant context
to scope this job under.

Per sweep, for every 'pending' or 'quarantined' raw_prospect_companies row:
  1. Re-validate required fields.
  2. Company-level non-poach check (evaluate_company_non_poach).
  3. Dedup-on-ingest: exact domain match -> merge into the existing
     Company (safe, no judgement call — domain is the deterministic
     identity key). Near-duplicate normalized-name match on a DIFFERENT
     domain -> quarantine with DUPLICATE_COMPANY for human review (not
     auto-merged — a judgement call).
  4. Create the canonical Company row if none exists, with the entity_type
     heuristic populated.
  5. Contact completeness gate (Week 1 Subtask 1.1.3): both OWNER_BROKER_MD
     and OFFICE_MANAGER_OPS must be present in staging with first_name,
     last_name, title, email, and phone all populated, or BOTH contacts are
     skipped together with reason code CONTACT_INCOMPLETE_SKIPPED — a joint
     check, run before step 6.
  6. For every 'pending'/'quarantined' contact linked to this company (once
     step 5 passes), run the quarantine-gate contact predicates and promote
     the ones that clear — including promoting the company with only ONE
     clean contact if the other fails compliance (confirmed behavior),
     never holding the whole company back for one bad contact. A contact
     whose email already exists on another promoted contact is skipped
     with reason code DUPLICATE_CONTACT_EMAIL rather than raising.

Idempotent per row: a contact already promoted (matched by company_id +
role in the canonical `contacts` table) is not re-inserted.

CLI: `python -m src.tasks.promotion_sweep [--batch-size N]` (default:
unbounded, same as the docker-compose scheduled-loop invocation). Each run
logs and returns a structured summary — read/inserted/duplicate_skipped/
quarantined/rejected/failed counts (Week 1 Subtask 1.1.2 DoD). An exact
domain match on an existing Company logs a `duplicate_domain_skipped` event
instead of a fatal error and still promotes the row into that company.
"""

import json
import logging
from collections import Counter
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.database import get_system_db_context
from src.core.models import Company, Contact, RawProspectCompany
from src.loaders.base import BaseIngestLoader
from src.services import quarantine_gate

logger = logging.getLogger(__name__)

_COMPANY_NAME_FUZZY_MATCH_THRESHOLD = 90  # rapidfuzz token_sort_ratio, 0-100

# Week 1 Subtask 1.1.3: a promoted company must resolve exactly these two
# contact roles, each with every one of these fields populated in staging —
# missing either fails BOTH contacts for that promotion round (a joint
# completeness check, distinct from the per-contact DNC/verification
# compliance check that runs afterward and can still promote just one).
_REQUIRED_CONTACT_FIELDS = ("first_name", "last_name", "title", "email", "phone")
_EXPECTED_CONTACT_ROLES = ("OWNER_BROKER_MD", "OFFICE_MANAGER_OPS")


def _find_existing_company_by_domain(session: Session, domain: str) -> Optional[Company]:
	return session.query(Company).filter_by(domain=domain).first()


def _find_fuzzy_duplicate_company(session: Session, normalized_name: str, domain: str) -> Optional[Company]:
	"""Near-duplicate normalized-name match on a DIFFERENT domain — a
	judgement call, not auto-merged. Bounded scan (name isn't indexed for
	fuzzy search here; fine at Dev 1's scale, revisit with pg_trgm if the
	companies table grows large)."""
	try:
		from rapidfuzz import fuzz
	except ImportError:
		return None

	candidates = session.query(Company).filter(Company.domain != domain).limit(5000).all()
	for candidate in candidates:
		candidate_normalized = BaseIngestLoader.normalize_company_name(candidate.company_name)
		if not candidate_normalized:
			continue
		score = fuzz.token_sort_ratio(normalized_name, candidate_normalized)
		if score >= _COMPANY_NAME_FUZZY_MATCH_THRESHOLD:
			return candidate
	return None


def _classify_entity_type(company_name: str, door_count_est: Optional[int]) -> str:
	has_llc_suffix = BaseIngestLoader.normalize_company_name(company_name) != company_name.upper().strip()
	if (door_count_est or 0) > 1 and has_llc_suffix:
		return "llc_portfolio_owner"
	return "single_property_owner"


def _reject_company(session: Session, raw_company, reason_code: str) -> None:
	session.execute(
		text(
			"UPDATE raw_prospect_companies SET validation_status = 'rejected', "
			"reject_reason_code = :reason WHERE id = :id"
		),
		{"reason": reason_code, "id": raw_company.id},
	)
	session.execute(
		text(
			"UPDATE raw_prospect_contacts SET validation_status = 'rejected', "
			"reject_reason_code = 'COMPANY_REJECTED' "
			"WHERE company_ref_id = :id AND validation_status IN ('pending','quarantined')"
		),
		{"id": raw_company.id},
	)


def _quarantine_company(session: Session, raw_company, reason_code: str) -> None:
	session.execute(
		text(
			"UPDATE raw_prospect_companies SET validation_status = 'quarantined', "
			"reject_reason_code = :reason WHERE id = :id"
		),
		{"reason": reason_code, "id": raw_company.id},
	)


def _promote_company_row(session: Session, raw_company, resolved_company_id: str) -> None:
	session.execute(
		text(
			"UPDATE raw_prospect_companies SET validation_status = 'cleared', reject_reason_code = NULL, "
			"promoted_at = NOW(), promoted_company_id = :cid WHERE id = :id"
		),
		{"cid": resolved_company_id, "id": raw_company.id},
	)


def _process_company(session: Session, raw_company) -> str:
	"""Returns one of: inserted, duplicate_skipped, quarantined, rejected."""
	if not raw_company.company_name or not raw_company.domain:
		_reject_company(session, raw_company, "MISSING_REQUIRED_FIELD:company_name_or_domain")
		return "rejected"

	normalized_domain = BaseIngestLoader.normalize_domain(raw_company.domain)
	if not normalized_domain:
		_reject_company(session, raw_company, "MISSING_REQUIRED_FIELD:company_name_or_domain")
		return "rejected"

	if not raw_company.county_slug:
		_quarantine_company(session, raw_company, "MISSING_REQUIRED_FIELD:county_slug")
		return "quarantined"

	non_poach = quarantine_gate.evaluate_company_non_poach(session, normalized_domain)
	if non_poach.status == quarantine_gate.REJECTED:
		_reject_company(session, raw_company, non_poach.reason_code)
		return "rejected"

	existing = _find_existing_company_by_domain(session, normalized_domain)
	if existing is not None:
		resolved_company = existing
		session.execute(
			text(
				"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
				"VALUES ('_platform_internal', 'duplicate_domain_skipped', 'company', :entity_id, :payload)"
			),
			{
				"entity_id": existing.company_id,
				"payload": json.dumps({"raw_prospect_id": raw_company.id, "submitted_domain": raw_company.domain}),
			},
		)
		outcome = "duplicate_skipped"
	else:
		normalized_name = BaseIngestLoader.normalize_company_name(raw_company.company_name)
		fuzzy_match = _find_fuzzy_duplicate_company(session, normalized_name, normalized_domain)
		if fuzzy_match is not None:
			_quarantine_company(session, raw_company, "DUPLICATE_COMPANY")
			return "quarantined"

		resolved_company = Company(
			company_id=raw_company.company_id,
			company_name=raw_company.company_name,
			domain=normalized_domain,
			county_slug=raw_company.county_slug,
			door_count_est=raw_company.door_count_est,
			entity_type=_classify_entity_type(raw_company.company_name, raw_company.door_count_est),
			status="PROSPECTING",
		)
		session.add(resolved_company)
		session.flush()
		session.execute(
			text(
				"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
				"VALUES ('_platform_internal', 'company_promoted', 'company', :entity_id, :payload)"
			),
			{"entity_id": resolved_company.company_id, "payload": "{}"},
		)
		outcome = "inserted"

	_promote_company_row(session, raw_company, resolved_company.company_id)
	_process_contacts_for_company(session, raw_company, resolved_company)
	return outcome


def _contacts_are_complete(raw_contacts_by_role: dict) -> bool:
	"""True iff both expected roles are present and every required field is
	populated on each — a joint check, not a per-contact one (see
	_REQUIRED_CONTACT_FIELDS docstring at module scope)."""
	for role in _EXPECTED_CONTACT_ROLES:
		rc = raw_contacts_by_role.get(role)
		if rc is None:
			return False
		if any(not getattr(rc, field) for field in _REQUIRED_CONTACT_FIELDS):
			return False
	return True


def _process_contacts_for_company(session: Session, raw_company, company: Company) -> None:
	raw_contacts = session.execute(
		text(
			"SELECT id, role, first_name, last_name, title, email, phone FROM raw_prospect_contacts "
			"WHERE company_ref_id = :id AND validation_status IN ('pending','quarantined')"
		),
		{"id": raw_company.id},
	).fetchall()

	raw_contacts_by_role = {rc.role: rc for rc in raw_contacts}
	if not _contacts_are_complete(raw_contacts_by_role):
		for rc in raw_contacts:
			session.execute(
				text(
					"UPDATE raw_prospect_contacts SET validation_status = 'rejected', "
					"reject_reason_code = 'CONTACT_INCOMPLETE_SKIPPED' WHERE id = :id"
				),
				{"id": rc.id},
			)
		return

	for rc in raw_contacts:
		already_promoted = (
			session.query(Contact)
			.filter_by(company_id=company.company_id, contact_role_type=rc.role)
			.first()
		)
		if already_promoted is not None:
			session.execute(
				text(
					"UPDATE raw_prospect_contacts SET validation_status = 'cleared', reject_reason_code = NULL "
					"WHERE id = :id"
				),
				{"id": rc.id},
			)
			continue

		verdict = quarantine_gate.evaluate_contact(rc.email, rc.phone, session)
		if verdict.status == quarantine_gate.REJECTED:
			session.execute(
				text(
					"UPDATE raw_prospect_contacts SET validation_status = 'rejected', reject_reason_code = :r "
					"WHERE id = :id"
				),
				{"r": verdict.reason_code, "id": rc.id},
			)
			continue
		if verdict.status == quarantine_gate.QUARANTINED:
			session.execute(
				text(
					"UPDATE raw_prospect_contacts SET validation_status = 'quarantined', reject_reason_code = :r "
					"WHERE id = :id"
				),
				{"r": verdict.reason_code, "id": rc.id},
			)
			continue

		duplicate_email = session.query(Contact).filter_by(email=rc.email).first()
		if duplicate_email is not None:
			session.execute(
				text(
					"UPDATE raw_prospect_contacts SET validation_status = 'rejected', "
					"reject_reason_code = 'DUPLICATE_CONTACT_EMAIL' WHERE id = :id"
				),
				{"id": rc.id},
			)
			continue

		contact = Contact(
			company_id=company.company_id,
			contact_role_type=rc.role,
			first_name=rc.first_name,
			last_name=rc.last_name,
			title=rc.title,
			email=rc.email,
			email_status="UNVERIFIED",
			phone=rc.phone,
		)
		if not session.query(Contact).filter_by(
			company_id=company.company_id, contact_role_type=rc.role
		).first():
			session.add(contact)
			session.flush()
		session.execute(
			text(
				"UPDATE raw_prospect_contacts SET validation_status = 'cleared', reject_reason_code = NULL "
				"WHERE id = :id"
			),
			{"id": rc.id},
		)


def run_promotion_sweep(batch_size: Optional[int] = None) -> dict:
	"""Returns a summary dict: {read, inserted, duplicate_skipped, quarantined, rejected, failed}."""
	counts: Counter = Counter()
	query = "SELECT id FROM raw_prospect_companies WHERE validation_status IN ('pending','quarantined') ORDER BY id"
	if batch_size is not None:
		query += " LIMIT :batch_size"
	with get_system_db_context() as session:
		params = {"batch_size": batch_size} if batch_size is not None else {}
		pending = session.execute(text(query), params).fetchall()
		for row in pending:
			counts["read"] += 1
			raw_company = session.get(RawProspectCompany, row.id)
			try:
				outcome = _process_company(session, raw_company)
				counts[outcome] += 1
			except Exception:
				counts["failed"] += 1
				logger.exception("promotion_sweep: error processing raw_prospect_companies.id=%s", row.id)
	summary = {
		"read": counts["read"],
		"inserted": counts["inserted"],
		"duplicate_skipped": counts["duplicate_skipped"],
		"quarantined": counts["quarantined"],
		"rejected": counts["rejected"],
		"failed": counts["failed"],
	}
	logger.info(
		"promotion_sweep: read=%d inserted=%d duplicate_skipped=%d quarantined=%d rejected=%d failed=%d",
		summary["read"], summary["inserted"], summary["duplicate_skipped"],
		summary["quarantined"], summary["rejected"], summary["failed"],
	)
	return summary


if __name__ == "__main__":
	import argparse

	parser = argparse.ArgumentParser(description="Promote cleared raw_prospect_* rows into companies/contacts.")
	parser.add_argument("--batch-size", type=int, default=None, help="Max rows to process this run (default: unbounded)")
	args = parser.parse_args()

	logging.basicConfig(level=logging.INFO)
	run_promotion_sweep(batch_size=args.batch_size)
