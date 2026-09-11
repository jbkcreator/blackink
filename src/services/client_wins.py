"""Per-client Client Wins Dashboard (Subtask 3.2.5 Stage 6 / blueprint §3.3.5 A
DASH-WINS, dev item S-21).

Computes a paying tenant's wins and (a) exports them to that tenant's own Google
Sheet (four tabs, for a client-grade Looker Studio report) and (b) serves them
as CSV via src/api/client_wins_router.py (AC#5 export).

Why Sheets, not Looker-direct-to-Postgres: the same deliberate choice made for
the sandbox dashboard (see src/tasks/seed_demo_sandbox.export_dashboard_to_sheet
and migrations/apply_sandbox_dashboard_view.py) — pointing Looker at Postgres
would require opening the shared production DB to Google's connector IP range.
The Sheet is the buffer; reads happen server-side under the system role
(BYPASSRLS, scoped by an explicit :client_id bind), so there is no Looker-side
tenant context and therefore no RLS/security_invoker surface at all.

Four tabs, one per client-facing question ("what did I get for my money?"):
  * Summary  — the honest long Metric/Value table incl NOT RECORDED lines.
  * KPIs     — one wide numeric row → Looker Scorecard tiles + funnel.
  * Wins     — one row per signed agreement (date, doors, evidence-packet URL)
               → the downloadable-proof table.
  * Trend    — weekly meetings/attended/doors → a momentum line chart.

Metric honesty (repo ethos: NOT RECORDED over a fabricated value): saved doors
(Churn Tripwire) and ancillary revenue are Week-3 / unbuilt, so the Summary tab
renders the literal ``NOT RECORDED`` — never a plausible 0 that reads as "no
wins". The numeric tabs simply omit those two (a Scorecard has no honest way to
show "unknown").
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import gspread
from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_system_db_context

logger = logging.getLogger(__name__)

NOT_RECORDED = "NOT RECORDED"
WINS_HEADER: Tuple[str, str] = ("Metric", "Value")

KPI_HEADER = [
    "Meetings Booked", "Attended Appointments", "Signed Management Agreements",
    "Total Doors Signed", "Downloadable Evidence Packets",
]
WINS_DETAIL_HEADER = ["Signed Date", "Doors", "Source", "Status", "Evidence Packet"]
TREND_HEADER = ["Week", "Meetings Booked", "Attended Appointments", "Doors Signed"]

_ATTENDED_SQL = "SELECT COUNT(*) FROM appointments WHERE client_id = :client_id AND state = 'ATTENDED'"
_MEETINGS_SQL = "SELECT COUNT(*) FROM events WHERE client_id = :client_id AND event_type = 'meeting_booked'"
_AGREEMENTS_SQL = "SELECT COUNT(*), COALESCE(SUM(door_count), 0) FROM pms_agreements WHERE client_id = :client_id"
_PACKETS_SQL = (
    "SELECT COUNT(*) FROM settlement_transactions "
    "WHERE client_id = :client_id AND evidence_packet_url IS NOT NULL"
)
# One row per signed agreement, newest first, with its evidence packet URL (from
# the settlement ledger, joined on the same-tenant composite key) when published.
_WINS_DETAIL_SQL = """
    SELECT to_char(pa.door_signed_at, 'YYYY-MM-DD') AS signed_date,
           pa.door_count,
           pa.agreement_source,
           pa.status,
           COALESCE(st.evidence_packet_url, '') AS evidence_packet_url
    FROM pms_agreements pa
    LEFT JOIN settlement_transactions st
      ON st.client_id = pa.client_id AND st.pms_agreement_id = pa.pms_agreement_id
    WHERE pa.client_id = :client_id
    ORDER BY pa.door_signed_at DESC
