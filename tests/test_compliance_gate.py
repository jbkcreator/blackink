"""Table-driven PASS/FAIL/ABSTAIN coverage for every compliance-gate
predicate. Pure unit tests — no live database required, using SimpleNamespace
stand-ins for Contact rows and a minimal FakeSession for the DB-touching
non_poach predicate, so this suite runs anywhere without Postgres.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services.compliance_gate import (
	ABSTAIN,
	FAIL,
	PASS,
	DncProvider,
	_check_cooldown,
	_check_deterministic_columns,
	_check_dnc,
	_check_non_poach,
	_check_not_paused,
)


def _contact(**overrides):
	base = dict(
		email_status="VERIFIED",
		is_opted_out=False,
		suppression_state=False,
		compliance_eligibility="EMAIL_COLD_ELIGIBLE",
		last_outbound_touch_at=None,
		phone="+15551234567",
		dnc_clean=None,
		dnc_checked_at=None,
		outbound_paused_at=None,
		outbound_pause_reason=None,
	)
	base.update(overrides)
	return SimpleNamespace(**base)


class _AlwaysListedDnc(DncProvider):
	def check(self, phone):
		return True


class _AlwaysClearDnc(DncProvider):
	def check(self, phone):
		return False


class _UnknownDnc(DncProvider):
	def check(self, phone):
		return None


class FakeSession:
	"""Minimal stand-in for the one query _check_non_poach issues."""

	def __init__(self, scalar_result=None, raise_error=False):
		self._scalar_result = scalar_result
		self._raise_error = raise_error

	def execute(self, *args, **kwargs):
		if self._raise_error:
			raise RuntimeError("simulated DB error")
		return SimpleNamespace(scalar=lambda: self._scalar_result)


# ── _check_deterministic_columns ────────────────────────────────────────

@pytest.mark.parametrize(
	"overrides,expected_status",
	[
		({}, PASS),
		({"email_status": "UNVERIFIED"}, FAIL),
		({"is_opted_out": True}, FAIL),
		({"suppression_state": True}, FAIL),
		({"compliance_eligibility": "BLOCKED"}, FAIL),
	],
)
def test_deterministic_columns(overrides, expected_status):
	result = _check_deterministic_columns(_contact(**overrides))
	assert result.status == expected_status


# ── _check_cooldown ──────────────────────────────────────────────────────

def test_cooldown_pass_no_prior_touch():
	assert _check_cooldown(_contact(last_outbound_touch_at=None)).status == PASS


def test_cooldown_pass_elapsed():
	touch = datetime.now(timezone.utc) - timedelta(days=20)
	assert _check_cooldown(_contact(last_outbound_touch_at=touch)).status == PASS


def test_cooldown_fail_not_elapsed():
	touch = datetime.now(timezone.utc) - timedelta(days=2)
	assert _check_cooldown(_contact(last_outbound_touch_at=touch)).status == FAIL


# ── _check_dnc ───────────────────────────────────────────────────────────

def test_dnc_abstains_when_never_checked_and_provider_unknown():
	result = _check_dnc(_contact(dnc_checked_at=None), _UnknownDnc())
	assert result.status == ABSTAIN


def test_dnc_passes_with_no_phone_on_file():
	"""No phone → DNC registry not applicable → PASS (not ABSTAIN).
	A missing phone is not a registry hit; the registry has nothing to say."""
	result = _check_dnc(_contact(dnc_checked_at=None, phone=None), _AlwaysClearDnc())
	assert result.status == PASS


def test_dnc_fails_when_provider_reports_listed():
	result = _check_dnc(_contact(dnc_checked_at=None), _AlwaysListedDnc())
	assert result.status == FAIL


def test_dnc_passes_when_provider_reports_clear():
	result = _check_dnc(_contact(dnc_checked_at=None), _AlwaysClearDnc())
	assert result.status == PASS


def test_dnc_abstains_on_stale_cached_true():
	"""A stale TRUE is not trusted — must ABSTAIN, never silently PASS."""
	stale = datetime.now(timezone.utc) - timedelta(days=45)
	result = _check_dnc(_contact(dnc_clean=True, dnc_checked_at=stale), _AlwaysListedDnc())
	assert result.status == ABSTAIN


def test_dnc_passes_on_fresh_cached_true():
	fresh = datetime.now(timezone.utc) - timedelta(days=5)
	result = _check_dnc(_contact(dnc_clean=True, dnc_checked_at=fresh), _AlwaysListedDnc())
	assert result.status == PASS


def test_dnc_fails_on_fresh_cached_false():
	fresh = datetime.now(timezone.utc) - timedelta(days=5)
	result = _check_dnc(_contact(dnc_clean=False, dnc_checked_at=fresh), _AlwaysClearDnc())
	assert result.status == FAIL


# ── _check_not_paused (Subtask 3.2.3) ────────────────────────────────────

def test_not_paused_passes_when_never_paused():
	assert _check_not_paused(_contact()).status == PASS


def test_not_paused_fails_when_paused():
	paused_at = datetime.now(timezone.utc) - timedelta(hours=1)
	result = _check_not_paused(_contact(outbound_paused_at=paused_at, outbound_pause_reason="NO_SHOW_RECOVERY"))
	assert result.status == FAIL
	assert "NO_SHOW_RECOVERY" in result.detail


# ── _check_non_poach ─────────────────────────────────────────────────────

def test_non_poach_passes_when_not_claimed():
	session = FakeSession(scalar_result=False)
	result = _check_non_poach(session, "company_x")
	assert result.status == PASS


def test_non_poach_fails_when_claimed():
	session = FakeSession(scalar_result=True)
	result = _check_non_poach(session, "company_x")
	assert result.status == FAIL


def test_non_poach_abstains_on_db_error():
	"""The one case the Dev 1 plan calls out explicitly: a query failure
	must ABSTAIN, never be swallowed into an implicit PASS."""
	session = FakeSession(raise_error=True)
	result = _check_non_poach(session, "company_x")
	assert result.status == ABSTAIN
