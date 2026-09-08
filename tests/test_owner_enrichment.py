"""Tests for owner_enrichment.py (Subtask 3.2.1). No live DB — mirrors
tests/test_winback_ingest.py's scripted-session style."""

from datetime import datetime, timezone

import pytest

from src.services.owner_enrichment import (
	EnrichmentInput,
	EnrichmentResult,
	StubOwnerEnrichmentProvider,
	apply_result,
	build_qa_summary,
	chunk_inputs,
	enrich_homestead_drop_signals,
)


def _input(**overrides):
	base = dict(
		winback_row_id=1,
		owner_name="Jane Doe",
		property_address="123 Main St",
		county_slug="hillsborough_fl",
		known_email="jane@example.com",
		known_phone="8135550100",
	)
	base.update(overrides)
	return EnrichmentInput(**base)


def _result(**overrides):
	base = dict(email=None, email_status="UNVERIFIED", phone=None, phone_verified=None, provider="test")
	base.update(overrides)
	return EnrichmentResult(**base)


# ---------------------------------------------------------------------------
# EnrichmentResult validation
# ---------------------------------------------------------------------------

def test_enrichment_result_rejects_invalid_email_status():
	with pytest.raises(ValueError):
		EnrichmentResult(email=None, email_status="BOGUS", phone=None, phone_verified=None, provider="test")


def test_enrichment_result_rejects_blank_provider():
	with pytest.raises(ValueError):
		EnrichmentResult(email=None, email_status="UNVERIFIED", phone=None, phone_verified=None, provider="")


# ---------------------------------------------------------------------------
# StubOwnerEnrichmentProvider — must pass CSV values through unchanged
# ---------------------------------------------------------------------------

def test_stub_provider_passes_known_values_through_unchanged():
	provider = StubOwnerEnrichmentProvider()
	inputs = [_input(winback_row_id=7, known_email="owner@example.com", known_phone="8135551234")]
	handle = provider.submit(inputs)
	results = provider.collect(handle)
	assert results[7].email == "owner@example.com"
	assert results[7].phone == "8135551234"
	assert results[7].provider == "stub"


def test_stub_provider_never_invents_verified_status():
	provider = StubOwnerEnrichmentProvider()
	inputs = [_input(winback_row_id=1, known_email="jane@example.com")]
	results = provider.collect(provider.submit(inputs))
	assert results[1].email_status == "UNVERIFIED"
	assert results[1].phone_verified is None


def test_stub_provider_row_with_no_known_contact_returns_none_not_invented():
	provider = StubOwnerEnrichmentProvider()
	inputs = [_input(winback_row_id=1, known_email=None, known_phone=None)]
	results = provider.collect(provider.submit(inputs))
	assert results[1].email is None
	assert results[1].phone is None


# ---------------------------------------------------------------------------
# chunk_inputs
# ---------------------------------------------------------------------------

def test_chunk_inputs_splits_at_batch_size():
	inputs = [_input(winback_row_id=i) for i in range(5)]
	chunks = chunk_inputs(inputs, 2)
	assert [len(c) for c in chunks] == [2, 2, 1]


def test_chunk_inputs_single_chunk_when_under_batch_size():
	inputs = [_input(winback_row_id=i) for i in range(3)]
	assert chunk_inputs(inputs, 10) == [inputs]


# ---------------------------------------------------------------------------
# apply_result — the row-mutation contract
# ---------------------------------------------------------------------------

class _FakeApplyResultSession:
	"""Scripts the SELECT ... FOR UPDATE that apply_result issues, then
	records the UPDATE's bind params for assertion."""

	def __init__(self, existing_email, existing_phone):
		self._existing_email = existing_email
		self._existing_phone = existing_phone
		self.update_params: dict = {}
		self.update_sql: str = ""

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if sql.strip().startswith("SELECT"):
			return _FetchOne(self._existing_email, self._existing_phone)
		self.update_sql = sql
		self.update_params = dict(params or {})
		return None


class _FetchOne:
	def __init__(self, email, phone):
		self.email = email
		self.phone = phone

	def fetchone(self):
		return self


_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_apply_result_first_time_email_population_leaves_email_previous_null():
	session = _FakeApplyResultSession(existing_email=None, existing_phone=None)
	outcome = apply_result(session, 1, _result(email="new@example.com"), _NOW)
	assert outcome.email == "new@example.com"
	assert session.update_params["email_previous"] is None
	assert "email_previous = :email_previous" not in session.update_sql


def test_apply_result_overwrite_of_existing_email_populates_email_previous():
	session = _FakeApplyResultSession(existing_email="old@example.com", existing_phone=None)
	outcome = apply_result(session, 1, _result(email="new@example.com"), _NOW)
	assert outcome.email == "new@example.com"
	assert session.update_params["email_previous"] == "old@example.com"
	assert "email_previous = :email_previous" in session.update_sql


def test_apply_result_same_value_result_does_not_populate_email_previous():
	session = _FakeApplyResultSession(existing_email="same@example.com", existing_phone=None)
	apply_result(session, 1, _result(email="same@example.com"), _NOW)
	assert session.update_params["email_previous"] is None


def test_apply_result_never_overwrites_existing_email_with_none():
	session = _FakeApplyResultSession(existing_email="keep@example.com", existing_phone=None)
	outcome = apply_result(session, 1, _result(email=None), _NOW)
	assert outcome.email == "keep@example.com"


