"""Pure tests for src/services/settlement/split.py — no DB, no network."""
from datetime import datetime, timedelta, timezone

import pytest

from src.services.settlement.split import (
	FLAT_PER_AGREEMENT,
	PER_DOOR,
	OfferTerms,
	SettlementNotEnabledError,
	SettlementNotPricedError,
	compute_split,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _terms(**overrides) -> OfferTerms:
	defaults = dict(
		offer_code="test_offer", settlement_enabled=True, pricing_basis=PER_DOOR,
		per_door_bounty_cents=10_000, flat_bounty_cents=None,
		installment_1_bps=5000, clawback_window_days=60,
	)
	defaults.update(overrides)
	return OfferTerms(**defaults)


def test_per_door_50_50_exact():
	plan = compute_split(_terms(), door_count=4, door_signed_at=_NOW)
	assert plan.total_bounty_cents == 40_000
	assert plan.installment_1_cents == 20_000
	assert plan.installment_2_cents == 20_000
	assert plan.installment_1_cents + plan.installment_2_cents == plan.total_bounty_cents


def test_odd_cent_remainder_lands_on_installment_2():
	plan = compute_split(_terms(per_door_bounty_cents=101), door_count=1, door_signed_at=_NOW)
	assert plan.total_bounty_cents == 101
	assert plan.installment_1_cents == 50  # 101 * 5000 // 10000
	assert plan.installment_2_cents == 51
	assert plan.installment_1_cents + plan.installment_2_cents == 101


def test_bps_zero_and_full():
	plan_zero = compute_split(_terms(installment_1_bps=0), door_count=1, door_signed_at=_NOW)
	assert plan_zero.installment_1_cents == 0
	assert plan_zero.installment_2_cents == plan_zero.total_bounty_cents

	plan_full = compute_split(_terms(installment_1_bps=10000), door_count=1, door_signed_at=_NOW)
	assert plan_full.installment_1_cents == plan_full.total_bounty_cents
	assert plan_full.installment_2_cents == 0


def test_scheduled_for_is_door_signed_at_plus_window():
	plan = compute_split(_terms(clawback_window_days=60), door_count=1, door_signed_at=_NOW)
	assert plan.installment_2_scheduled_for == _NOW + timedelta(days=60)


def test_flat_per_agreement_ignores_door_count():
	terms = _terms(pricing_basis=FLAT_PER_AGREEMENT, per_door_bounty_cents=None, flat_bounty_cents=9_900)
	plan_1_door = compute_split(terms, door_count=1, door_signed_at=_NOW)
	plan_40_doors = compute_split(terms, door_count=40, door_signed_at=_NOW)
	assert plan_1_door.total_bounty_cents == 9_900
	assert plan_40_doors.total_bounty_cents == 9_900


def test_per_door_scales_with_door_count():
	terms = _terms(pricing_basis=PER_DOOR, per_door_bounty_cents=1_000)
	plan_1 = compute_split(terms, door_count=1, door_signed_at=_NOW)
	plan_10 = compute_split(terms, door_count=10, door_signed_at=_NOW)
	assert plan_10.total_bounty_cents == plan_1.total_bounty_cents * 10


def test_settlement_not_enabled_raises():
	with pytest.raises(SettlementNotEnabledError):
		compute_split(_terms(settlement_enabled=False), door_count=1, door_signed_at=_NOW)


def test_per_door_unpriced_raises():
	with pytest.raises(SettlementNotPricedError):
		compute_split(_terms(per_door_bounty_cents=None), door_count=1, door_signed_at=_NOW)


def test_flat_unpriced_raises():
	terms = _terms(pricing_basis=FLAT_PER_AGREEMENT, per_door_bounty_cents=None, flat_bounty_cents=None)
	with pytest.raises(SettlementNotPricedError):
		compute_split(terms, door_count=1, door_signed_at=_NOW)