"""
# Weekly buckets — three independent grouped scalars merged by week in Python
# (a single SQL FULL OUTER JOIN over three date series is harder to read and no
# faster at this row count).
_TREND_DOORS_SQL = (
    "SELECT to_char(date_trunc('week', door_signed_at), 'YYYY-MM-DD') AS wk, COALESCE(SUM(door_count), 0) "
    "FROM pms_agreements WHERE client_id = :client_id GROUP BY wk"
)
_TREND_ATTENDED_SQL = (
    "SELECT to_char(date_trunc('week', scheduled_for), 'YYYY-MM-DD') AS wk, COUNT(*) "
    "FROM appointments WHERE client_id = :client_id AND state = 'ATTENDED' GROUP BY wk"
)
_TREND_MEETINGS_SQL = (
    "SELECT to_char(date_trunc('week', created_at), 'YYYY-MM-DD') AS wk, COUNT(*) "
    "FROM events WHERE client_id = :client_id AND event_type = 'meeting_booked' GROUP BY wk"
)


def _kpis(session, client_id: str):
    attended = session.execute(text(_ATTENDED_SQL), {"client_id": client_id}).scalar_one()
    meetings = session.execute(text(_MEETINGS_SQL), {"client_id": client_id}).scalar_one()
    agg = session.execute(text(_AGREEMENTS_SQL), {"client_id": client_id}).first()
    packets = session.execute(text(_PACKETS_SQL), {"client_id": client_id}).scalar_one()
    signed = agg[0] if agg else 0
    doors = agg[1] if agg else 0
    return meetings, attended, signed, doors, packets


def compute_wins(client_id: str) -> List[Tuple[str, str]]:
    """The Summary tab: honest (Metric, Value) rows incl NOT RECORDED lines."""
    with get_system_db_context() as session:
        meetings, attended, signed, doors, packets = _kpis(session, client_id)
    return [
        ("Attended Discovery Appointments", str(attended)),
        ("Meetings Booked", str(meetings)),
        ("Signed Management Agreements", str(signed)),
        ("Total Doors Signed", str(doors)),
        ("Downloadable Evidence Packets", str(packets)),
        ("Saved Doors (Churn Tripwire)", f"{NOT_RECORDED} — Churn Tripwire not built (Week 3)"),
        ("Ancillary Revenue", f"{NOT_RECORDED} — no ancillary-revenue source in schema"),
    ]


def compute_kpis_wide(client_id: str) -> List[list]:
    """The KPIs tab: header + one wide numeric row → Scorecards / funnel."""
    with get_system_db_context() as session:
        meetings, attended, signed, doors, packets = _kpis(session, client_id)
    return [KPI_HEADER, [meetings, attended, signed, doors, packets]]


def compute_wins_detail(client_id: str) -> List[list]:
    """The Wins tab: header + one row per signed agreement (with evidence URL)."""
    with get_system_db_context() as session:
        rows = session.execute(text(_WINS_DETAIL_SQL), {"client_id": client_id}).fetchall()
    return [WINS_DETAIL_HEADER] + [list(r) for r in rows]


def compute_weekly_trend(client_id: str) -> List[list]:
    """The Trend tab: header + one row per week (meetings, attended, doors)."""
    with get_system_db_context() as session:
        doors = dict(session.execute(text(_TREND_DOORS_SQL), {"client_id": client_id}).fetchall())
        attended = dict(session.execute(text(_TREND_ATTENDED_SQL), {"client_id": client_id}).fetchall())
        meetings = dict(session.execute(text(_TREND_MEETINGS_SQL), {"client_id": client_id}).fetchall())
    weeks = sorted(set(doors) | set(attended) | set(meetings))
    return [TREND_HEADER] + [
        [wk, int(meetings.get(wk, 0)), int(attended.get(wk, 0)), int(doors.get(wk, 0))]
        for wk in weeks
    ]


def _write_tab(spreadsheet, title: str, values: List[list]) -> None:
    """Write `values` (header + rows) to the named worksheet tab, creating it if
    absent. Cleared first so a shrinking dataset never leaves stale trailing
    rows."""
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=max(len(values) + 5, 20), cols=max(len(values[0]) if values else 2, 5))
    ws.clear()
    ws.update(values)


def export_client_wins(client_id: str, sheet_id: str) -> int:
    """Write all four tabs into the client's Google Sheet. Returns the number of
    Summary metric rows written, or 0 if export is unconfigured/unreachable —
    never raises, so a Sheets outage can never fail the sweep that calls this.
    """
    settings = get_settings()
    if not settings.google_sheets_credentials_path:
        logger.info("export_client_wins: GOOGLE_SHEETS_CREDENTIALS_PATH not set, skipping")
        return 0
    if not sheet_id:
        logger.info("export_client_wins: client %s has no wins_sheet_id, skipping", client_id)
        return 0

    summary = compute_wins(client_id)
    try:
        sh = gspread.service_account(filename=settings.google_sheets_credentials_path).open_by_key(sheet_id)
        _write_tab(sh, "Summary", [list(WINS_HEADER)] + [list(r) for r in summary])
        _write_tab(sh, "KPIs", compute_kpis_wide(client_id))
        _write_tab(sh, "Wins", compute_wins_detail(client_id))
        _write_tab(sh, "Trend", compute_weekly_trend(client_id))
    except Exception:
        logger.warning("export_client_wins: failed to update sheet for client %s", client_id, exc_info=True)
        return 0

    return len(summary)


def wins_csv(client_id: str) -> str:
    """The client's Summary wins as CSV text (header + rows) — the AC#5 export."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(WINS_HEADER)
    writer.writerows(compute_wins(client_id))
    return buf.getvalue()
