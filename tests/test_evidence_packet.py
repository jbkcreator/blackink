"""Pure(ish) tests for the Evidence Packet assembly + PDF render — a
FakeSession stands in for Postgres, no live DB required.

Asserts: a missing value renders as NOT RECORDED; the §3 qualification-gap
and §4 SYNTHETIC-NOT-PMS-VERIFIED strings are present when expected; and
compile_packet() returns real PDF bytes with all four section titles.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from src.services.settlement.evidence import assemble_evidence_packet
from src.services.settlement.evidence_pdf import compile_packet

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _txn_row(agreement_source="SYNTHETIC", last_verified_at=None):
	return SimpleNamespace(
		transaction_id=1, client_id="acme", company_id="co1", opportunity_id="opp-1", door_count=3,
		door_signed_at=_NOW, pms_agreement_id=1, pms_property_ref="prop-ref-1",
		agreement_source=agreement_source, agreement_status="ACTIVE",
		verified_at=None, last_verified_at=last_verified_at, terminated_at=None,
		company_name="Acme Property Management", domain="acme-pm.com", county_slug="hillsborough",
		company_status="active", owning_client_id="acme",
	)


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def first(self):
		return self._row

	def one(self):
		return self._row

	def all(self):
		return [self._row] if self._row else []


class _FakeSession:
	def __init__(self, txn_row):
		self._txn_row = txn_row

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "a.pms_property_ref, a.agreement_source" in sql:
			return _FakeResult(self._txn_row)
		# Every sub-query (raw_prospect_companies, county_allocations,
		# compliance_gate_checks, sequence_runs, bookings, appointments,
		# appointment_dispositions, appointment_disputes, client_pm_books)
		# returns nothing found — exercises the NOT RECORDED path.
		return _FakeResult(None)


def test_missing_fields_render_as_not_recorded_and_gaps_present():
	session = _FakeSession(_txn_row(agreement_source="SYNTHETIC", last_verified_at=None))
	packet = assemble_evidence_packet(session, transaction_id=1, installment=1)

	assert len(packet.sections) == 4
	titles = [s.title for s in packet.sections]
	assert titles == [
		"Source & Outreach Lineage",
		"Engagement & Booking Record",
		"Meeting & Qualification Verification",
		"PMS Contract Verification",
	]

	section3 = packet.sections[2]
	assert any("4-rule qualification check" in gap for gap in section3.gaps)
	# A field with no matching row must render NOT RECORDED, never blank.
	appt_state_field = next(f for f in section3.fields if f.label == "Appointment state")
	assert appt_state_field.value is None  # renders as NOT RECORDED in the PDF

	section4 = packet.sections[3]
	assert any("SYNTHETIC - NOT PMS-VERIFIED" in gap for gap in section4.gaps)
	assert any("COULD NOT DETERMINE" in gap for gap in section4.gaps)

	assert "Meeting & Qualification Verification" in packet.sections_with_gaps
	assert "PMS Contract Verification" in packet.sections_with_gaps


def test_pms_sync_agreement_has_no_synthetic_gap():
	session = _FakeSession(_txn_row(agreement_source="PMS_SYNC", last_verified_at=_NOW))
	packet = assemble_evidence_packet(session, transaction_id=1, installment=2)
	section4 = packet.sections[3]
	assert not any("SYNTHETIC" in gap for gap in section4.gaps)
	assert not any("COULD NOT DETERMINE" in gap for gap in section4.gaps)


def test_compile_packet_returns_real_pdf_bytes():
	session = _FakeSession(_txn_row())
	packet = assemble_evidence_packet(session, transaction_id=1, installment=1)
	pdf_bytes = compile_packet(packet)
	assert pdf_bytes.startswith(b"%PDF")
	assert len(pdf_bytes) > 0
