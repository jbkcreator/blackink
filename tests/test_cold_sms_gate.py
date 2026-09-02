"""CI/CD gate — cold outbound SMS block.

Dev 4 / Week 0 AC #5: build MUST FAIL if outbound SMS is attempted for a
contact where inbound_sms_count = 0 AND booked_appointment_id IS NULL.

These tests run without a live DB (FakeSession pattern). They are the
CI enforcement mechanism — if assert_not_cold_sms is removed or bypassed,
the test suite catches it here before merge.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.services.cold_sms_gate import (
    assert_not_cold_sms,
    get_booked_appointment_id,
    get_inbound_sms_count,
    is_cold_contact,
)


# ---------------------------------------------------------------------------
# Helpers — fake session that returns controlled query results
# ---------------------------------------------------------------------------

def _fake_session(inbound_count: int = 0, booked_id=None):
    """Build a mock Session whose execute().fetchone() returns fixture data."""
    session = MagicMock()

    def _execute(stmt, params):
        result = MagicMock()
        event_types = params.get("event_types", [])
        # Inbound SMS query
        if any(t in event_types for t in ("reply_received", "sms_inbound")):
            result.fetchone.return_value = (inbound_count,)
        # Booking query
        elif any(t in event_types for t in ("meeting_booked",)):
            result.fetchone.return_value = (booked_id,) if booked_id else None
        else:
            result.fetchone.return_value = None
        return result

    session.execute.side_effect = _execute
    return session


# ---------------------------------------------------------------------------
# get_inbound_sms_count
# ---------------------------------------------------------------------------

def test_inbound_count_zero():
    assert get_inbound_sms_count(_fake_session(inbound_count=0), contact_id=1) == 0


def test_inbound_count_nonzero():
    assert get_inbound_sms_count(_fake_session(inbound_count=3), contact_id=1) == 3


# ---------------------------------------------------------------------------
# get_booked_appointment_id
# ---------------------------------------------------------------------------

def test_no_booking_returns_none():
    assert get_booked_appointment_id(_fake_session(), contact_id=1) is None


def test_booking_returns_id():
    assert get_booked_appointment_id(_fake_session(booked_id=42), contact_id=1) == "42"


# ---------------------------------------------------------------------------
# is_cold_contact
# ---------------------------------------------------------------------------

def test_cold_when_no_inbound_no_booking():
    assert is_cold_contact(_fake_session(inbound_count=0, booked_id=None), 1) is True


def test_not_cold_when_has_inbound_sms():
    assert is_cold_contact(_fake_session(inbound_count=1, booked_id=None), 1) is False


def test_not_cold_when_has_booking():
    assert is_cold_contact(_fake_session(inbound_count=0, booked_id=99), 1) is False


def test_not_cold_when_has_both():
    assert is_cold_contact(_fake_session(inbound_count=2, booked_id=99), 1) is False


# ---------------------------------------------------------------------------
# assert_not_cold_sms — the CI/CD hard gate
# ---------------------------------------------------------------------------

def test_gate_blocks_cold_contact():
    """BUILD MUST FAIL if this gate does not raise for a cold contact."""
    with pytest.raises(ValueError, match="Cold SMS blocked"):
        assert_not_cold_sms(_fake_session(inbound_count=0, booked_id=None), contact_id=7)


def test_gate_passes_after_inbound_sms():
    """Gate must not block a contact who has replied via SMS."""
    assert_not_cold_sms(_fake_session(inbound_count=1, booked_id=None), contact_id=7)


def test_gate_passes_after_booking():
    """Gate must not block a contact with a confirmed booking."""
    assert_not_cold_sms(_fake_session(inbound_count=0, booked_id=55), contact_id=7)


def test_gate_error_message_contains_contact_id():
    """Error message must identify the contact for dead-letter routing."""
    with pytest.raises(ValueError, match="contact_id=7"):
        assert_not_cold_sms(_fake_session(inbound_count=0, booked_id=None), contact_id=7)


def test_gate_error_message_contains_reason():
    """Error message must state the blocking condition for audit trail."""
    with pytest.raises(ValueError, match="inbound_sms_count=0"):
        assert_not_cold_sms(_fake_session(inbound_count=0, booked_id=None), contact_id=7)
