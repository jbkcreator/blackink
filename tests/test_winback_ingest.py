"""Unit tests for src/services/winback_ingest.py's pure/branchy logic —
the disposition matrix, CSV validation, address normalization, and the
FakeSession-backed lookup helpers (assessor match, non-poach check, county
resolution). No live DB or network call.

Full pipeline orchestration (run_import) touches many SQL statements in an
order that depends on control flow — exercising it faithfully needs a real
Postgres, same posture as tests/test_tenant_isolation.py. This suite covers
the actual business logic the plan doc's disposition matrix depends on,
which is where a real bug would live.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.services.winback_ingest import (
	SOLD,
	STILL_OWNS_NOT_RENTING,
	STILL_OWNS_STILL_RENTING,
	UNKNOWN,
	ParsedRow,
	StagingTableAssessorProvider,
	StubFrboProvider,
	_check_non_poach,
	_csv_safe,
	_email_domain,
	_process_row,
	_resolve_county_slug,
	compute_disposition,
	export_csv,
	normalize_address,
	parse_csv,
)


# ---------------------------------------------------------------------------
# compute_disposition — the disposition matrix from the plan doc
# ---------------------------------------------------------------------------

def test_disposition_still_owns_still_renting():
	assert compute_disposition(True, True) == STILL_OWNS_STILL_RENTING


def test_disposition_still_owns_not_renting():
	assert compute_disposition(True, False) == STILL_OWNS_NOT_RENTING


def test_disposition_name_mismatch_is_sold():
	assert compute_disposition(False, None) == SOLD


def test_disposition_sold_regardless_of_renting_status():
	# still_owns=False (confirmed sale) always wins, even if still_renting
	# somehow came back True (shouldn't happen given run_import only checks
	# FRBO when still_owns is True, but the pure function must be correct
	# on its own regardless of caller discipline).
	assert compute_disposition(False, True) == SOLD


def test_disposition_parcel_not_found_is_unknown_not_sold():
	# The documented deviation from the spec's literal wording — a data
	# coverage gap must never silently suppress a real lead forever.
	assert compute_disposition(None, None) == UNKNOWN


def test_disposition_still_owns_true_but_frbo_unresolved_is_unknown():
	# The direct consequence of shipping with StubFrboProvider — never
	# defaulted to NOT_RENTING.
	assert compute_disposition(True, None) == UNKNOWN


# ---------------------------------------------------------------------------
# normalize_address
# ---------------------------------------------------------------------------

def test_normalize_address_strips_punctuation_and_case():
	assert normalize_address("123 Main St., Apt #4") == "123 MAIN ST APT 4"


def test_normalize_address_collapses_whitespace():
	assert normalize_address("123   Main   St") == "123 MAIN ST"


def test_normalize_address_empty():
	assert normalize_address("") == ""
	assert normalize_address(None) == ""


# ---------------------------------------------------------------------------
# _email_domain
# ---------------------------------------------------------------------------

def test_email_domain_extracts_lowercase_domain():
	assert _email_domain("Jane@Example.COM") == "example.com"


def test_email_domain_none_for_missing_or_malformed():
	assert _email_domain(None) is None
	assert _email_domain("") is None
	assert _email_domain("not-an-email") is None


# ---------------------------------------------------------------------------
# parse_csv
# ---------------------------------------------------------------------------

_VALID_CSV = (
	"owner_name,property_address,county,phone,email\n"
	"Jane Doe,123 Main St,hillsborough_fl,8135550100,jane@example.com\n"
)

_MISSING_COLUMN_CSV = (
	"owner_name,property_address,county,phone,email\n"
	",123 Main St,hillsborough_fl,8135550100,jane@example.com\n"
)


def test_parse_csv_valid_row_has_no_validation_error():
	rows = parse_csv(_VALID_CSV)
	assert len(rows) == 1
	assert rows[0].validation_error is None
	assert rows[0].owner_name == "Jane Doe"
	assert rows[0].county_input == "hillsborough_fl"


def test_parse_csv_flags_missing_required_column():
	rows = parse_csv(_MISSING_COLUMN_CSV)
	assert len(rows) == 1
	assert rows[0].validation_error is not None
	assert "owner_name" in rows[0].validation_error


def test_parse_csv_empty_phone_and_email_flagged_as_missing():
	# Phone and email are BOTH in the spec's own "minimum required columns"
	# list (Week2_Tasks_Dev_Split_v1.md:236) — an empty value for either is
	# a validation failure, not a silently-accepted optional field.
	csv_text = "owner_name,property_address,county,phone,email\nJane Doe,123 Main St,hillsborough_fl,,\n"
	rows = parse_csv(csv_text)
	assert rows[0].phone is None
	assert rows[0].email is None
	assert rows[0].validation_error is not None
	assert "phone" in rows[0].validation_error and "email" in rows[0].validation_error


# ---------------------------------------------------------------------------
# StubFrboProvider — always UNKNOWN, the documented open blocker
# ---------------------------------------------------------------------------

def test_stub_frbo_provider_always_returns_none():
	provider = StubFrboProvider()
	assert provider.check_active_listing("123 Main St") is None


# ---------------------------------------------------------------------------
# StagingTableAssessorProvider — FakeSession, no live DB
# ---------------------------------------------------------------------------

class _FakeResult:
	def __init__(self, row):
		self._row = row

	def fetchone(self):
		return self._row


class _FakeSession:
	def __init__(self, row):
		self._row = row

	def execute(self, *_args, **_kwargs):
		return _FakeResult(self._row)


def test_assessor_still_owns_true_on_name_match():
	session = _FakeSession(("Jane Doe",))
	provider = StagingTableAssessorProvider(session)
	assert provider.check_still_owns("hillsborough_fl", "123 Main St", "Jane Doe") is True


def test_assessor_still_owns_false_on_name_mismatch():
	session = _FakeSession(("Totally Different Person",))
	provider = StagingTableAssessorProvider(session)
	assert provider.check_still_owns("hillsborough_fl", "123 Main St", "Jane Doe") is False


def test_assessor_still_owns_none_when_parcel_not_found():
	session = _FakeSession(None)
	provider = StagingTableAssessorProvider(session)
	assert provider.check_still_owns("hillsborough_fl", "999 Nowhere Rd", "Jane Doe") is None


def test_assessor_tolerates_minor_name_variation():
	# rapidfuzz token_sort_ratio should treat "Jane A. Doe" and "Jane Doe"
	# as the same owner (matches dbpr_licence.py's own documented rationale
	# for a fuzzy, not exact, match).
	session = _FakeSession(("Jane A Doe",))
	provider = StagingTableAssessorProvider(session)
	assert provider.check_still_owns("hillsborough_fl", "123 Main St", "Jane Doe") is True


# ---------------------------------------------------------------------------
# _check_non_poach — client_pm_books, cross-client by design
# ---------------------------------------------------------------------------

def test_non_poach_true_on_email_match():
	session = _FakeSession((1,))
	assert _check_non_poach(session, "owner@example.com") is True


def test_non_poach_false_when_no_email():
	session = _FakeSession((1,))  # would match if queried, but must not be queried
	assert _check_non_poach(session, None) is False


def test_non_poach_false_on_no_match():
	session = _FakeSession(None)
	assert _check_non_poach(session, "owner@example.com") is False


# ---------------------------------------------------------------------------
# _resolve_county_slug
# ---------------------------------------------------------------------------

def test_resolve_county_slug_found():
	session = _FakeSession(("hillsborough_fl",))
	assert _resolve_county_slug(session, "Hillsborough") == "hillsborough_fl"


def test_resolve_county_slug_not_found_returns_none():
	session = _FakeSession(None)
	assert _resolve_county_slug(session, "Nonexistent County") is None


def test_resolve_county_slug_empty_input_returns_none_without_querying():
	session = MagicMock()
	assert _resolve_county_slug(session, "") is None
	session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# _process_row — SOLD suppression, disposition-gated DNC targeting
# (findings from the post-implementation audit, not just the plan)
# ---------------------------------------------------------------------------

class _ScriptedResult:
	def __init__(self, fetchone_value=None, scalar_one_value=None):
		self._fetchone_value = fetchone_value
		self._scalar_one_value = scalar_one_value

	def fetchone(self):
		return self._fetchone_value

	def scalar_one(self):
		return self._scalar_one_value


class _ScriptedSession:
	"""Returns queued results in call order — county resolution, assessor
	lookup, non-poach check, then the row insert, matching _process_row's
	fixed call sequence."""

	def __init__(self, results):
		self._results = list(results)

	def execute(self, *_args, **_kwargs):
		return self._results.pop(0)


def _row(**overrides):
	base = dict(
		owner_name="Jane Doe",
		property_address="123 Main St",
		county_input="hillsborough_fl",
		phone="8135550100",
		email="jane@example.com",
		validation_error=None,
	)
	base.update(overrides)
	return ParsedRow(**base)


def test_process_row_sold_sets_suppression_state_and_reason():
	# county resolves, assessor name mismatch (still_owns=False -> SOLD),
	# non-poach check runs but its result must not matter for a SOLD row.
	session = _ScriptedSession(
		[
			_ScriptedResult(fetchone_value=("hillsborough_fl",)),  # county resolution
			_ScriptedResult(fetchone_value=("Totally Different Person",)),  # assessor lookup
			_ScriptedResult(fetchone_value=None),  # non-poach: no match
			_ScriptedResult(scalar_one_value=1),  # insert
		]
	)
	counts = {"total_rows": 1, "still_owns_still_renting_count": 0, "still_owns_not_renting_count": 0,
			  "sold_count": 0, "unknown_count": 0, "suppressed_count": 0}
	dnc_targets: list = []
	_process_row(session, "import-1", "acme_pm", _row(), StagingTableAssessorProvider(session), StubFrboProvider(),
				 datetime.now(timezone.utc), counts, dnc_targets)
	assert counts["sold_count"] == 1
	assert counts["suppressed_count"] == 1
	assert dnc_targets == []  # SOLD must never queue a paid DNC scrub


def test_process_row_outreach_eligible_queues_dnc_target_with_normalized_phone():
	session = _ScriptedSession(
		[
			_ScriptedResult(fetchone_value=("hillsborough_fl",)),
			_ScriptedResult(fetchone_value=("Jane Doe",)),  # assessor match -> still_owns=True
			_ScriptedResult(fetchone_value=None),  # non-poach: no match
			_ScriptedResult(scalar_one_value=42),  # insert
		]
	)
	counts = {"total_rows": 1, "still_owns_still_renting_count": 0, "still_owns_not_renting_count": 0,
			  "sold_count": 0, "unknown_count": 0, "suppressed_count": 0}
	dnc_targets: list = []
	frbo = StubFrboProvider()
	# still_renting stays None under the stub -> disposition UNKNOWN, so
	# force a real FRBO answer here to exercise the outreach-eligible path.
	frbo.check_active_listing = lambda address: True
	_process_row(session, "import-1", "acme_pm", _row(), StagingTableAssessorProvider(session), frbo,
				 datetime.now(timezone.utc), counts, dnc_targets)
	assert counts["still_owns_still_renting_count"] == 1
	assert counts["suppressed_count"] == 0
	# +1 country code must be normalized away, matching dnc_refresh.py's
	# own _normalize_phone convention, not a bare digit-strip.
	assert dnc_targets == [(42, "8135550100")]


def test_process_row_non_poach_hit_suppresses_and_skips_dnc():
	session = _ScriptedSession(
		[
			_ScriptedResult(fetchone_value=("hillsborough_fl",)),
			_ScriptedResult(fetchone_value=("Jane Doe",)),
			_ScriptedResult(fetchone_value=(1,)),  # non-poach: match found
			_ScriptedResult(scalar_one_value=7),
		]
	)
	counts = {"total_rows": 1, "still_owns_still_renting_count": 0, "still_owns_not_renting_count": 0,
			  "sold_count": 0, "unknown_count": 0, "suppressed_count": 0}
	dnc_targets: list = []
	frbo = StubFrboProvider()
	frbo.check_active_listing = lambda address: True
	_process_row(session, "import-1", "acme_pm", _row(), StagingTableAssessorProvider(session), frbo,
				 datetime.now(timezone.utc), counts, dnc_targets)
	assert counts["suppressed_count"] == 1
	assert dnc_targets == []


# ---------------------------------------------------------------------------
# _csv_safe / export_csv — formula-injection guard
# ---------------------------------------------------------------------------

def test_csv_safe_neutralizes_leading_formula_characters():
	for hostile in ("=cmd|'/c calc'!A0", "+1+1", "-1+1", "@SUM(1,1)"):
		assert _csv_safe(hostile).startswith("'")


def test_csv_safe_passes_through_normal_strings():
	assert _csv_safe("123 Main St") == "123 Main St"


def test_csv_safe_passes_through_non_strings():
	assert _csv_safe(None) is None
	assert _csv_safe(True) is True


def test_export_csv_neutralizes_formula_in_owner_name():
	class _ExportResult:
		def fetchall(self):
			return [("=cmd|'/c calc'!A0", "123 Main St", "hillsborough_fl", "8135550100", "jane@example.com",
					  SOLD, False, None, False, True, "SOLD", None)]

	class _ExportSession:
		def execute(self, *_args, **_kwargs):
			return _ExportResult()

	csv_text = export_csv(_ExportSession(), "import-1")
	assert "'=cmd" in csv_text
