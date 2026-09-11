"""Per-client Client Wins Dashboard (Subtask 3.2.5 Stage 6 / blueprint §3.3.5 A
DASH-WINS, dev item S-21).

Computes the wins metrics for a single paying tenant and (a) exports them to
that tenant's own Google Sheet for Looker Studio's Sheets connector, and (b)
serves them as CSV via src/api/client_wins_router.py (AC#5 export).

Why Sheets, not Looker-direct-to-Postgres: the same deliberate choice made for
the sandbox dashboard (see src/tasks/seed_demo_sandbox.export_dashboard_to_sheet
and migrations/apply_sandbox_dashboard_view.py) — pointing Looker Studio at
Postgres would require opening the shared production DB to Google's connector IP
range. The Sheet is the buffer; reads happen server-side under the system role
(BYPASSRLS, same as every other cross-tenant batch job), so there is no
Looker-side tenant context and therefore no RLS/security_invoker surface at all.

Metric honesty (repo ethos: NOT RECORDED over a fabricated value): of the five
DASH-WINS fields the blueprint names, three have real producers in this repo
today and two do not. Saved doors (Churn Tripwire) and ancillary revenue are
Week-3 / unbuilt, so they render the literal string ``NOT RECORDED`` with their
absent source noted — never a plausible-looking 0 that reads as "no wins".
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

# The wins are emitted as a (Metric, Value) table rather than a single wide row:
# it renders cleanly as Looker Studio tiles, and it lets a not-yet-built metric
# carry the literal NOT_RECORDED string alongside numeric metrics without a
# type clash (Sheet/CSV cells are text anyway).
WINS_HEADER: Tuple[str, str] = ("Metric", "Value")

# Each numeric metric is a scalar aggregate scoped to one client_id. Kept as
# named-bind text() per repo convention (never the ORM query API).
_ATTENDED_APPOINTMENTS_SQL = (
    "SELECT COUNT(*) FROM appointments WHERE client_id = :client_id AND state = 'ATTENDED'"
)
_MEETINGS_BOOKED_SQL = (
    "SELECT COUNT(*) FROM events WHERE client_id = :client_id AND event_type = 'meeting_booked'"
)
# A signed management agreement = a pms_agreements row (door_signed_at is NOT
# NULL on that table). Door counts sum the agreements' recorded door_count.
_SIGNED_AGREEMENTS_SQL = (
    "SELECT COUNT(*), COALESCE(SUM(door_count), 0) "
    "FROM pms_agreements WHERE client_id = :client_id"
)
# Evidence Packets that actually have a published URL (the row exists before the
# packet is compiled, so filter on a non-null URL — an unpublished packet is not
# a downloadable win yet).
_EVIDENCE_PACKETS_SQL = (
    "SELECT COUNT(*) FROM settlement_transactions "
    "WHERE client_id = :client_id AND evidence_packet_url IS NOT NULL"
)


def compute_wins(client_id: str) -> List[Tuple[str, str]]:
    """Return the client's wins as a list of (Metric, Value) rows.

    Reads under the system role (BYPASSRLS) because this is a cross-tenant batch
    surface scoped explicitly by the :client_id bind — never a session-context
    scope. Every value is stringified so a numeric metric and a NOT_RECORDED
    metric coexist in the same table.
    """
    with get_system_db_context() as session:
        attended = session.execute(
            text(_ATTENDED_APPOINTMENTS_SQL), {"client_id": client_id}
        ).scalar_one()
        meetings = session.execute(
            text(_MEETINGS_BOOKED_SQL), {"client_id": client_id}
        ).scalar_one()
        agreements_row = session.execute(
            text(_SIGNED_AGREEMENTS_SQL), {"client_id": client_id}
        ).first()
        packets = session.execute(
            text(_EVIDENCE_PACKETS_SQL), {"client_id": client_id}
        ).scalar_one()

    signed_agreements = agreements_row[0] if agreements_row else 0
    doors_signed = agreements_row[1] if agreements_row else 0

    return [
        ("Attended Discovery Appointments", str(attended)),
        ("Meetings Booked", str(meetings)),
        ("Signed Management Agreements", str(signed_agreements)),
        ("Total Doors Signed", str(doors_signed)),
        ("Downloadable Evidence Packets", str(packets)),
        # Blueprint DASH-WINS fields with no producer in this repo yet — stated
        # plainly rather than shown as a misleading 0. See module docstring.
        ("Saved Doors (Churn Tripwire)", f"{NOT_RECORDED} — Churn Tripwire not built (Week 3)"),
        ("Ancillary Revenue", f"{NOT_RECORDED} — no ancillary-revenue source in schema"),
    ]


def export_client_wins(client_id: str, sheet_id: str) -> int:
    """Write one client's wins into its Google Sheet. Returns the number of
    metric rows written, or 0 if export is unconfigured/unreachable — never
    raises, so a Sheets outage can never fail the sweep that calls this (same
    contract as export_dashboard_to_sheet).
    """
    settings = get_settings()
    if not settings.google_sheets_credentials_path:
        logger.info("export_client_wins: GOOGLE_SHEETS_CREDENTIALS_PATH not set, skipping")
        return 0
    if not sheet_id:
        logger.info("export_client_wins: client %s has no wins_sheet_id, skipping", client_id)
        return 0

    rows = compute_wins(client_id)
    try:
        gc = gspread.service_account(filename=settings.google_sheets_credentials_path)
        worksheet = gc.open_by_key(sheet_id).sheet1
        worksheet.clear()
        worksheet.update([list(WINS_HEADER)] + [list(r) for r in rows])
    except Exception:
        logger.warning(
            "export_client_wins: failed to update sheet for client %s", client_id, exc_info=True
        )
        return 0

    return len(rows)


def wins_csv(client_id: str) -> str:
    """The client's wins as CSV text (header + rows) — the AC#5 export path.

    Uses the stdlib csv writer so a metric value containing a comma (the
    NOT_RECORDED explanation lines do) is correctly quoted rather than splitting
    a column.
    """
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(WINS_HEADER)
    writer.writerows(compute_wins(client_id))
    return buf.getvalue()
