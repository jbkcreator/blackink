from unittest.mock import patch

import pytest

from config.settings import AppSettings
from src.services.rent_valuation import (
    ParsedAddress,
    RentValuationProvider,
    StubLiveRentValuationProvider,
    ValuationResult,
    get_live_provider,
    is_live_provider_enabled,
)


def _addr():
    return ParsedAddress(street="123 Ocean Dr", city="Tampa", state="FL", zip_code="33606")


def test_provider_row_ships_disabled():
    """Client instruction Wk1 #5: leave the provider row disabled. Asserts
    the DECLARED DEFAULT, not the resolved setting — a developer's local
    .env must not be able to make this pass or fail. If this ever goes red,
    someone enabled a vendor path that has no contract behind it."""
    assert AppSettings.model_fields["rentbot_live_api_enabled"].default is False


def test_stub_provider_returns_none_never_a_fabricated_number():
    assert StubLiveRentValuationProvider().get(_addr()) is None


def test_default_provider_is_the_stub():
    assert isinstance(get_live_provider(), StubLiveRentValuationProvider)


def test_interface_rejects_incomplete_implementations():
    """The ABC is the contract. A provider missing get() must fail loudly at
    construction, not silently at first call during a live demo."""

    class Incomplete(RentValuationProvider):
        pass

    with pytest.raises(TypeError):
        Incomplete()


def test_a_real_provider_satisfies_the_interface_unchanged():
    """Proves the seam actually works: this is exactly the shape a Q1
    RentCast or CoreLogic adapter takes, with zero changes to this module."""

    class FakeVendor(RentValuationProvider):
        def get(self, address):
            return ValuationResult(
                estimated_rent=2450, range_low=2300, range_high=2600,
                confidence_score=94, source="fake_vendor",
                address_display=f"{address.street}, {address.city}",
            )

    result = FakeVendor().get(_addr())
    assert result.estimated_rent == 2450
    assert result.source == "fake_vendor"


def test_is_live_provider_enabled_reads_settings():
    with patch("src.services.rent_valuation.get_settings") as mock_settings:
        mock_settings.return_value.rentbot_live_api_enabled = True
        assert is_live_provider_enabled() is True
