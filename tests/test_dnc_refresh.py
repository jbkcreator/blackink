"""Tests for monthly DNC re-scrub task — FakeSession pattern, no live DB or API."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call
import pytest

from src.tasks.dnc_refresh import (
    _collect_contacts,
    _persist_results,
    run_dnc_refresh,
)


# ---------------------------------------------------------------------------
# _collect_contacts
# ---------------------------------------------------------------------------

def _session_with_contacts(rows):
    session = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = rows
    session.execute.return_value = result
    return session


def test_collect_normalizes_phone():
    session = _session_with_contacts([(1, "+18135550100")])
    contacts = _collect_contacts(session, datetime.now(timezone.utc), limit=None)
    assert contacts == [(1, "8135550100")]


def test_collect_skips_unnormalizable_phone():
    session = _session_with_contacts([(1, "abc")])
    contacts = _collect_contacts(session, datetime.now(timezone.utc), limit=None)
    assert contacts == []


def test_collect_returns_all_rows():
    session = _session_with_contacts([(1, "8135550100"), (2, "7275550100")])
    contacts = _collect_contacts(session, datetime.now(timezone.utc), limit=None)
    assert len(contacts) == 2


# ---------------------------------------------------------------------------
# _persist_results
# ---------------------------------------------------------------------------

def _make_stats():
    return {"total": 0, "clean": 0, "dnc_hits": 0, "failed": 0, "unmatched": 0, "skipped": False}


def test_persist_marks_dnc_clean_false_for_national_dnc():
    session = MagicMock()
    stats = _make_stats()
    _persist_results(
        session,
        [{"phone": "8135550100", "national_dnc": "Y", "litigator": "N"}],
        {"8135550100": [1]},
        stats,
    )
    assert stats["dnc_hits"] == 1
    assert stats["clean"] == 0
    sql = str(session.execute.call_args_list[0][0][0])
    assert "dnc_clean" in sql


def test_persist_marks_dnc_clean_true_when_both_false():
    session = MagicMock()
    stats = _make_stats()
    _persist_results(
        session,
        [{"phone": "8135550100", "national_dnc": "N", "litigator": "N"}],
        {"8135550100": [1]},
        stats,
    )
    assert stats["clean"] == 1
    assert stats["dnc_hits"] == 0


def test_persist_litigator_flag_blocks():
    session = MagicMock()
    stats = _make_stats()
    _persist_results(
        session,
        [{"phone": "8135550100", "national_dnc": "N", "litigator": "Y"}],
        {"8135550100": [1]},
        stats,
    )
    assert stats["dnc_hits"] == 1


def test_persist_unmatched_phone_increments_stat():
    session = MagicMock()
    stats = _make_stats()
    _persist_results(
        session,
        [{"phone": "9995550100", "national_dnc": "N", "litigator": "N"}],
        {"8135550100": [1]},  # 9995550100 not in map
        stats,
    )
    assert stats["unmatched"] == 1


def test_persist_multiple_contacts_same_phone():
    session = MagicMock()
    stats = _make_stats()
    _persist_results(
        session,
        [{"phone": "8135550100", "national_dnc": "Y", "litigator": "N"}],
        {"8135550100": [1, 2, 3]},
        stats,
    )
    params = session.execute.call_args_list[0][0][1]
    assert params["ids"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# run_dnc_refresh — integration shape
# ---------------------------------------------------------------------------

def test_run_skips_when_no_api_key():
    with patch("src.tasks.dnc_refresh.get_settings") as mock_settings:
        mock_settings.return_value.dnc_vendor_api_key = None
        result = run_dnc_refresh()
    assert result["skipped"] is True


def test_run_dry_run_no_api_call():
    with patch("src.tasks.dnc_refresh.get_settings") as mock_settings, \
         patch("src.tasks.dnc_refresh.get_system_db_context") as mock_ctx, \
         patch("src.tasks.dnc_refresh._submit_batch") as mock_submit:

        mock_key = MagicMock()
        mock_key.get_secret_value.return_value = "fake-key"
        mock_settings.return_value.dnc_vendor_api_key = mock_key
        mock_settings.return_value.dnc_recheck_days = 30

        mock_session = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchall.return_value = [(1, "8135550100")]
        mock_session.execute.return_value = result_mock
        mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        result = run_dnc_refresh(dry_run=True)

    mock_submit.assert_not_called()
    assert result["total"] == 1


def test_run_returns_zero_when_no_contacts():
    with patch("src.tasks.dnc_refresh.get_settings") as mock_settings, \
         patch("src.tasks.dnc_refresh.get_system_db_context") as mock_ctx:

        mock_key = MagicMock()
        mock_key.get_secret_value.return_value = "fake-key"
        mock_settings.return_value.dnc_vendor_api_key = mock_key
        mock_settings.return_value.dnc_recheck_days = 30

        mock_session = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchall.return_value = []
        mock_session.execute.return_value = result_mock
        mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

        result = run_dnc_refresh()

    assert result["total"] == 0
