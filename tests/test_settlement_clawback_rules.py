"""Pure tests for decide_installment_2() — the day-60 clawback rule."""
from datetime import datetime, timedelta, timezone

from src.services.settlement.split import CHARGE, DEFER_UNVERIFIED, VOID_CLAWBACK, decide_installment_2

_SIGNED = datetime(2026, 1, 1, tzinfo=timezone.utc)
_WINDOW = 60


def _decide(*, terminated_at=None, still_active=None, as_of):
	return decide_installment_2(
		door_signed_at=_SIGNED, clawback_window_days=_WINDOW,
		terminated_at=terminated_at, agreement_still_active=still_active, as_of=as_of,
	)


def test_terminated_day_59_voids():
	terminated = _SIGNED + timedelta(days=59)
	assert _decide(terminated_at=terminated, still_active=False, as_of=_SIGNED + timedelta(days=61)) == VOID_CLAWBACK


def test_terminated_day_61_charges():
	# Terminated AFTER the 60-day window closed — outside the clawback rule,
	# so a definite-active provider reading proceeds to CHARGE.
	terminated = _SIGNED + timedelta(days=61)
	assert _decide(terminated_at=terminated, still_active=True, as_of=_SIGNED + timedelta(days=62)) == CHARGE


def test_day_60_boundary_is_outside_the_clawback_window():
	# Exactly at the deadline: documented as NOT clawed back.
	terminated = _SIGNED + timedelta(days=_WINDOW)
	assert _decide(terminated_at=terminated, still_active=True, as_of=_SIGNED + timedelta(days=61)) == CHARGE


def test_no_termination_active_charges():
	assert _decide(still_active=True, as_of=_SIGNED + timedelta(days=61)) == CHARGE


def test_no_termination_confirmed_inactive_voids():
	assert _decide(still_active=False, as_of=_SIGNED + timedelta(days=61)) == VOID_CLAWBACK


def test_uncertain_provider_defers_regardless_of_termination_state():
	as_of = _SIGNED + timedelta(days=61)
	assert _decide(terminated_at=None, still_active=None, as_of=as_of) == DEFER_UNVERIFIED
	assert _decide(terminated_at=_SIGNED + timedelta(days=61), still_active=None, as_of=as_of) == DEFER_UNVERIFIED
