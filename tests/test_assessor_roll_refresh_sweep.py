"""Unit tests for src/tasks/assessor_roll_refresh_sweep.py (S-24)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.services.assessor_roll_loader import ImportResult
from src.tasks.assessor_roll_refresh_sweep import _check_staleness, _read_file, _run_county


def test_read_file_returns_none_for_missing_path(tmp_path):
    assert _read_file(str(tmp_path / "does_not_exist.csv")) is None


def test_read_file_returns_none_for_empty_path_string():
    assert _read_file("") is None


def test_read_file_reads_real_file(tmp_path):
    p = tmp_path / "roll.csv"
    p.write_bytes(b"parcel_address,owner_name\n123 Main St,John Smith\n")
    assert _read_file(str(p)) == b"parcel_address,owner_name\n123 Main St,John Smith\n"


def test_run_county_missing_file_alerts_and_records_missing(tmp_path):
    db = MagicMock()
    db.execute.return_value.first.return_value = None
    missing_path = str(tmp_path / "nope.csv")
    with patch("src.tasks.assessor_roll_refresh_sweep.asyncio.run",
                side_effect=lambda coro: coro.close()) as mock_run:
        status = _run_county(db, "hillsborough_fl", missing_path, datetime.now(timezone.utc), 400)
    assert status == "MISSING"
    assert mock_run.called
    insert_calls = [c for c in db.execute.call_args_list if "INSERT INTO assessor_roll_imports" in str(c[0][0])]
    assert len(insert_calls) == 1
    assert insert_calls[0][0][1]["status"] == "MISSING"


def test_run_county_missing_file_on_never_imported_county_fires_exactly_one_alert(tmp_path):
    """Code-review fix: a never-imported county with a missing file used to
    fire BOTH a MISSING alert and a redundant, misleadingly-worded STALE
    alert ('no successful import since {now}') every single tick. Only the
    MISSING alert should fire."""
    db = MagicMock()
    db.execute.return_value.first.return_value = None  # never imported at all
    missing_path = str(tmp_path / "nope.csv")
    with patch("src.tasks.assessor_roll_refresh_sweep.asyncio.run",
                side_effect=lambda coro: coro.close()) as mock_run:
        _run_county(db, "pinellas_fl", missing_path, datetime.now(timezone.utc), 400)
    assert mock_run.call_count == 1


def test_run_county_failed_on_never_imported_county_fires_exactly_one_alert(tmp_path):
    p = tmp_path / "roll.csv"
    p.write_bytes(b"wrong_columns\nvalue\n")
    db = MagicMock()
    db.execute.return_value.first.return_value = None
    with patch("src.tasks.assessor_roll_refresh_sweep.asyncio.run",
                side_effect=lambda coro: coro.close()) as mock_run:
        _run_county(db, "hillsborough_fl", str(p), datetime.now(timezone.utc), 400)
    assert mock_run.call_count == 1


def test_run_county_success_does_not_alert(tmp_path):
    p = tmp_path / "roll.csv"
    p.write_bytes(b"parcel_address,owner_name\n123 Main St,John Smith\n")
    db = MagicMock()
    db.execute.return_value.first.return_value = None  # no prior import -> first-ever SUCCESS
    with patch("src.tasks.assessor_roll_refresh_sweep.asyncio.run",
                side_effect=lambda coro: coro.close()) as mock_run:
        status = _run_county(db, "hillsborough_fl", str(p), datetime.now(timezone.utc), 400)
    assert status == "SUCCESS"
    assert not mock_run.called


def test_run_county_failed_parse_alerts(tmp_path):
    p = tmp_path / "roll.csv"
    p.write_bytes(b"wrong_columns\nvalue\n")
    db = MagicMock()
    db.execute.return_value.first.return_value = None
    with patch("src.tasks.assessor_roll_refresh_sweep.asyncio.run",
                side_effect=lambda coro: coro.close()) as mock_run:
        status = _run_county(db, "hillsborough_fl", str(p), datetime.now(timezone.utc), 400)
    assert status == "FAILED"
    assert mock_run.called


# ---------------------------------------------------------------------------
# _check_staleness boundary tests
# ---------------------------------------------------------------------------

def test_staleness_never_imported_returns_none():
    """Code-review fix: a county with no prior SUCCESS at all is NOT a
    'stale' case on its own — that's fully covered by the MISSING/FAILED
    alert already firing every tick the file is unprovisioned/broken. This
    used to return `as_of` itself, producing a misleading 'no successful
    import since {right now}' alert alongside the MISSING/FAILED one."""
    db = MagicMock()
    db.execute.return_value.first.return_value = None
    as_of = datetime.now(timezone.utc)
    result = _check_staleness(db, "hillsborough_fl", as_of, 400)
    assert result is None


def test_staleness_recent_import_is_not_stale():
    db = MagicMock()
    recent = datetime.now(timezone.utc) - timedelta(days=10)
    db.execute.return_value.first.return_value = (recent,)
    result = _check_staleness(db, "hillsborough_fl", datetime.now(timezone.utc), 400)
    assert result is None


def test_staleness_boundary_exactly_at_threshold_is_not_yet_stale():
    as_of = datetime.now(timezone.utc)
    last_success = as_of - timedelta(days=400)
    db = MagicMock()
    db.execute.return_value.first.return_value = (last_success,)
    result = _check_staleness(db, "hillsborough_fl", as_of, 400)
    assert result is None  # exactly 400 days is not > 400


def test_staleness_boundary_one_day_past_threshold_is_stale():
    as_of = datetime.now(timezone.utc)
    last_success = as_of - timedelta(days=401)
    db = MagicMock()
    db.execute.return_value.first.return_value = (last_success,)
    result = _check_staleness(db, "hillsborough_fl", as_of, 400)
    assert result == last_success
