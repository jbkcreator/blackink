"""Unit tests for the per-client Client Wins Dashboard (S-21) — no DB required.

The DB and Google Sheets are mocked (same pattern as test_seed_demo_sandbox);
these cover the metric assembly, the NOT_RECORDED honesty for unbuilt fields,
the CSV quoting, and the export's fail-safe/skip contracts.
"""

import csv
import io
from unittest.mock import MagicMock, patch

from src.services.client_wins import (
    NOT_RECORDED,
    WINS_HEADER,
    compute_wins,
    export_client_wins,
    wins_csv,
)


def _session_returning(attended, meetings, agreements, doors, packets) -> MagicMock:
    """A fake session whose four compute_wins queries return, in order:
    attended count, meetings count, (agreements, doors) row, packets count."""
    session = MagicMock()
    scalars = iter([attended, meetings, packets])  # scalar_one() calls, in order
    first_row = (agreements, doors)

    def execute(*_args, **_kwargs):
        res = MagicMock()
        # scalar_one is used for attended, meetings, packets; first for agreements row
        res.scalar_one.side_effect = lambda: next(scalars)
        res.first.return_value = first_row
        return res

    session.execute.side_effect = execute
    return session


def test_compute_wins_assembles_all_metrics():
    session = _session_returning(attended=3, meetings=5, agreements=2, doors=47, packets=2)
    with patch("src.services.client_wins.get_system_db_context") as ctx:
        ctx.return_value.__enter__.return_value = session
        rows = compute_wins("ACME")

    d = dict(rows)
    assert d["Attended Discovery Appointments"] == "3"
    assert d["Meetings Booked"] == "5"
    assert d["Signed Management Agreements"] == "2"
    assert d["Total Doors Signed"] == "47"
    assert d["Downloadable Evidence Packets"] == "2"


def test_unbuilt_metrics_render_not_recorded_not_zero():
    """Saved doors + ancillary revenue have no producer — they must say
    NOT RECORDED, never a fabricated 0 that reads as 'no wins'."""
    session = _session_returning(0, 0, 0, 0, 0)
    with patch("src.services.client_wins.get_system_db_context") as ctx:
        ctx.return_value.__enter__.return_value = session
        d = dict(compute_wins("ACME"))
    assert d["Saved Doors (Churn Tripwire)"].startswith(NOT_RECORDED)
    assert d["Ancillary Revenue"].startswith(NOT_RECORDED)


def test_wins_csv_quotes_comma_bearing_values():
    session = _session_returning(1, 1, 1, 1, 1)
    with patch("src.services.client_wins.get_system_db_context") as ctx:
        ctx.return_value.__enter__.return_value = session
        out = wins_csv("ACME")

    parsed = list(csv.reader(io.StringIO(out)))
    assert parsed[0] == list(WINS_HEADER)
    # The NOT_RECORDED rows contain a comma/em-dash — must survive as one field.
    saved = [r for r in parsed if r[0] == "Saved Doors (Churn Tripwire)"][0]
    assert saved[1].startswith(NOT_RECORDED)
    assert len(saved) == 2  # not split into extra columns


def test_export_skips_when_credentials_unset():
    fake_settings = MagicMock(google_sheets_credentials_path=None)
    with patch("src.services.client_wins.get_settings", return_value=fake_settings), \
         patch("src.services.client_wins.gspread") as gs, \
         patch("src.services.client_wins.get_system_db_context") as ctx:
        assert export_client_wins("ACME", "sheet-123") == 0
    gs.service_account.assert_not_called()
    ctx.assert_not_called()


def test_export_skips_when_no_sheet_id():
    fake_settings = MagicMock(google_sheets_credentials_path="secrets/fake.json")
    with patch("src.services.client_wins.get_settings", return_value=fake_settings), \
         patch("src.services.client_wins.gspread") as gs:
        assert export_client_wins("ACME", "") == 0
    gs.service_account.assert_not_called()


def test_export_writes_header_and_rows():
    fake_settings = MagicMock(google_sheets_credentials_path="secrets/fake.json")
    session = _session_returning(3, 5, 2, 47, 2)
    with patch("src.services.client_wins.get_settings", return_value=fake_settings), \
         patch("src.services.client_wins.get_system_db_context") as ctx, \
         patch("src.services.client_wins.gspread") as gs:
        ctx.return_value.__enter__.return_value = session
        worksheet = MagicMock()
        gs.service_account.return_value.open_by_key.return_value.sheet1 = worksheet
        result = export_client_wins("ACME", "sheet-123")

    gs.service_account.assert_called_once_with(filename="secrets/fake.json")
    gs.service_account.return_value.open_by_key.assert_called_once_with("sheet-123")
    worksheet.clear.assert_called_once()
    written = worksheet.update.call_args[0][0]
    assert written[0] == list(WINS_HEADER)
    assert result == 7  # five real metrics + two NOT_RECORDED rows


def test_export_failure_does_not_raise():
    fake_settings = MagicMock(google_sheets_credentials_path="secrets/fake.json")
    session = _session_returning(0, 0, 0, 0, 0)
    with patch("src.services.client_wins.get_settings", return_value=fake_settings), \
         patch("src.services.client_wins.get_system_db_context") as ctx, \
         patch("src.services.client_wins.gspread") as gs:
        ctx.return_value.__enter__.return_value = session
        gs.service_account.side_effect = RuntimeError("auth failed")
        assert export_client_wins("ACME", "sheet-123") == 0
