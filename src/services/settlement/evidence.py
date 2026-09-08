"""Evidence Packet data assembly (Subtask 1.2.2) — DB reads only, no I/O.

Assembles the 4 sections the blueprint requires (Source & Outreach
Lineage / Engagement & Booking Record / Meeting & Qualification
Verification / PMS Contract Verification) from whatever this repo's
tables genuinely contain today. A missing value renders as NOT RECORDED
with its source table printed underneath — never a blank, never a
plausible-looking default. Real data gaps are stated verbatim in `gaps`
rather than papered over; see evidence_pdf.py for how these render.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class EvidenceField:
	label: str
	value: Optional[str]
	source_table: str


@dataclass(frozen=True)
class EvidenceSection:
	number: int
	title: str
	fields: tuple[EvidenceField, ...]
	gaps: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EvidencePacketData:
	transaction_id: int
	installment: int
	company_name: Optional[str]
	sections: tuple[EvidenceSection, ...]

	@property
	def sections_with_gaps(self) -> list[str]:
		return [s.title for s in self.sections if s.gaps]


def _f(label: str, value, source_table: str) -> EvidenceField:
	return EvidenceField(label=label, value=None if value is None else str(value), source_table=source_table)


def assemble_evidence_packet(session: Session, *, transaction_id: int, installment: int) -> EvidencePacketData:
	txn = session.execute(
		text(
			"SELECT t.transaction_id, t.client_id, t.company_id, t.opportunity_id, t.door_count, "
			"       t.door_signed_at, a.pms_agreement_id, a.pms_property_ref, a.agreement_source, "
			"       a.status AS agreement_status, a.verified_at, a.last_verified_at, a.terminated_at, "
			"       c.company_name, c.domain, c.county_slug, c.status AS company_status, "
			"       c.owning_client_id "
			"FROM settlement_transactions t "
			"JOIN pms_agreements a ON a.pms_agreement_id = t.pms_agreement_id "
			"LEFT JOIN companies c ON c.company_id = t.company_id "
			"WHERE t.transaction_id = :transaction_id"
		),
		{"transaction_id": transaction_id},
	).one()

	sections = (
		_section_1_lineage(session, txn),
		_section_2_engagement(session, txn),
		_section_3_meeting(session, txn),
		_section_4_pms(session, txn),
	)
	return EvidencePacketData(
		transaction_id=transaction_id, installment=installment,
		company_name=txn.company_name, sections=sections,
	)


def _section_1_lineage(session: Session, txn) -> EvidenceSection:
	raw = session.execute(
		text(
			"SELECT source_channel, source_timestamp, submitted_by, enrichment_provider, "
			"       enrichment_timestamp, validation_status, door_count_est, door_count_source "
			"FROM raw_prospect_companies WHERE company_id = :company_id "
			"ORDER BY id DESC LIMIT 1"
		),
		{"company_id": txn.company_id},
	).first()
	allocation = session.execute(
		text(
			"SELECT allocated_at, allocation_reason FROM county_allocations "
			"WHERE county_slug = :county_slug AND superseded_at IS NULL"
		),
		{"county_slug": txn.county_slug},
	).first()
	dnc_check = session.execute(
		text(
			"SELECT status FROM compliance_gate_checks cgc "
			"JOIN contacts ct ON ct.contact_id = cgc.contact_id "
			"WHERE ct.company_id = :company_id AND cgc.check_name = 'dnc_clean' "
			"ORDER BY cgc.checked_at DESC LIMIT 1"
		),
		{"company_id": txn.company_id},
	).first()

	fields = [
		_f("Source channel", raw.source_channel if raw else None, "raw_prospect_companies"),
		_f("Source timestamp", raw.source_timestamp if raw else None, "raw_prospect_companies"),
		_f("Enrichment provider", raw.enrichment_provider if raw else None, "raw_prospect_companies"),
		_f("Validation status", raw.validation_status if raw else None, "raw_prospect_companies"),
		_f("Door count (estimated)", raw.door_count_est if raw else None, "raw_prospect_companies"),
		_f("County allocated at", allocation.allocated_at if allocation else None, "county_allocations"),
		_f("Allocation reason", allocation.allocation_reason if allocation else None, "county_allocations"),
		_f(
			"DNC clean check",
			(dnc_check.status if dnc_check else "ABSTAIN - no vendor contracted"),
			"compliance_gate_checks",
		),
	]
	gaps = []
	if not dnc_check or dnc_check.status == "ABSTAIN":
		gaps.append("ABSTAIN - no DNC vendor contracted; this is not a clean-list confirmation.")
	return EvidenceSection(number=1, title="Source & Outreach Lineage", fields=tuple(fields), gaps=tuple(gaps))


def _section_2_engagement(session: Session, txn) -> EvidenceSection:
	touches = session.execute(
		text(
			"SELECT COUNT(*) AS n, MAX(sr.updated_at) AS last_touch FROM sequence_runs sr "
			"JOIN contacts ct ON ct.contact_id = sr.contact_id "
			"WHERE ct.company_id = :company_id"
		),
		{"company_id": txn.company_id},
	).first()
	booking = session.execute(
		text(
			"SELECT provider, external_event_id, scheduled_at, status, client_rep_name, client_rep_email "
			"FROM bookings WHERE target_company_id = :company_id ORDER BY scheduled_at DESC LIMIT 1"
		),
		{"company_id": txn.company_id},
	).first()

	fields = [
		_f("Sequence touches recorded", touches.n if touches else None, "sequence_runs"),
		_f("Last touch", touches.last_touch if touches else None, "sequence_runs"),
		_f("Booking provider", booking.provider if booking else None, "bookings"),
		_f("Booking scheduled at", booking.scheduled_at if booking else None, "bookings"),
		_f("Booking status", booking.status if booking else None, "bookings"),
		_f("Assigned rep", booking.client_rep_name if booking else None, "bookings"),
	]
	gaps = ["DATA GAP: open/click/reply tracking not instrumented - dispatch records only."]
	return EvidenceSection(number=2, title="Engagement & Booking Record", fields=tuple(fields), gaps=tuple(gaps))


def _section_3_meeting(session: Session, txn) -> EvidenceSection:
	appt = session.execute(
		text(
			"SELECT appointment_id, state, scheduled_for, attended_at, confirmed_24h_timestamp, "
			"       confirmed_3h_timestamp, is_billable, reschedule_count "
			"FROM appointments WHERE company_id = :company_id ORDER BY scheduled_for DESC LIMIT 1"
		),
		{"company_id": txn.company_id},
	).first()
	disposition = None
	dispute = None
	if appt:
		disposition = session.execute(
			text("SELECT outcome, doors_signed, close_reason, brief_accurate FROM appointment_dispositions "
				 "WHERE appointment_id = :appointment_id"),
			{"appointment_id": appt.appointment_id},
		).first()
		dispute = session.execute(
			text("SELECT flagged_at, outcome FROM appointment_disputes WHERE appointment_id = :appointment_id"),
			{"appointment_id": appt.appointment_id},
		).first()

	fields = [
		_f("Appointment state", appt.state if appt else None, "appointments"),
		_f("Attended at", appt.attended_at if appt else None, "appointments"),
		_f("24h confirmation", appt.confirmed_24h_timestamp if appt else None, "appointments"),
		_f("3h confirmation", appt.confirmed_3h_timestamp if appt else None, "appointments"),
		_f("Billing-gate schema flag (is_billable)", appt.is_billable if appt else None, "appointments"),
		_f("Reschedule count", appt.reschedule_count if appt else None, "appointments"),
		_f("Disposition outcome", disposition.outcome if disposition else None, "appointment_dispositions"),
		_f("Doors signed (disposition)", disposition.doors_signed if disposition else None, "appointment_dispositions"),
		_f("Disputed", ("YES - flagged at " + str(dispute.flagged_at)) if dispute else "No dispute on record", "appointment_disputes"),
	]
	gaps = [
		"DATA GAP: 4-rule qualification check (ownership / intent / ICP / duration) not "
		"implemented - this packet asserts the two-tier confirmation + ATTENDED state only."
	]
	return EvidenceSection(number=3, title="Meeting & Qualification Verification", fields=tuple(fields), gaps=tuple(gaps))


def _section_4_pms(session: Session, txn) -> EvidenceSection:
	claim = session.execute(
		text(
			"SELECT synced_at FROM client_pm_books WHERE client_id = :client_id "
			"AND (owner_domain = :domain OR owner_domain IS NULL) ORDER BY synced_at DESC LIMIT 1"
		),
		{"client_id": txn.client_id, "domain": txn.domain},
	).first()

	fields = [
		_f("PMS agreement door_signed_at", txn.door_signed_at, "pms_agreements"),
		_f("Door count", txn.door_count, "pms_agreements"),
		_f("PMS property reference", txn.pms_property_ref, "pms_agreements"),
		_f("Agreement source", txn.agreement_source, "pms_agreements"),
		_f("Last PMS re-verification", txn.last_verified_at, "pms_agreements"),
		_f("Terminated at", txn.terminated_at, "pms_agreements"),
		_f("Non-poach claim synced at", claim.synced_at if claim else None, "client_pm_books"),
	]
	gaps = []
	if txn.agreement_source != "PMS_SYNC":
		gaps.append(
			f"SOURCE: {txn.agreement_source} - NOT PMS-VERIFIED. DATA GAP: nightly PMS read-sync "
			"not implemented; no properties table exists in this schema."
		)
	if txn.last_verified_at is None:
		gaps.append("PMS RE-VERIFICATION: COULD NOT DETERMINE.")
	return EvidenceSection(number=4, title="PMS Contract Verification", fields=tuple(fields), gaps=tuple(gaps))
