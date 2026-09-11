"""Unit tests for the per-client Client Wins Dashboard (S-21) — no DB required.

DB and Google Sheets are mocked (same pattern as test_seed_demo_sandbox). Covers
the Summary metric assembly + NOT_RECORDED honesty, the KPI/Wins/Trend tab
shapes, CSV quoting, and the multi-tab export's skip/fail-safe contracts.
"""

import csv
import io
from unittest.mock import MagicMock, patch

from src.services.client_wins import (
    KPI_HEADER,
    NOT_RECORDED,
    TREND_HEADER,
    WINS_DETAIL_HEADER,
    WINS_HEADER,
    compute_kpis_wide,
    compute_weekly_trend,
    compute_wins,
    compute_wins_detail,
    export_client_wins,
    wins_csv,
)


def _kpi_session(attended, meetings, agreements, doors, packets) -> MagicMock:
    """Fake session for the 4 KPI queries: attended, meetings (scalar_one),
    agreements row (first), packets (scalar_one)."""
    session = MagicMock()
    scalars = iter([attended, meetings, packets])
    row = (agreements, doors)

    def execute(*_a, **_k):
        res = MagicMock()
        res.scalar_one.side_effect = lambda: next(scalars)
        res.first.return_value = row
        return res

    session.execute.side_effect = execute
    return session


def _fetchall_session(*result_sets) -> MagicMock:
    """Fake session returning the given fetchall() result sets in call order."""
    session = MagicMock()
    it = iter(result_sets)

    def execute(*_a, **_k):
        res = MagicMock()
        res.fetchall.return_value = next(it)
        return res

    session.execute.side_effect = execute
    return session


def _ctx(session):
    m = MagicMock()
    m.return_value.__enter__.return_value = session
    return m


# ── Summary tab ───────────────────────────────────────────────────────────────

def test_compute_wins_assembles_all_metrics():
    with patch("src.services.client_wins.get_system_db_context", _ctx(_kpi_session(3, 5, 2, 47, 2))):
        d = dict(compute_wins("ACME"))
    assert d["Attended Discovery Appointments"] == "3"
    assert d["Meetings Booked"] == "5"
    assert d["Signed Management Agreements"] == "2"
    assert d["Total Doors Signed"] == "47"
    assert d["Downloadable Evidence Packets"] == "2"


def test_unbuilt_metrics_render_not_recorded_not_zero():
    with patch("src.services.client_wins.get_system_db_context", _ctx(_kpi_session(0, 0, 0, 0, 0))):
        d = dict(compute_wins("ACME"))
    assert d["Saved Doors (Churn Tripwire)"].startswith(NOT_RECORDED)
    assert d["Ancillary Revenue"].startswith(NOT_RECORDED)


# ── KPIs tab (wide numeric) ───────────────────────────────────────────────────

def test_compute_kpis_wide_is_header_plus_one_numeric_row():
    with patch("src.services.client_wins.get_system_db_context", _ctx(_kpi_session(3, 5, 2, 47, 2))):
        out = compute_kpis_wide("ACME")
    assert out[0] == KPI_HEADER
    # meetings, attended, signed, doors, packets — numeric, not strings
    assert out[1] == [5, 3, 2, 47, 2]


# ── Wins tab (detail rows with evidence links) ────────────────────────────────

def test_compute_wins_detail_rows_with_evidence():
    detail = [
        ("2026-09-05", 30, "PMS_SYNC", "ACTIVE", "https://files.example/packet1.pdf"),
        ("2026-09-01", 17, "SYNTHETIC", "ACTIVE", ""),
    ]
    with patch("src.services.client_wins.get_system_db_context", _ctx(_fetchall_session(detail))):
        out = compute_wins_detail("ACME")
    assert out[0] == WINS_DETAIL_HEADER
    assert out[1][1] == 30
    assert out[1][4] == "https://files.example/packet1.pdf"
    assert out[2][4] == ""  # unpublished packet -> blank, not a fake link


# ── Trend tab (weekly, merged across three series) ────────────────────────────

def test_compute_weekly_trend_merges_series_by_week():
    doors = [("2026-08-31", 47), ("2026-09-07", 12)]
    attended = [("2026-09-07", 2)]
    meetings = [("2026-08-31", 3), ("2026-09-07", 4)]
    with patch("src.services.client_wins.get_system_db_context", _ctx(_fetchall_session(doors, attended, meetings))):
        out = compute_weekly_trend("ACME")
    assert out[0] == TREND_HEADER
    # weeks sorted; row = [week, meetings, attended, doors]
    assert out[1] == ["2026-08-31", 3, 0, 47]
    assert out[2] == ["2026-09-07", 4, 2, 12]


