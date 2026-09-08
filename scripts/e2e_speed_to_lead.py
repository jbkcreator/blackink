"""End-to-end integration test for Task 4.2.1 Speed-to-Lead ingest.

Drives BOTH ingress paths against a live local Postgres, then the SLA sweep,
asserting the full chain: HTTP -> orchestrator -> inbound_messages + events →
auto-response dispatch. Uses FastAPI's TestClient, which runs the real routers
(auth, HMAC, RLS-safe client resolution, dedupe) and executes the webhook's
BackgroundTask synchronously — so there is no server to start and no async race.

PREREQUISITES
  1. Disposable local Postgres up:   docker compose up -d postgres-test
  2. Point this shell at it:          export ENV_FILE=.env.local
     (PowerShell:                     $env:ENV_FILE=".env.local")
  3. Full migration sequence applied (see CLAUDE.md), including the two new
     4.2.1 migrations:
        migrations/apply_clients_stl_fields.py           (before RLS)
        migrations/apply_inbound_messages_lead_fields.py (before RLS)

RUN
    PYTHONPATH=. python scripts/e2e_speed_to_lead.py

The script forces EMAIL_SENDER_MODE=stub and a fixed MAILGUN_WEBHOOK_SIGNING_KEY
via os.environ BEFORE settings load, so no real mail is sent and Path B signing
is deterministic regardless of your .env. It seeds its own throwaway client
(E2E_STL_CLIENT), cleans up prior runs, and cleans up on exit.
"""

from __future__ import annotations

# ── Force test-only settings BEFORE any src import (env wins over .env file) ──
import os

os.environ.setdefault("EMAIL_SENDER_MODE", "stub")
os.environ.setdefault("MAILGUN_WEBHOOK_SIGNING_KEY", "e2e-test-signing-key")

# Slack policy: only allow the closer-alert card to REALLY post when running
# against the dedicated TEST workspace (ENV_FILE=.env.test). Then the card
# lands in the test setter channel and we assert closer_alert_posted. Under
# any other env (e.g. prod .env), force a dummy token via os.environ — which
# overrides the .env file — so a real prod channel can never be hit.
USE_TEST_SLACK = os.environ.get("ENV_FILE", ".env").endswith(".env.test")
if not USE_TEST_SLACK:
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-e2e-disabled"

import hashlib
import hmac
import json
import sys

sys.path.insert(0, ".")
# Windows consoles default to cp1252 and choke on non-ASCII in prints.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_owner_db_context, get_system_db_context

# ── Test fixtures ────────────────────────────────────────────────────────────
CLIENT_ID = "E2E_STL_CLIENT"
SECRET = "e2e-secret-value"
SECRET_HASH = hashlib.sha256(SECRET.encode()).hexdigest()
SLUG = "e2eacme"
DOMAIN = "e2eacme-outreach.com"
MAILBOX = "sales1@e2eacme-outreach.com"
SIGNING_KEY = os.environ["MAILGUN_WEBHOOK_SIGNING_KEY"]


# ── Tiny assertion harness ───────────────────────────────────────────────────
class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}  {detail}")


R = Results()


def _owner_exec(sql: str, **params) -> None:
    with get_owner_db_context() as s:
        s.execute(text(sql), params)
        s.commit()


def _owner_query(sql: str, **params):
    with get_owner_db_context() as s:
        return s.execute(text(sql), params).fetchall()


# ── Seed / teardown ──────────────────────────────────────────────────────────
def cleanup() -> None:
    """Delete every row this test owns, in FK-safe order. Idempotent."""
    _owner_exec("DELETE FROM inbound_messages WHERE client_id = :c", c=CLIENT_ID)
    _owner_exec("DELETE FROM events WHERE client_id = :c", c=CLIENT_ID)
    _owner_exec("DELETE FROM mailboxes WHERE client_id = :c", c=CLIENT_ID)
    _owner_exec("DELETE FROM sending_domains WHERE client_id = :c", c=CLIENT_ID)
    _owner_exec("DELETE FROM clients WHERE client_id = :c", c=CLIENT_ID)


def seed() -> None:
    _owner_exec(
        "INSERT INTO clients (client_id, display_name, is_active, plan_tier, "
        " daily_send_ceiling, inbound_webhook_secret_hash, subdomain_slug, "
        " stl_reply_subject, stl_reply_html_template) "
        "VALUES (:c, 'E2E STL Client', TRUE, 'pilot', 30, :h, :slug, "
        " 'Thanks for reaching out', "
        " '<p>Hi {{name}}, thanks for your inquiry. {{booking_link}}</p>')",
        c=CLIENT_ID, h=SECRET_HASH, slug=SLUG,
    )
    _owner_exec(
        "INSERT INTO sending_domains (domain, client_id, cluster_label, "
        " quarantine_state, is_reserve) "
        "VALUES (:d, :c, 'e2e-cluster', 'active', FALSE)",
        d=DOMAIN, c=CLIENT_ID,
    )
    _owner_exec(
        "INSERT INTO mailboxes (domain_id, mailbox_address, client_id, "
        " warmup_status, quarantine_state) "
        "VALUES ((SELECT id FROM sending_domains WHERE domain = :d), "
        " :m, :c, 'warmed', 'active')",
        d=DOMAIN, m=MAILBOX, c=CLIENT_ID,
    )


