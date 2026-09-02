"""Pure unit tests (no live DB) for Week 1 Subtask 1.2.2's Checks 3+4
(DNC/quiet-hours/warm-channel waterfall). Mirrors test_compliance_gate.py's
SimpleNamespace/FakeSession pattern. The live-DB integration path
(evaluate_full_readiness() end to end, including the SQL evaluate_
campaign_readiness() call) is covered separately in
tests/test_tenant_isolation.py.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services.campaign_readiness_gate import (
	COLD_EMAIL_ONLY,
	ENGAGED_DNC_SMS_WITHHELD,
	ENGAGED_DNC_UNKNOWN_SMS_WITHHELD,
	ENGAGED_QUIET_HOURS_SMS_WITHHELD,
	ENGAGED_SMS_ELIGIBLE,
	_decide_channel,
	_in_quiet_hours,
	is_engaged,
	_resolve_dnc_listed,
)
from migrations.apply_area_code_timezones import SEED_AREA_CODES
from src.services.compliance_gate import DncProvider, StubDncProvider


class _AlwaysListedDnc(DncProvider):
	def check(self, phone):
		return True


class _AlwaysClearDnc(DncProvider):
	def check(self, phone):
		return False


class _CountingDnc(DncProvider):
	def __init__(self, listed=False):
		self.listed = listed
		self.calls = 0

	def check(self, phone):
		self.calls += 1
		return self.listed


class _UnknownDnc(DncProvider):
	"""A live provider whose lookup failed or couldn't determine a result —
	distinct from StubDncProvider (no vendor at all), same None contract."""

	def __init__(self):
		self.calls = 0

	def check(self, phone):
		self.calls += 1
		return None


class _FakeResult:
	def __init__(self, row=None, scalar=None):
		self._row = row
		self._scalar = scalar

	def one(self):
		return self._row

	def scalar(self):
		return self._scalar


class _FakeSession:
	"""Scripted responses keyed by call order: pass a list of _FakeResult
	for successive .execute() calls (SELECTs); UPDATE/INSERT calls are
	recorded but return nothing consumed."""

	def __init__(self, results):
		self._results = list(results)
		self.executed = []

	def execute(self, stmt, params=None):
		self.executed.append((str(stmt), params))
		if self._results:
			return self._results.pop(0)
		return _FakeResult()


# ── is_engaged ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
	"inbound_sms_count,booked_appointment_id,expected",
	[
		(0, None, False),
		(1, None, True),
		(0, "appt_123", True),
		(5, "appt_123", True),
	],
)
def test_is_engaged(inbound_sms_count, booked_appointment_id, expected):
	assert is_engaged(inbound_sms_count, booked_appointment_id) is expected


# ── _resolve_dnc_listed — cache behavior ────────────────────────────────


def test_dnc_cache_miss_calls_provider_and_writes_cache():
	session = _FakeSession([_FakeResult(row=SimpleNamespace(dnc_clean=None, dnc_checked_at=None))])
	provider = _CountingDnc(listed=True)
	listed = _resolve_dnc_listed(session, 1, "+15551234567", provider)
	assert listed is True
	assert provider.calls == 1
	# Second execute call is the cache-write UPDATE.
	assert "UPDATE contacts" in session.executed[1][0]


def test_dnc_cache_hit_fresh_does_not_call_provider():
	fresh = datetime.now(timezone.utc) - timedelta(days=5)
	session = _FakeSession([_FakeResult(row=SimpleNamespace(dnc_clean=False, dnc_checked_at=fresh))])
	provider = _CountingDnc(listed=True)
	listed = _resolve_dnc_listed(session, 1, "+15551234567", provider)
	assert listed is True  # dnc_clean=False means listed
	assert provider.calls == 0


def test_dnc_cache_stale_treated_as_miss():
	stale = datetime.now(timezone.utc) - timedelta(days=45)
	session = _FakeSession([_FakeResult(row=SimpleNamespace(dnc_clean=True, dnc_checked_at=stale))])
	provider = _CountingDnc(listed=True)
	listed = _resolve_dnc_listed(session, 1, "+15551234567", provider)
	assert provider.calls == 1
	assert listed is True


def test_dnc_no_phone_does_not_call_provider():
	session = _FakeSession([_FakeResult(row=SimpleNamespace(dnc_clean=None, dnc_checked_at=None))])
	provider = _CountingDnc(listed=True)
	listed = _resolve_dnc_listed(session, 1, None, provider)
	assert listed is None, "no phone means unknown, not clear — must not be treated as not-listed"
	assert provider.calls == 0


def test_dnc_provider_unknown_result_is_not_cached_as_clean():
	"""A live provider returning None (lookup failed / indeterminate) must
	propagate as unknown, not get coerced to False and written into the
	dnc_clean cache — that would falsely mark the contact clean for the
	whole recheck window."""
	session = _FakeSession([_FakeResult(row=SimpleNamespace(dnc_clean=None, dnc_checked_at=None))])
	provider = _UnknownDnc()
	listed = _resolve_dnc_listed(session, 1, "+15551234567", provider)
	assert listed is None
	assert provider.calls == 1
	assert not any("UPDATE contacts" in stmt for stmt, _ in session.executed), (
		"an unknown DNC result must not be written to the cache"
	)


def test_dnc_stub_provider_unknown_result_is_not_cached_as_clean():
	"""StubDncProvider — the default when no vendor is contracted — always
	returns None. Same contract, same guarantee: never coerced to clean."""
	session = _FakeSession([_FakeResult(row=SimpleNamespace(dnc_clean=None, dnc_checked_at=None))])
	listed = _resolve_dnc_listed(session, 1, "+15551234567", StubDncProvider())
	assert listed is None
	assert not any("UPDATE contacts" in stmt for stmt, _ in session.executed), (
		"StubDncProvider's unknown result must not be written to the cache"
	)


# ── _in_quiet_hours ──────────────────────────────────────────────────────


def test_quiet_hours_unknown_area_code_fails_closed():
	session = _FakeSession([_FakeResult(scalar=None)])
	assert _in_quiet_hours(session, "+19995551234") is True


def test_quiet_hours_no_phone_fails_closed():
	session = _FakeSession([])
	assert _in_quiet_hours(session, None) is True


def test_quiet_hours_non_us_phone_fails_closed():
	session = _FakeSession([])
	assert _in_quiet_hours(session, "+442071234567") is True


# ── us_area_code_timezones seed data — production coverage ─────────────


def test_area_code_seed_is_not_a_small_representative_subset():
	"""Regression for a prior version of this migration that seeded only 11
	area codes while _in_quiet_hours() fails closed (quiet hours, no SMS)
	for anything unmapped — silently suppressing transactional SMS for the
	overwhelming majority of real US phone numbers. The production seed
	must cover essentially the full NANP US assignment, not a handful of
	examples."""
	assert len(SEED_AREA_CODES) > 250, (
		f"only {len(SEED_AREA_CODES)} area codes seeded — looks like a small "
		"representative subset again, not full US NANP coverage"
	)


def test_area_code_seed_covers_a_non_example_code():
	"""214 (Dallas) was never one of the original hand-picked example codes
	(212/813/305/407/904/312/713/303/602/415/213) — proves the seed is real
	coverage, not just the old examples re-labeled."""
	area_codes = {row["area_code"] for row in SEED_AREA_CODES}
	assert "214" in area_codes
	dallas = next(row for row in SEED_AREA_CODES if row["area_code"] == "214")
	assert dallas["iana_timezone"] == "America/Chicago"


def test_area_code_seed_has_no_duplicate_codes():
	area_codes = [row["area_code"] for row in SEED_AREA_CODES]
	assert len(area_codes) == len(set(area_codes))


# ── _decide_channel — the CI-enforced predicate ─────────────────────────


def test_cold_contact_never_gets_sms():
	eligibility, reason = _decide_channel(engaged=False, dnc_listed=False, quiet_hours_active=False)
	assert eligibility == "EMAIL_COLD_ELIGIBLE"
	assert reason == COLD_EMAIL_ONLY


def test_engaged_dnc_listed_gets_email_only_not_blocked():
	"""Locks in the corrected blueprint behavior: DNC is a channel
	restriction, not a full block — the gate never fails for this."""
	eligibility, reason = _decide_channel(engaged=True, dnc_listed=True, quiet_hours_active=False)
	assert eligibility == "EMAIL_COLD_ELIGIBLE"
	assert reason == ENGAGED_DNC_SMS_WITHHELD


def test_engaged_dnc_unknown_withholds_sms_not_blocked():
	"""An unresolved DNC check (None) must withhold SMS exactly like a
	listed result — never fall through to TRANSACTIONAL_SMS_ONLY just
	because it isn't literally True."""
	eligibility, reason = _decide_channel(engaged=True, dnc_listed=None, quiet_hours_active=False)
	assert eligibility == "EMAIL_COLD_ELIGIBLE"
	assert reason == ENGAGED_DNC_UNKNOWN_SMS_WITHHELD


