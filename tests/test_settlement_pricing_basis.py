"""FLAT_PER_AGREEMENT must never scale with door_count; PER_DOOR always
does. Each basis must raise when only the OTHER basis's amount is set."""
from datetime import datetime, timezone

import pytest

from src.services.settlement.split import (
	FLAT_PER_AGREEMENT,
	PER_DOOR,
	OfferTerms,
	SettlementNotPricedError,
	compute_split,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _terms(**overrides) -> OfferTerms:
	defaults = dict(
		offer_code="test_offer", settlement_enabled=True, pricing_basis=PER_DOOR,
		per_door_bounty_cents=1_000, flat_bounty_cents=None,
		installment_1_bps=5000, clawback_window_days=60,
	)
	defaults.update(overrides)
	return OfferTerms(**defaults)


def test_flat_per_agreement_same_total_regardless_of_door_count():
	terms = _terms(pricing_basis=FLAT_PER_AGREEMENT, per_door_bounty_cents=None, flat_bounty_cents=9_900)
	one_door = compute_split(terms, door_count=1, door_signed_at=_NOW)
	forty_doors = compute_split(terms, door_count=40, door_signed_at=_NOW)
	assert one_door.total_bounty_cents == forty_doors.total_bounty_cents == 9_900


def test_per_door_scales_with_door_count():
	terms = _terms(pricing_basis=PER_DOOR, per_door_bounty_cents=1_000)
	one_door = compute_split(terms, door_count=1, door_signed_at=_NOW)
	forty_doors = compute_split(terms, door_count=40, door_signed_at=_NOW)
	assert forty_doors.total_bounty_cents == one_door.total_bounty_cents * 40


def test_per_door_basis_with_only_flat_amount_set_raises():
	terms = _terms(pricing_basis=PER_DOOR, per_door_bounty_cents=None, flat_bounty_cents=5_000)
	with pytest.raises(SettlementNotPricedError):
		compute_split(terms, door_count=1, door_signed_at=_NOW)


def test_flat_basis_with_only_per_door_amount_set_raises():
	terms = _terms(pricing_basis=FLAT_PER_AGREEMENT, per_door_bounty_cents=1_000, flat_bounty_cents=None)
	with pytest.raises(SettlementNotPricedError):
		compute_split(terms, door_count=1, door_signed_at=_NOW)
