"""Seed a demo tenant with realistic Client Wins data across several weeks, then
export it to the Google Sheet so the Looker Studio dashboard (Scorecards +
funnel + wins table + weekly trend) can be reviewed with real-looking numbers.

Intended to be run on the SERVER via Claude Code (DB is local there). It seeds a
persistent demo client (DEMO_CLIENT_WINS) — not a throwaway — so the dashboard
keeps showing data; re-running refreshes it, and --cleanup removes it.

    # seed + export
    PYTHONPATH=. GOOGLE_SHEETS_CREDENTIALS_PATH=/root/blackink/gsheets-sa.json \
        python scripts/seed_client_wins_demo.py --sheet-id <SHEET_ID>

    # remove the demo tenant and its rows
    PYTHONPATH=. python scripts/seed_client_wins_demo.py --cleanup

Evidence-packet links are intentionally left blank: settlement_transactions is
gated by the billing offer-config + guard trigger and is not seeded here, so the
Wins tab's Evidence Packet column populates only once a real packet is published.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from sqlalchemy import text

from src.core.database import get_owner_db_context

CLIENT_ID = "DEMO_CLIENT_WINS"
DEFAULT_SHEET_ID = "1_HHeaCyXDxASDcHqv2tsPq_sDk4YX3CBqlgMx5hrruY"

# (door_signed offset days ago, door_count) — three signed agreements across 3 weeks.
AGREEMENTS = [(21, 30), (14, 17), (7, 25)]
# ATTENDED appointments, offsets in days ago.
ATTENDED_DAYS = [20, 16, 12, 6, 2]
# meeting_booked events, offsets in days ago.
MEETING_DAYS = [21, 19, 16, 14, 9, 6, 3, 1]


def owner_exec(sql: str, **params) -> None:
    with get_owner_db_context() as s:
        s.execute(text(sql), params)
        s.commit()


def cleanup() -> None:
    owner_exec("DELETE FROM events WHERE client_id = :c", c=CLIENT_ID)
    owner_exec("DELETE FROM appointments WHERE client_id = :c", c=CLIENT_ID)
    owner_exec("DELETE FROM pms_agreements WHERE client_id = :c", c=CLIENT_ID)
    owner_exec("DELETE FROM clients WHERE client_id = :c", c=CLIENT_ID)
    print(f"cleanup: removed {CLIENT_ID}")


def seed(sheet_id: str) -> None:
    owner_exec("ALTER TABLE clients ADD COLUMN IF NOT EXISTS wins_sheet_id VARCHAR(120)")
    cleanup()
    owner_exec(
        "INSERT INTO clients (client_id, display_name, is_active, plan_tier, wins_sheet_id) "
        "VALUES (:c, 'Demo Client Wins', TRUE, 'pilot', :sheet)",
        c=CLIENT_ID, sheet=sheet_id,
    )
    for days_ago, doors in AGREEMENTS:
        owner_exec(
            "INSERT INTO pms_agreements (client_id, opportunity_id, door_count, agreement_source, status, door_signed_at) "
            "VALUES (:c, gen_random_uuid(), :d, 'SYNTHETIC', 'ACTIVE', NOW() - (:days || ' days')::interval)",
            c=CLIENT_ID, d=doors, days=days_ago,
        )
    for days_ago in ATTENDED_DAYS:
        owner_exec(
            "INSERT INTO appointments (client_id, opportunity_id, state, scheduled_for, owner_brief_url, "
            "confirmed_24h_timestamp, confirmed_3h_timestamp) "
            "VALUES (:c, gen_random_uuid(), 'ATTENDED', NOW() - (:days || ' days')::interval, "
            "'https://example.test/brief', NOW(), NOW())",
            c=CLIENT_ID, days=days_ago,
        )
    for i, days_ago in enumerate(MEETING_DAYS):
        owner_exec(
            "INSERT INTO events (client_id, event_type, entity_type, entity_id, payload, created_at) "
            "VALUES (:c, 'meeting_booked', 'company', :eid, '{}'::jsonb, NOW() - (:days || ' days')::interval)",
            c=CLIENT_ID, eid=f"demo-wins-{i}", days=days_ago,
        )
    total_doors = sum(d for _, d in AGREEMENTS)
    print(
        f"seed: {CLIENT_ID} — {len(AGREEMENTS)} agreements ({total_doors} doors), "
        f"{len(ATTENDED_DAYS)} attended, {len(MEETING_DAYS)} meetings; wins_sheet_id={sheet_id}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    ap.add_argument("--cleanup", action="store_true")
    args = ap.parse_args()

    if args.cleanup:
        cleanup()
        return 0

    seed(args.sheet_id)
    # Export immediately so the Sheet reflects the seed without waiting for the cron.
    from src.services.client_wins import export_client_wins
    rows = export_client_wins(CLIENT_ID, args.sheet_id)
    print(f"export: {rows} summary rows written to the sheet (4 tabs: Summary/KPIs/Wins/Trend)")
    if rows == 0:
        print("NOTE: export wrote nothing — is GOOGLE_SHEETS_CREDENTIALS_PATH set and the sheet shared?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