# ── Query helpers scoped to our client ───────────────────────────────────────
def messages():
    return _owner_query(
        "SELECT id, status, channel, source_channel, sender_email, "
        " sender_name, sender_phone, send_at, lead_sla_due_at, idempotency_key, "
        " destination_address, ack_latency_seconds, mailbox_id, responded_at "
        "FROM inbound_messages WHERE client_id = :c ORDER BY received_at",
        c=CLIENT_ID,
    )


def event_types():
    rows = _owner_query(
        "SELECT event_type FROM events WHERE client_id = :c ORDER BY id", c=CLIENT_ID
    )
    return [r.event_type for r in rows]


# ── Path B signing ───────────────────────────────────────────────────────────
def mailgun_sig(timestamp: str, token: str) -> str:
    return hmac.new(
        SIGNING_KEY.encode(), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()


# ── The test flow ────────────────────────────────────────────────────────────
def main() -> int:
    settings = get_settings()
    print(f"DB: {settings.database_url_app or settings.database_url}")
    print(f"EMAIL_SENDER_MODE={os.environ['EMAIL_SENDER_MODE']}  "
          f"MAILGUN key set={'yes' if settings.mailgun_webhook_signing_key else 'no'}\n")

    print("[setup] cleanup + seed")
    cleanup()
    seed()

    # TestClient WITHOUT `with` — do not trigger lifespan (no Slack socket / no
    # background-worker threads). BackgroundTasks still run synchronously.
    from fastapi.testclient import TestClient
    from src.api.main import app
    client = TestClient(app)

    # ── Path A: happy path ───────────────────────────────────────────────────
    print("\n[Path A] webhook happy path")
    resp = client.post("/api/v1/webhooks/inbound-lead", json={
        "client_secret": SECRET,
        "prospect_name": "Jane Renter",
        "email": "jane@renterco.com",
        "inquiry_text": "I want to view 12 Oak St",
        "property_address": "12 Oak St",
        "source": "WEBSITE_FORM",
        "external_id": "e2e-lead-1",
    })
    R.check("Path A returns 202", resp.status_code == 202, f"got {resp.status_code}")
    msgs = messages()
    R.check("one inbound_messages row written", len(msgs) == 1, f"got {len(msgs)}")
    if msgs:
        m = msgs[0]
        R.check("status RECEIVED", m.status == "RECEIVED", m.status)
        R.check("channel WEBHOOK", m.channel == "WEBHOOK", m.channel)
        R.check("sender_email stored inline", m.sender_email == "jane@renterco.com", str(m.sender_email))
        R.check("destination_address set", bool(m.destination_address), str(m.destination_address))
        R.check("send_at computed", m.send_at is not None)
        R.check("lead_sla_due_at computed", m.lead_sla_due_at is not None)
    R.check("inbound_lead_received event logged", "inbound_lead_received" in event_types())
    if USE_TEST_SLACK:
        # Real card was posted to the TEST setter channel — verify the event.
        R.check("closer_alert_posted event logged (test Slack)",
                "closer_alert_posted" in event_types())
    else:
        print("  SKIP  closer_alert_posted (Slack disabled — run ENV_FILE=.env.test to verify)")

    # ── Path A: validation 422 (no email/phone) ──────────────────────────────
    print("\n[Path A] validation + auth")
    resp = client.post("/api/v1/webhooks/inbound-lead", json={
        "client_secret": SECRET, "prospect_name": "No Contact",
        "inquiry_text": "hi", "source": "WEBSITE_FORM",
    })
    R.check("422 when no email/phone", resp.status_code == 422, f"got {resp.status_code}")

    # ── Path A: bad secret 401 ───────────────────────────────────────────────
    resp = client.post("/api/v1/webhooks/inbound-lead", json={
        "client_secret": "wrong-secret", "email": "x@y.com", "source": "WEBSITE_FORM",
    })
    R.check("401 on bad client_secret", resp.status_code == 401, f"got {resp.status_code}")

    # ── Path A: dedupe on explicit external_id ────────────────────────────────
    print("\n[Path A] idempotency")
    before = len(messages())
    client.post("/api/v1/webhooks/inbound-lead", json={
        "client_secret": SECRET, "email": "jane@renterco.com",
        "inquiry_text": "dup", "source": "WEBSITE_FORM", "external_id": "e2e-lead-1",
    })
    R.check("duplicate external_id -> no new row", len(messages()) == before, f"{before}→{len(messages())}")

    # ── Path A: deterministic fallback dedupe (no external_id) ────────────────
    payload_no_id = {
        "client_secret": SECRET, "email": "bob@renterco.com",
        "inquiry_text": "same body twice", "source": "WEBSITE_FORM",
    }
    r1 = client.post("/api/v1/webhooks/inbound-lead", json=payload_no_id)
    n_after_first = len(messages())
    r2 = client.post("/api/v1/webhooks/inbound-lead", json=payload_no_id)
    n_after_second = len(messages())
    same_key = r1.json().get("dedupe_key") == r2.json().get("dedupe_key")
    R.check("no-external_id retry reuses deterministic key", same_key,
            f"{r1.json().get('dedupe_key')} vs {r2.json().get('dedupe_key')}")
    R.check("no-external_id retry -> no duplicate row", n_after_first == n_after_second,
            f"{n_after_first}→{n_after_second}")

    # ── Path B: bad signature 406 ─────────────────────────────────────────────
    print("\n[Path B] Mailgun email parse")
    resp = client.post("/api/v1/webhooks/mailgun-inbound", data={
        "timestamp": "1700000000", "token": "tok", "signature": "deadbeef",
        "recipient": f"leads@{SLUG}.getblackink.com", "sender": "eve@renterco.com",
        "subject": "inquiry", "body-plain": "hello", "Message-Id": "<m1@mg>",
    })
    R.check("406 on bad HMAC signature", resp.status_code == 406, f"got {resp.status_code}")

    # ── Path B: unknown subdomain 406 ─────────────────────────────────────────
    ts, tok = "1700000001", "tok2"
    resp = client.post("/api/v1/webhooks/mailgun-inbound", data={
        "timestamp": ts, "token": tok, "signature": mailgun_sig(ts, tok),
        "recipient": "leads@nosuchclient.getblackink.com", "sender": "eve@renterco.com",
        "subject": "x", "body-plain": "x", "Message-Id": "<m2@mg>",
    })
    R.check("406 on unknown subdomain slug", resp.status_code == 406, f"got {resp.status_code}")

    # ── Path B: valid delivery writes a row ───────────────────────────────────
    ts, tok = "1700000002", "tok3"
    before = len(messages())
    resp = client.post("/api/v1/webhooks/mailgun-inbound", data={
        "timestamp": ts, "token": tok, "signature": mailgun_sig(ts, tok),
        "recipient": f"leads@{SLUG}.getblackink.com",
        "sender": "Eve Owner <eve@renterco.com>",
        "subject": "Interested in listing", "body-plain": "Please call me",
        "Message-Id": "<mg-e2e-1@mg>",
    })
    R.check("valid Mailgun delivery returns 200", resp.status_code == 200, f"got {resp.status_code}")
    after = messages()
    email_rows = [m for m in after if m.channel == "EMAIL"]
    R.check("Path B wrote an EMAIL-channel row", len(email_rows) == 1, f"got {len(email_rows)}")
    if email_rows:
        R.check("Path B parsed sender email", email_rows[0].sender_email == "eve@renterco.com",
                str(email_rows[0].sender_email))

    # ── SLA sweep: dispatch the auto-response ─────────────────────────────────
    print("\n[SLA sweep] force due + dispatch")
    # Force every RECEIVED row due now (bypasses off-hours deferral for the test).
    _owner_exec(
        "UPDATE inbound_messages SET send_at = NOW() - INTERVAL '1 minute' "
        "WHERE client_id = :c AND status = 'RECEIVED'", c=CLIENT_ID,
    )
    received_before = [m for m in messages() if m.status == "RECEIVED"]
    from src.tasks.speed_to_lead_sweep import run_sweep
    dispatched = run_sweep(limit=50)
    R.check("sweep dispatched the due rows", dispatched == len(received_before),
            f"dispatched {dispatched}, expected {len(received_before)}")
    responded = [m for m in messages() if m.status == "RESPONDED"]
    R.check("rows flipped to RESPONDED", len(responded) == len(received_before),
            f"{len(responded)} RESPONDED")
    R.check("ack_latency_seconds recorded", all(m.ack_latency_seconds is not None for m in responded))
    # #4: sends that actually emailed must record the mailbox for capacity counting.
    emailed = [m for m in responded if m.sender_email]
    R.check("responded sends record mailbox_id + responded_at (capacity accounting)",
            all(m.mailbox_id is not None and m.responded_at is not None for m in emailed),
            f"emailed={len(emailed)}")
    R.check("speed_to_lead_response_sent event logged", "speed_to_lead_response_sent" in event_types())

    # ── Report ────────────────────────────────────────────────────────────────
    print(f"\n{'='*50}\nRESULT: {R.passed} passed, {R.failed} failed\n{'='*50}")
    return 0 if R.failed == 0 else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        print("\n[teardown] cleanup")
        try:
            cleanup()
        except Exception as exc:  # noqa: BLE001
            print(f"  cleanup warning: {exc}")
    sys.exit(code)
