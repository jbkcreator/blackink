"""Tests for owner_enrichment.py (Subtask 3.2.1). No live DB — mirrors
tests/test_winback_ingest.py's scripted-session style."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.services.owner_enrichment import (
	EnrichmentInput,
	EnrichmentResult,
	StubOwnerEnrichmentProvider,
	TracerfyEnrichmentProvider,
	apply_result,
	build_qa_summary,
	chunk_inputs,
	enrich_homestead_drop_signals,
	_first_present,
	_split_address,
	_split_owner_name,
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


def test_apply_result_unprocessable_forces_review_even_with_preexisting_email():
	# PR review finding, confirmed real: a row with a CSV email must not
	# pass the /arm gate just because it already had one -- if enrichment
	# never actually ran (address didn't parse), requires_enrichment_review
	# must be TRUE regardless of has_usable_email's normal fallback.
	session = _FakeApplyResultSession(existing_email="keep@example.com", existing_phone=None)
	outcome = apply_result(session, 1, _result(email=None), _NOW, unprocessable=True)
	assert outcome.email == "keep@example.com"
	assert outcome.requires_enrichment_review is True
	assert session.update_params["requires_review"] is True


def test_apply_result_unprocessable_forces_review_even_with_verified_phone():
	session = _FakeApplyResultSession(existing_email=None, existing_phone=None)
	outcome = apply_result(session, 1, _result(phone="8135551234", phone_verified=True), _NOW, unprocessable=True)
	assert outcome.requires_enrichment_review is True


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


# ---------------------------------------------------------------------------
# _split_address — the confirmed "STREET, CITY, ST[ ZIP]" convention only
# ---------------------------------------------------------------------------

def test_split_address_with_zip():
	assert _split_address("123 Main St, Tampa, FL 33601") == ("123 Main St", "Tampa", "FL")


def test_split_address_without_zip():
	assert _split_address("123 Main St, Tampa, FL") == ("123 Main St", "Tampa", "FL")


def test_split_address_no_commas_returns_none():
	# No guessing — a format this regex doesn't recognize is excluded from
	# submission entirely, never fed to Tracerfy as a garbage city.
	assert _split_address("123 Main St") is None


def test_split_address_blank_returns_none():
	assert _split_address("") is None
	assert _split_address(None) is None


def test_split_address_lowercases_state_normalized_to_upper():
	assert _split_address("1 Elm St, Clearwater, fl 33755") == ("1 Elm St", "Clearwater", "FL")


# ---------------------------------------------------------------------------
# _split_owner_name — deliberately simple (first token / remaining tokens)
# ---------------------------------------------------------------------------

def test_split_owner_name_two_tokens():
	assert _split_owner_name("Jane Doe") == ("Jane", "Doe")


def test_split_owner_name_three_tokens_keeps_rest_as_last():
	assert _split_owner_name("Jane Q Doe") == ("Jane", "Q Doe")


def test_split_owner_name_single_token_is_last_name_only():
	assert _split_owner_name("Doe") == ("", "Doe")


def test_split_owner_name_blank():
	assert _split_owner_name("") == ("", "")
	assert _split_owner_name(None) == ("", "")


# ---------------------------------------------------------------------------
# _first_present
# ---------------------------------------------------------------------------

def test_first_present_returns_first_nonblank():
	row = {"email_1": "", "email_2": "  ", "email_3": "jane@example.com", "email_4": "other@example.com"}
	assert _first_present(row, "email_1", "email_2", "email_3", "email_4") == "jane@example.com"


def test_first_present_none_when_all_blank():
	row = {"email_1": "", "email_2": None}
	assert _first_present(row, "email_1", "email_2") is None


# ---------------------------------------------------------------------------
# TracerfyEnrichmentProvider — submit()/collect() end to end, transport mocked
# ---------------------------------------------------------------------------

def _tracerfy_input(**overrides):
	base = dict(
		winback_row_id=1,
		owner_name="Jane Doe",
		property_address="123 Main St, Tampa, FL 33601",
		county_slug="hillsborough_fl",
		known_email=None,
		known_phone=None,
	)
	base.update(overrides)
	return EnrichmentInput(**base)


def test_tracerfy_provider_submits_only_parseable_addresses():
	unparseable = _tracerfy_input(winback_row_id=2, property_address="123 Main St")  # no city/state
	parseable = _tracerfy_input(winback_row_id=1)
	provider = TracerfyEnrichmentProvider("fake-key")
	with patch("src.services.tracerfy_client.submit_skiptrace_batch", return_value=("q1", 0)) as mock_submit:
		handle = provider.submit([parseable, unparseable])
	records = mock_submit.call_args.args[0]
	assert len(records) == 1
	assert records[0]["address"] == "123 Main St"
	assert records[0]["city"] == "Tampa"
	assert records[0]["state"] == "FL"
	assert records[0]["first_name"] == "Jane"
	assert records[0]["last_name"] == "Doe"
	# only the parseable row has a match key -- the unparseable one was
	# never submitted, so it correctly falls into collect()'s not-found path
	assert list(handle.match_keys.values()) == [1]
	# ... but it's still distinguishable from a genuine vendor miss, via
	# unprocessable_ids (PR review finding, confirmed real -- see
	# apply_result's own unprocessable parameter).
	assert handle.unprocessable_ids == frozenset({2})


def test_tracerfy_provider_submits_nothing_when_every_address_unparseable():
	provider = TracerfyEnrichmentProvider("fake-key")
	with patch("src.services.tracerfy_client.submit_skiptrace_batch") as mock_submit:
		handle = provider.submit([_tracerfy_input(property_address="123 Main St")])
	mock_submit.assert_not_called()
	assert handle.match_keys == {}
	assert handle.unprocessable_ids == frozenset({1})


def test_tracerfy_provider_collect_matches_by_normalized_address_not_row_id():
	# Confirmed contract: Tracerfy does NOT echo back any submitted row id.
	provider = TracerfyEnrichmentProvider("fake-key")
	with patch("src.services.tracerfy_client.submit_skiptrace_batch", return_value=("q1", 0)):
		handle = provider.submit([_tracerfy_input(winback_row_id=7)])
	result_row = {"address": "123 main st", "email_1": "jane@example.com", "primary_phone": "8135550100"}
	with patch("src.services.tracerfy_client.poll_skiptrace_queue", return_value=[result_row]):
		results = provider.collect(handle)
	assert 7 in results
	assert results[7].email == "jane@example.com"
	assert results[7].phone == "8135550100"
	assert results[7].phone_verified is True
	assert results[7].provider == "tracerfy"


def test_tracerfy_provider_collect_unmatched_result_row_is_ignored_not_crashed():
	provider = TracerfyEnrichmentProvider("fake-key")
	with patch("src.services.tracerfy_client.submit_skiptrace_batch", return_value=("q1", 0)):
		handle = provider.submit([_tracerfy_input(winback_row_id=1)])
	with patch("src.services.tracerfy_client.poll_skiptrace_queue", return_value=[{"address": "999 Nowhere Ave"}]):
		results = provider.collect(handle)
	assert results == {}


def test_tracerfy_provider_collect_no_phone_found_gives_none_not_false():
	provider = TracerfyEnrichmentProvider("fake-key")
	with patch("src.services.tracerfy_client.submit_skiptrace_batch", return_value=("q1", 0)):
		handle = provider.submit([_tracerfy_input(winback_row_id=1)])
	with patch("src.services.tracerfy_client.poll_skiptrace_queue", return_value=[{"address": "123 main st", "email_1": "jane@example.com"}]):
		results = provider.collect(handle)
	assert results[1].phone is None
	assert results[1].phone_verified is None  # never False -- Tracerfy never disproves a phone, only finds or doesn't


def test_tracerfy_provider_collect_empty_match_keys_never_calls_poll():
	# Every input was unparseable -- submit() must not waste an HTTP call
	# either, and collect() must not waste a poll call.
	provider = TracerfyEnrichmentProvider("fake-key")
	with patch("src.services.tracerfy_client.submit_skiptrace_batch") as mock_submit:
		handle = provider.submit([_tracerfy_input(property_address="123 Main St")])
	mock_submit.assert_not_called()
	with patch("src.services.tracerfy_client.poll_skiptrace_queue") as mock_poll:
		results = provider.collect(handle)
	mock_poll.assert_not_called()
	assert results == {}


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
