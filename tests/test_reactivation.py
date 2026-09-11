"""Unit tests for src/services/reactivation.py (S-10 — LATER-intent
reactivation date extraction and pause)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.services.reactivation import extract_target_date, pause_contact_until, _MAX_FUTURE_DAYS


_AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_empty_text_returns_none():
    assert extract_target_date("", as_of=_AS_OF) is None
    assert extract_target_date("   ", as_of=_AS_OF) is None


def test_clear_future_date_is_extracted():
    result = extract_target_date("Let's talk again on March 15, 2026", as_of=_AS_OF)
    assert result is not None
    assert result.year == 2026 and result.month == 3 and result.day == 15


def test_no_date_shaped_text_returns_none():
    assert extract_target_date("not interested right now, maybe later", as_of=_AS_OF) is None


def test_past_date_is_rejected():
    result = extract_target_date("we spoke back on January 1, 2020", as_of=_AS_OF)
    assert result is None


def test_date_exactly_at_as_of_is_rejected():
    """Boundary: a date equal to as_of is not a future date."""
    text = _AS_OF.strftime("%B %d, %Y")
    result = extract_target_date(f"call me on {text}", as_of=_AS_OF)
    assert result is None


def test_date_one_day_past_boundary_is_accepted():
    text = (_AS_OF + timedelta(days=_MAX_FUTURE_DAYS)).strftime("%B %d, %Y")
    result = extract_target_date(f"circle back on {text}", as_of=_AS_OF)
    assert result is not None


def test_date_beyond_max_future_window_is_rejected():
    text = (_AS_OF + timedelta(days=_MAX_FUTURE_DAYS + 1)).strftime("%B %d, %Y")
    result = extract_target_date(f"circle back on {text}", as_of=_AS_OF)
    assert result is None


def test_unparseable_garbage_returns_none():
    assert extract_target_date("asdkjfh 9999999999999999999999", as_of=_AS_OF) is None


# ---------------------------------------------------------------------------
# pause_contact_until
# ---------------------------------------------------------------------------

def test_pause_applies_when_no_existing_pause_reason():
    session = MagicMock()
    session.execute.return_value.first.return_value = (42,)
    target = _AS_OF + timedelta(days=30)
    applied = pause_contact_until(session, 42, target)
    assert applied is True
    args, kwargs = session.execute.call_args
    params = args[1]
    assert params["contact_id"] == 42
    assert params["target_date"] == target


def test_pause_does_not_clobber_existing_different_reason():
    """The WHERE clause guard means an existing NO_SHOW_RECOVERY pause blocks
    the UPDATE from matching any row — simulated here by RETURNING nothing."""
    session = MagicMock()
    session.execute.return_value.first.return_value = None
    target = _AS_OF + timedelta(days=30)
    applied = pause_contact_until(session, 42, target)
    assert applied is False
