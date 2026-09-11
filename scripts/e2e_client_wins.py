"""End-to-end verification for the per-client Client Wins Dashboard (S-21).

Seeds a throwaway tenant with real wins rows (ATTENDED appointments, a signed
pms_agreement with door count, meeting_booked events), points its wins_sheet_id
at the real pilot Google Sheet, runs the export sweep, reads the Sheet back to
confirm the metrics, and exercises the admin JSON + CSV endpoints. Creates only
the E2E_WINS_<runid> tenant and removes its rows on exit.

Safety: outgoing email is stubbed and Slack disabled before settings load. The
ONE real external write is the Google Sheet update — that is the point of this
harness. No production tenant is touched (throwaway client_id only).

Run from the repository root:
    $env:ENV_FILE = ".env.test"
    $env:PYTHONPATH = "."
    python scripts/e2e_client_wins.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

if Path(os.environ.get("ENV_FILE", "")).name.lower() != ".env.test":
    raise SystemExit("Refusing to run: set ENV_FILE to the dedicated .env.test file")

os.environ["EMAIL_SENDER_MODE"] = "stub"
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-e2e-disabled")

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

import gspread
from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_owner_db_context

RUN_ID = uuid4().hex[:10]
CLIENT_ID = f"E2E_WINS_{RUN_ID.upper()}"
SHEET_ID = os.environ.get("E2E_WINS_SHEET_ID", "1_HHeaCyXDxASDcHqv2tsPq_sDk4YX3CBqlgMx5hrruY")

EXPECT_ATTENDED = 2
EXPECT_MEETINGS = 3
EXPECT_AGREEMENTS = 1
EXPECT_DOORS = 47


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}: {detail}")


R = Results()


def owner_exec(sql: str, **params: object) -> None:
    with get_owner_db_context() as s:
        s.execute(text(sql), params)
        s.commit()


def cleanup() -> None:
    # Owner role can DELETE from the append-only / delete-revoked runtime tables.
    owner_exec("DELETE FROM events WHERE client_id = :c", c=CLIENT_ID)
    owner_exec("DELETE FROM appointments WHERE client_id = :c", c=CLIENT_ID)
    owner_exec("DELETE FROM pms_agreements WHERE client_id = :c", c=CLIENT_ID)
    owner_exec("DELETE FROM clients WHERE client_id = :c", c=CLIENT_ID)


def seed() -> None:
    owner_exec("ALTER TABLE clients ADD COLUMN IF NOT EXISTS wins_sheet_id VARCHAR(120)")
    owner_exec(
        "INSERT INTO clients (client_id, display_name, is_active, plan_tier, wins_sheet_id) "
        "VALUES (:c, 'E2E Client Wins', TRUE, 'pilot', :sheet)",
        c=CLIENT_ID, sheet=SHEET_ID,
    )
    # 2 ATTENDED appointments (company_id/contact_id NULL dodges the ownership
    # trigger; confirmations set so is_billable is realistic though not required
    # for the count).
    for _ in range(EXPECT_ATTENDED):
        owner_exec(
            "INSERT INTO appointments (client_id, opportunity_id, state, scheduled_for, "
            "owner_brief_url, confirmed_24h_timestamp, confirmed_3h_timestamp) "
            "VALUES (:c, gen_random_uuid(), 'ATTENDED', NOW(), 'https://example.test/brief', NOW(), NOW())",
            c=CLIENT_ID,
        )
    # 1 signed management agreement carrying the door count.
    owner_exec(
        "INSERT INTO pms_agreements (client_id, opportunity_id, door_count, agreement_source, "
        "status, door_signed_at) "
        "VALUES (:c, gen_random_uuid(), :doors, 'SYNTHETIC', 'ACTIVE', NOW())",
        c=CLIENT_ID, doors=EXPECT_DOORS,
    )
    # 3 meeting_booked events.
    for i in range(EXPECT_MEETINGS):
        owner_exec(
            "INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
            "VALUES (:c, 'meeting_booked', 'company', :eid, '{}'::jsonb)",
            c=CLIENT_ID, eid=f"e2e-wins-{i}",
        )


def sheet_values():
    gc = gspread.service_account(filename=get_settings().google_sheets_credentials_path)
    return {r[0]: r[1] for r in gc.open_by_key(SHEET_ID).sheet1.get_all_values() if len(r) >= 2}


def main() -> int:
    settings = get_settings()
    print(f"DB: {settings.database_url_app or settings.database_url}")
    R.check("sheets credentials configured", bool(settings.google_sheets_credentials_path),
            "set GOOGLE_SHEETS_CREDENTIALS_PATH")
    if not settings.google_sheets_credentials_path:
        return 1

    print("[setup] cleanup + seed")
    cleanup()
    seed()

    print("[compute] wins reflect seeded rows")
    from src.services.client_wins import compute_wins, wins_csv
    d = dict(compute_wins(CLIENT_ID))
    R.check("attended appointments", d["Attended Discovery Appointments"] == str(EXPECT_ATTENDED), d.get("Attended Discovery Appointments"))
    R.check("meetings booked", d["Meetings Booked"] == str(EXPECT_MEETINGS), d.get("Meetings Booked"))
    R.check("signed agreements", d["Signed Management Agreements"] == str(EXPECT_AGREEMENTS), d.get("Signed Management Agreements"))
    R.check("total doors signed", d["Total Doors Signed"] == str(EXPECT_DOORS), d.get("Total Doors Signed"))
    R.check("unbuilt metrics say NOT RECORDED", d["Ancillary Revenue"].startswith("NOT RECORDED"), d.get("Ancillary Revenue"))

    print("[sweep] export writes the client's Google Sheet")
    from src.tasks.client_wins_sweep import run_sweep
    exported = run_sweep()
    R.check("sweep exported at least this client", exported >= 1, str(exported))
    sv = sheet_values()
    R.check("sheet shows attended count", sv.get("Attended Discovery Appointments") == str(EXPECT_ATTENDED), sv.get("Attended Discovery Appointments"))
    R.check("sheet shows doors signed", sv.get("Total Doors Signed") == str(EXPECT_DOORS), sv.get("Total Doors Signed"))

    print("[api] admin JSON + CSV export")
    secret = settings.admin_jwt_secret
    if not secret:
        R.check("admin_jwt_secret set (endpoint checks)", False, "ADMIN_JWT_SECRET unset — skipping endpoint asserts")
    else:
        import jwt
        from fastapi.testclient import TestClient
        from src.api.main import app
        tok = jwt.encode({"sub": "e2e", "scope": "admin"}, secret.get_secret_value(), algorithm="HS256")
        client = TestClient(app)
        h = {"Authorization": f"Bearer {tok}"}
        rj = client.get(f"/api/metrics/wins/{CLIENT_ID}", headers=h)
        R.check("JSON endpoint 200", rj.status_code == 200, str(rj.status_code))
        R.check("JSON carries wins", any(w["metric"] == "Total Doors Signed" and w["value"] == str(EXPECT_DOORS) for w in rj.json().get("wins", [])), rj.text[:200])
        rc = client.get(f"/api/metrics/wins/{CLIENT_ID}/export.csv", headers=h)
        R.check("CSV endpoint 200", rc.status_code == 200, str(rc.status_code))
        R.check("CSV is attachment", "attachment" in rc.headers.get("content-disposition", ""), rc.headers.get("content-disposition", ""))
        R.check("CSV body has header", rc.text.splitlines()[0].startswith("Metric,Value"), rc.text.splitlines()[0] if rc.text else "")
        r404 = client.get("/api/metrics/wins/NO_SUCH_CLIENT", headers=h)
        R.check("unknown client -> 404", r404.status_code == 404, str(r404.status_code))
        R.check("no auth -> 401", client.get(f"/api/metrics/wins/{CLIENT_ID}").status_code == 401)

    print(f"\nResults: {R.passed} passed, {R.failed} failed")
    return 1 if R.failed else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        print("[teardown] cleanup")
        cleanup()
    raise SystemExit(code)
