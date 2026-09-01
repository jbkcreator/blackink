"""Tests for SMS quiet hours enforcement."""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src.services.sms_quiet_hours import is_quiet_hours, assert_not_quiet_hours, _recipient_tz

# ---------------------------------------------------------------------------
# _recipient_tz
# ---------------------------------------------------------------------------

def test_tampa_area_code_is_eastern():
    tz = _recipient_tz("+18135550100")
    assert tz.key == "America/New_York"


def test_panhandle_area_code_is_central():
    tz = _recipient_tz("+18505550100")
    assert tz.key == "America/Chicago"


def test_unknown_area_code_defaults_eastern():
    tz = _recipient_tz("+19995550100")
    assert tz.key == "America/New_York"


def test_strips_leading_1():
    assert _recipient_tz("+18135550100").key == _recipient_tz("8135550100").key


# ---------------------------------------------------------------------------
# is_quiet_hours
# ---------------------------------------------------------------------------

def _mock_now(hour: int, tz_key: str):
    """Patch datetime.now to return a fixed hour in the given timezone."""
    dt = datetime(2026, 9, 1, hour, 0, 0, tzinfo=ZoneInfo(tz_key))
    return patch("src.services.sms_quiet_hours.datetime", wraps=datetime,
                 **{"now.return_value": dt})


def test_quiet_at_9pm():
    with _mock_now(21, "America/New_York"):
        assert is_quiet_hours("+18135550100") is True


def test_quiet_at_midnight():
    with _mock_now(0, "America/New_York"):
        assert is_quiet_hours("+18135550100") is True


def test_quiet_at_7am():
    with _mock_now(7, "America/New_York"):
        assert is_quiet_hours("+18135550100") is True


def test_not_quiet_at_8am():
    with _mock_now(8, "America/New_York"):
        assert is_quiet_hours("+18135550100") is False


def test_not_quiet_at_noon():
    with _mock_now(12, "America/New_York"):
        assert is_quiet_hours("+18135550100") is False


def test_not_quiet_at_8pm():
    with _mock_now(20, "America/New_York"):
        assert is_quiet_hours("+18135550100") is False


# ---------------------------------------------------------------------------
# assert_not_quiet_hours
# ---------------------------------------------------------------------------

def test_assert_raises_during_quiet():
    with _mock_now(22, "America/New_York"):
        with pytest.raises(ValueError, match="quiet hours"):
            assert_not_quiet_hours("+18135550100")


def test_assert_passes_during_business_hours():
    with _mock_now(10, "America/New_York"):
        assert_not_quiet_hours("+18135550100")  # no raise