def test_engaged_quiet_hours_withholds_sms():
	eligibility, reason = _decide_channel(engaged=True, dnc_listed=False, quiet_hours_active=True)
	assert eligibility == "EMAIL_COLD_ELIGIBLE"
	assert reason == ENGAGED_QUIET_HOURS_SMS_WITHHELD


def test_engaged_clean_daytime_gets_sms():
	eligibility, reason = _decide_channel(engaged=True, dnc_listed=False, quiet_hours_active=False)
	assert eligibility == "TRANSACTIONAL_SMS_ONLY"
	assert reason == ENGAGED_SMS_ELIGIBLE


@pytest.mark.parametrize(
	"engaged,dnc_listed,quiet_hours_active",
	[
		(False, False, False),
		(False, True, False),
		(False, False, True),
		(False, True, True),
	],
)
def test_never_grants_sms_when_not_engaged(engaged, dnc_listed, quiet_hours_active):
	"""CI-enforced predicate from the master blueprint §3.0.4: SMS must
	never be eligible when inbound_sms_count == 0 AND booked_appointment_id
	IS NULL (i.e. not engaged), regardless of DNC/quiet-hours state."""
	eligibility, _ = _decide_channel(engaged, dnc_listed, quiet_hours_active)
	assert eligibility != "TRANSACTIONAL_SMS_ONLY"
