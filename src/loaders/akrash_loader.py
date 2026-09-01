"""AkrashProspectLoader — writes a raw Akrash payload into the staging
tables, validated but not yet promoted.

Ingest-time validation is deliberately shallow (required-field presence
only) — the real predicates (email verification, DNC, opt-out, non-poach)
run in the promotion sweep (src/tasks/promotion_sweep.py), because the
blueprint's actual ingestion path is direct restricted database write
access, not a synchronous API round-trip (Dev 1 plan §Key decision 6).
Nothing is dropped at this layer: a row missing a required field is still
inserted, tagged 'rejected' with a reason code, never silently discarded
before insert.
"""

from typing import Any, Dict, List, Optional

from src.core.models import RawProspectCompany, RawProspectContact
from src.loaders.base import BaseIngestLoader

REQUIRED_COMPANY_FIELDS = ("company_name", "domain")


class AkrashProspectLoader(BaseIngestLoader):
	def ingest_row(self, raw_row: Dict[str, Any]) -> RawProspectCompany:
		"""raw_row shape: {company_name, domain, county_slug, door_count_est,
		door_count_source, source_channel, source_timestamp, submitted_by,
		enrichment_provider, enrichment_timestamp,
		contacts: [{role, name, email, phone, source}, ...]}"""
		missing = [f for f in REQUIRED_COMPANY_FIELDS if not raw_row.get(f)]

		domain = raw_row.get("domain") or ""
		company_id = self.compute_company_id(domain) if domain else ""

		company = RawProspectCompany(
			company_id=company_id,
			company_name=raw_row.get("company_name") or "",
			domain=domain,
			county_slug=raw_row.get("county_slug"),
			door_count_est=raw_row.get("door_count_est"),
			door_count_source=raw_row.get("door_count_source"),
			source_channel=raw_row.get("source_channel"),
			source_timestamp=raw_row.get("source_timestamp"),
			submitted_by=raw_row.get("submitted_by") or "unknown",
			enrichment_provider=raw_row.get("enrichment_provider"),
			enrichment_timestamp=raw_row.get("enrichment_timestamp"),
			raw_payload=raw_row,
			validation_status="rejected" if missing else "pending",
			reject_reason_code=f"MISSING_REQUIRED_FIELD:{missing[0]}" if missing else None,
		)
		if not self.safe_add(company):
			return company

		for contact in raw_row.get("contacts", []):
			self._ingest_contact(company, contact)

		return company

	def _ingest_contact(self, company: RawProspectCompany, contact: Dict[str, Any]) -> Optional[RawProspectContact]:
		role = contact.get("role")
		missing_role = role not in ("OWNER_BROKER_MD", "OFFICE_MANAGER_OPS")
		record = RawProspectContact(
			company_ref_id=company.id,
			role=role or "OWNER_BROKER_MD",
			name=contact.get("name"),
			email=contact.get("email"),
			phone=contact.get("phone"),
			source=contact.get("source"),
			validation_status="rejected" if missing_role else "pending",
			reject_reason_code="MISSING_REQUIRED_FIELD:role" if missing_role else None,
		)
		self.safe_add(record)
		return record

	def ingest_batch(self, raw_rows: List[Dict[str, Any]]) -> List[RawProspectCompany]:
		return [self.ingest_row(row) for row in raw_rows]