# ── CSV export ────────────────────────────────────────────────────────────────

def test_wins_csv_quotes_comma_bearing_values():
    with patch("src.services.client_wins.get_system_db_context", _ctx(_kpi_session(1, 1, 1, 1, 1))):
        out = wins_csv("ACME")
    parsed = list(csv.reader(io.StringIO(out)))
    assert parsed[0] == list(WINS_HEADER)
    saved = [r for r in parsed if r[0] == "Saved Doors (Churn Tripwire)"][0]
    assert saved[1].startswith(NOT_RECORDED)
    assert len(saved) == 2  # comma-bearing value stays one field


# ── Multi-tab export contracts ────────────────────────────────────────────────

def test_export_skips_when_credentials_unset():
    with patch("src.services.client_wins.get_settings", return_value=MagicMock(google_sheets_credentials_path=None)), \
         patch("src.services.client_wins.gspread") as gs, \
         patch("src.services.client_wins.get_system_db_context") as ctx:
        assert export_client_wins("ACME", "sheet-123") == 0
    gs.service_account.assert_not_called()
    ctx.assert_not_called()


def test_export_skips_when_no_sheet_id():
    with patch("src.services.client_wins.get_settings", return_value=MagicMock(google_sheets_credentials_path="k.json")), \
         patch("src.services.client_wins.gspread") as gs:
        assert export_client_wins("ACME", "") == 0
    gs.service_account.assert_not_called()


def test_export_writes_four_tabs():
    fake_settings = MagicMock(google_sheets_credentials_path="k.json")
    with patch("src.services.client_wins.get_settings", return_value=fake_settings), \
         patch("src.services.client_wins.gspread") as gs, \
         patch("src.services.client_wins.compute_wins", return_value=[("Meetings Booked", "5")]), \
         patch("src.services.client_wins.compute_kpis_wide", return_value=[KPI_HEADER, [5, 3, 2, 47, 2]]), \
         patch("src.services.client_wins.compute_wins_detail", return_value=[WINS_DETAIL_HEADER]), \
         patch("src.services.client_wins.compute_weekly_trend", return_value=[TREND_HEADER]):
        sh = MagicMock()
        ws = MagicMock()
        sh.worksheet.return_value = ws
        gs.service_account.return_value.open_by_key.return_value = sh
        result = export_client_wins("ACME", "sheet-123")

    titles = [c.args[0] for c in sh.worksheet.call_args_list]
    assert titles == ["Summary", "KPIs", "Wins", "Trend"]
    assert ws.clear.call_count == 4
    assert ws.update.call_count == 4
    assert result == 1  # len(summary rows)


def test_export_creates_missing_tab():
    import gspread as real_gspread
    fake_settings = MagicMock(google_sheets_credentials_path="k.json")
    with patch("src.services.client_wins.get_settings", return_value=fake_settings), \
         patch("src.services.client_wins.gspread") as gs, \
         patch("src.services.client_wins.compute_wins", return_value=[("Meetings Booked", "5")]), \
         patch("src.services.client_wins.compute_kpis_wide", return_value=[KPI_HEADER, [5, 3, 2, 47, 2]]), \
         patch("src.services.client_wins.compute_wins_detail", return_value=[WINS_DETAIL_HEADER]), \
         patch("src.services.client_wins.compute_weekly_trend", return_value=[TREND_HEADER]):
        gs.WorksheetNotFound = real_gspread.WorksheetNotFound
        sh = MagicMock()
        sh.worksheet.side_effect = real_gspread.WorksheetNotFound("missing")
        newws = MagicMock()
        sh.add_worksheet.return_value = newws
        gs.service_account.return_value.open_by_key.return_value = sh
        assert export_client_wins("ACME", "sheet-123") == 1
    assert sh.add_worksheet.call_count == 4  # every tab created fresh


def test_export_failure_does_not_raise():
    fake_settings = MagicMock(google_sheets_credentials_path="k.json")
    with patch("src.services.client_wins.get_settings", return_value=fake_settings), \
         patch("src.services.client_wins.gspread") as gs, \
         patch("src.services.client_wins.compute_wins", return_value=[("Meetings Booked", "5")]):
        gs.service_account.side_effect = RuntimeError("auth failed")
        assert export_client_wins("ACME", "sheet-123") == 0