def test_apply_result_requires_review_when_no_email_and_no_verified_phone():
	session = _FakeApplyResultSession(existing_email=None, existing_phone=None)
	outcome = apply_result(session, 1, _result(email=None, phone=None), _NOW)
	assert outcome.requires_enrichment_review is True


def test_apply_result_not_requires_review_when_email_present():
	session = _FakeApplyResultSession(existing_email=None, existing_phone=None)
	outcome = apply_result(session, 1, _result(email="jane@example.com"), _NOW)
	assert outcome.requires_enrichment_review is False


def test_apply_result_phone_only_verified_counts_as_enriched():
	# Client comments:77 — "owner -> phone/email", either one.
	session = _FakeApplyResultSession(existing_email=None, existing_phone=None)
	outcome = apply_result(session, 1, _result(phone="8135551234", phone_verified=True), _NOW)
	assert outcome.requires_enrichment_review is False


def test_apply_result_phone_only_unverified_still_requires_review():
	# phone_verified=None (provider couldn't determine) must NOT count as
	# a successful enrichment, even though a phone value exists.
	session = _FakeApplyResultSession(existing_email=None, existing_phone=None)
	outcome = apply_result(session, 1, _result(phone="8135551234", phone_verified=None), _NOW)
	assert outcome.requires_enrichment_review is True


def test_apply_result_newly_discovered_phone_is_normalized_and_returned():
	session = _FakeApplyResultSession(existing_email=None, existing_phone=None)
	outcome = apply_result(session, 1, _result(phone="+1 (813) 555-0100", phone_verified=True), _NOW)
	assert outcome.newly_discovered_phone == "8135550100"


def test_apply_result_phone_unchanged_from_existing_is_not_newly_discovered():
	session = _FakeApplyResultSession(existing_email=None, existing_phone="8135550100")
	outcome = apply_result(session, 1, _result(phone="8135550100", phone_verified=True), _NOW)
	assert outcome.newly_discovered_phone is None


def test_apply_result_row_not_found_returns_none():
	class _EmptySession:
		def execute(self, *_a, **_kw):
			return _FetchOneNone()

	class _FetchOneNone:
		def fetchone(self):
			return None

	assert apply_result(_EmptySession(), 999, _result(), _NOW) is None


# ---------------------------------------------------------------------------
# build_qa_summary / enrich_homestead_drop_signals
# ---------------------------------------------------------------------------

def test_build_qa_summary_flags_stub_provider():
	text_out = build_qa_summary({"provider": "stub", "enriched": 8, "failed": 2, "pending": 0, "attempts_exhausted": 0})
	assert "provider=stub" in text_out
	assert "NOT a live verification" in text_out


def test_build_qa_summary_no_stub_note_for_live_provider():
	text_out = build_qa_summary({"provider": "tracerfy", "enriched": 8, "failed": 2, "pending": 0, "attempts_exhausted": 0})
	assert "NOT a live verification" not in text_out


def test_build_qa_summary_includes_all_counts():
	text_out = build_qa_summary({"enriched": 3, "failed": 1, "pending": 2, "attempts_exhausted": 4})
	assert "enriched=3" in text_out
	assert "failed=1" in text_out
	assert "pending=2" in text_out
	assert "attempts_exhausted=4" in text_out


def test_enrich_homestead_drop_signals_is_a_documented_noop():
	assert enrich_homestead_drop_signals(None, StubOwnerEnrichmentProvider()) == 0


def test_build_qa_summary_surfaces_submit_failures_loudly():
	# Bug fix: a broken vendor call (submit() raising) must not look like a
	# harmless "nothing to do yet" in the message a human actually reads.
	text_out = build_qa_summary({"provider": "tracerfy", "submit_failures": 2, "pending": 10})
	assert "2 vendor SUBMIT failure" in text_out
	assert "NOT running" in text_out


def test_build_qa_summary_no_failure_note_when_zero():
	text_out = build_qa_summary({"provider": "tracerfy", "submit_failures": 0})
	assert "SUBMIT failure" not in text_out


# ---------------------------------------------------------------------------
# EnrichmentResult — phone_verified=True requires a real phone value
# ---------------------------------------------------------------------------

def test_enrichment_result_rejects_verified_true_with_no_phone():
	with pytest.raises(ValueError):
		EnrichmentResult(email=None, email_status="UNVERIFIED", phone=None, phone_verified=True, provider="test")


def test_enrichment_result_allows_verified_false_with_no_phone():
	# Only phone_verified=True + phone=None is the invalid combination.
	EnrichmentResult(email=None, email_status="UNVERIFIED", phone=None, phone_verified=False, provider="test")


# ---------------------------------------------------------------------------
# apply_result — has_verified_phone must key off THIS call's own result,
# never off a pre-existing, never-verified phone already on the row
# ---------------------------------------------------------------------------

def test_apply_result_does_not_treat_preexisting_phone_as_verified():
	# The row already has an (unverified) phone from the CSV. This call's
	# result found no NEW phone but somehow carries phone_verified=True for
	# a different reason (e.g. an email-only result path) — must NOT borrow
	# verification for the OLD phone value.
	session = _FakeApplyResultSession(existing_email=None, existing_phone="8135550100")
	outcome = apply_result(session, 1, _result(email=None, phone=None, phone_verified=None), _NOW)
	assert outcome.requires_enrichment_review is True  # old phone was never actually verified


def test_apply_result_verified_phone_from_this_call_counts():
	session = _FakeApplyResultSession(existing_email=None, existing_phone=None)
	outcome = apply_result(session, 1, _result(phone="8135550100", phone_verified=True), _NOW)
	assert outcome.requires_enrichment_review is False
