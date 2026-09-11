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

The most recently signed agreement also gets one CHARGED settlement_transactions
row (with a placeholder evidence_packet_url), so the Wins tab's Evidence Packet
column has something to show — settlement_transactions is gated by the billing
offer-config + guard trigger, so this seeds a matching settlement_offer_config
row too. --cleanup removes both.
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
DEMO_OFFER_CODE = "client_wins_demo_bounty"
DEMO_PER_DOOR_BOUNTY_CENTS = 50000
DEMO_EVIDENCE_PACKET_URL = "https://example.test/evidence-packets/demo-client-wins.pdf"

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
    owner_exec("DELETE FROM settlement_transactions WHERE client_id = :c", c=CLIENT_ID)
    owner_exec("DELETE FROM settlement_offer_config WHERE offer_code = :o", o=DEMO_OFFER_CODE)
    owner_exec("DELETE FROM pms_agreements WHERE client_id = :c", c=CLIENT_ID)
    owner_exec("DELETE FROM clients WHERE client_id = :c", c=CLIENT_ID)
    print(f"cleanup: removed {CLIENT_ID}")


def _seed_charged_settlement() -> None:
    """Charge installment 1 on the most recently signed agreement, with a
    placeholder evidence packet, so the Wins tab's Evidence Packet column
    has a real link to show."""
    with get_owner_db_context() as s:
        s.execute(
            text(
                "INSERT INTO settlement_offer_config "
                "(offer_code, settlement_enabled, pricing_basis, per_door_bounty_cents, installment_1_bps) "
                "VALUES (:offer, TRUE, 'PER_DOOR', :per_door, 5000) "
                "ON CONFLICT (offer_code) DO NOTHING"
            ),
            {"offer": DEMO_OFFER_CODE, "per_door": DEMO_PER_DOOR_BOUNTY_CENTS},
        )
        agreement = s.execute(
            text(
                "SELECT pms_agreement_id, opportunity_id, door_count, door_signed_at "
                "FROM pms_agreements WHERE client_id = :c ORDER BY door_signed_at DESC LIMIT 1"
            ),
            {"c": CLIENT_ID},
        ).mappings().one()
        total_cents = agreement["door_count"] * DEMO_PER_DOOR_BOUNTY_CENTS
        installment_1_cents = total_cents // 2
        installment_2_cents = total_cents - installment_1_cents
        s.execute(
            text(
                "INSERT INTO settlement_transactions "
                "(client_id, pms_agreement_id, opportunity_id, offer_code, door_count, "
                " total_bounty_cents, installment_1_cents, installment_2_cents, "
                " installment_1_status, installment_1_charged_at, "
                " installment_2_status, installment_2_scheduled_for, "
                " door_signed_at, allow_synthetic_charge, "
                " evidence_packet_status, evidence_packet_url) "
                "VALUES (:c, :agreement_id, :opportunity_id, :offer, :doors, "
                " :total, :inst1, :inst2, "
                " 'CHARGED', :door_signed_at + INTERVAL '1 day', "
                " 'SCHEDULED', :door_signed_at + INTERVAL '60 days', "
                " :door_signed_at, TRUE, "
                " 'PUBLISHED', :evidence_url)"
            ),
            {
                "c": CLIENT_ID,
                "agreement_id": agreement["pms_agreement_id"],
                "opportunity_id": agreement["opportunity_id"],
                "offer": DEMO_OFFER_CODE,
                "doors": agreement["door_count"],
                "total": total_cents,
                "inst1": installment_1_cents,
                "inst2": installment_2_cents,
                "door_signed_at": agreement["door_signed_at"],
                "evidence_url": DEMO_EVIDENCE_PACKET_URL,
            },
        )
        s.commit()


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
    _seed_charged_settlement()
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
