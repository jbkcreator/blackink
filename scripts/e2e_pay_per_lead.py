"""End-to-end verification for Task 4.2.3 pay-per-lead portal parsing.

Runs the real Mailgun webhook, inbound pipeline, database persistence, and
speed-to-lead sweep against the database selected by ``ENV_FILE``. It creates
only the E2E_PPL_CLIENT tenant and removes those rows on exit.

Safety: outgoing email is always stubbed and Slack is disabled before settings
load. No test payload is sent to Mailgun, SMTP, Slack, or a production tenant.

Run from the repository root:
    $env:ENV_FILE = ".env.test"
    $env:PYTHONPATH = "."
    python scripts/e2e_pay_per_lead.py
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

_cli = argparse.ArgumentParser(add_help=False)
_cli.add_argument("--live-test-integrations", action="store_true")
_cli.add_argument("--live-recipient")
RUN_OPTIONS, _ = _cli.parse_known_args()
LIVE_INTEGRATIONS = RUN_OPTIONS.live_test_integrations
LIVE_RECIPIENT = RUN_OPTIONS.live_recipient

if Path(os.environ.get("ENV_FILE", "")).name.lower() != ".env.test":
    raise SystemExit("Refusing to run: set ENV_FILE to the dedicated .env.test file")

# Must precede every application import: settings read environment at import.
# The default is entirely local/stubbed. Live mode is deliberately opt-in and
# requires an explicit controlled recipient supplied by the operator.
if LIVE_INTEGRATIONS:
    if not LIVE_RECIPIENT:
        raise SystemExit("--live-test-integrations requires --live-recipient")
else:
    os.environ["EMAIL_SENDER_MODE"] = "stub"
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-e2e-disabled"
# Group D / D-2: merged into MAILGUN_SIGNING_KEY, shared by both Mailgun
# inbound routers.
os.environ.setdefault("MAILGUN_SIGNING_KEY", "e2e-pay-per-lead-signing-key")

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_owner_db_context


RUN_ID = uuid4().hex[:10]
CLIENT_ID = f"E2E_PPL_{RUN_ID.upper()}"
SLUG = f"e2eppl{RUN_ID}"
DOMAIN = f"{SLUG}-outreach.example"
MAILBOX = f"sales@{DOMAIN}"
SIGNING_KEY = os.environ["MAILGUN_SIGNING_KEY"]

APM_BODY = """Owner Name: Ava APM
Email: ava.apm@example.test
Phone: 303-555-0101
Property Address: 10 APM Ave, Denver, CO
Message: Please manage my four-unit building.
"""
MMP_BODY = """Owner: Milo MMP
E-mail: milo.mmp@example.test
Best Contact Number: 303-555-0102
Property Location: 20 MMP St, Aurora, CO
Inquiry: I need full-service management.
"""
THUMBTACK_BODY = """Name: Tia Thumbtack
Email: tia.thumbtack@example.test
Phone: 303-555-0103
Location: Lakewood, CO
Job: Property management for a duplex
Budget: $200/month
"""
APM_HTML = """<html><body>
<p>Owner Name: Holly HTML</p><p>Email: holly.html@example.test</p>
<p>Phone: 303-555-0104</p><p>Property Address: 40 HTML Rd, Denver, CO</p>
<p>Message: HTML-only lead notification.</p></body></html>"""


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}: {detail}")


R = Results()


def owner_exec(sql: str, **params: object) -> None:
    with get_owner_db_context() as session:
        session.execute(text(sql), params)
        session.commit()


def owner_rows(sql: str, **params: object):
    with get_owner_db_context() as session:
        return session.execute(text(sql), params).fetchall()


def cleanup() -> None:
    """Remove every record owned by this harness, in foreign-key-safe order."""
    owner_exec("DELETE FROM inbound_messages WHERE client_id = :client_id", client_id=CLIENT_ID)
    owner_exec("DELETE FROM mailboxes WHERE client_id = :client_id", client_id=CLIENT_ID)
    owner_exec("DELETE FROM sending_domains WHERE client_id = :client_id", client_id=CLIENT_ID)
    owner_exec("DELETE FROM clients WHERE client_id = :client_id", client_id=CLIENT_ID)


def seed() -> None:
    owner_exec(
        "INSERT INTO clients (client_id, display_name, is_active, plan_tier, "
        "daily_send_ceiling, subdomain_slug, stl_reply_subject, stl_reply_html_template) "
        "VALUES (:client_id, 'E2E Pay Per Lead Client', TRUE, 'pilot', 30, :slug, "
        "'Thanks for your inquiry', '<p>Hi {{name}}, {{booking_link}}</p>')",
        client_id=CLIENT_ID,
        slug=SLUG,
    )
    owner_exec(
        "INSERT INTO sending_domains (domain, client_id, cluster_label, quarantine_state, is_reserve) "
        "VALUES (:domain, :client_id, 'e2e-pay-per-lead', 'active', FALSE)",
        domain=DOMAIN,
        client_id=CLIENT_ID,
    )
    owner_exec(
        "INSERT INTO mailboxes (domain_id, mailbox_address, client_id, warmup_status, quarantine_state) "
        "VALUES ((SELECT id FROM sending_domains WHERE domain = :domain), :mailbox, "
        ":client_id, 'warmed', 'active')",
        domain=DOMAIN,
        mailbox=MAILBOX,
        client_id=CLIENT_ID,
    )


def message_rows():
    return owner_rows(
        "SELECT id, status, source_channel, sender_name, sender_email, sender_phone, "
        "property_address, body_text, requires_human_review, idempotency_key "
        "FROM inbound_messages WHERE client_id = :client_id ORDER BY received_at",
        client_id=CLIENT_ID,
    )


def sign(timestamp: str, token: str) -> str:
    return hmac.new(
        SIGNING_KEY.encode(), f"{timestamp}{token}".encode(), hashlib.sha256
    ).hexdigest()


def deliver(app, *, message_id: str, sender: str, body_plain: str = "", body_html: str = ""):
    """Submit one independent Mailgun delivery through its own test client."""
    from fastapi.testclient import TestClient

    timestamp = f"171000{len(message_id)}"
    token = f"token-{message_id}"
    return TestClient(app).post(
        "/api/v1/webhooks/mailgun-inbound",
        data={
            "timestamp": timestamp,
            "token": token,
            "signature": sign(timestamp, token),
            "recipient": f"leads@{SLUG}.getblackink.com",
            "sender": sender,
            "subject": "New property-management lead",
            "body-plain": body_plain,
            "body-html": body_html,
            "Message-Id": f"<{message_id}@e2e.test>",
        },
    )


def row_for(message_id: str):
    suffix = f":{message_id}@e2e.test"
    matches = [row for row in message_rows() if row.idempotency_key.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None


def main() -> int:
    settings = get_settings()
    database = settings.database_url_app or settings.database_url
    print(f"DB: {database}")
    if LIVE_INTEGRATIONS:
        R.check("live recipient is explicitly supplied", bool(LIVE_RECIPIENT))
    else:
        R.check("stub sender is enforced", os.environ["EMAIL_SENDER_MODE"] == "stub")

    print("[setup] cleanup + seed")
    cleanup()
    seed()

    from src.api.main import app
    from src.services import inbound_lead_orchestrator
    from src.tasks import speed_to_lead_sweep

    # The append-only audit ledger cannot be cleaned by the test database roles
    # (events intentionally grants INSERT only), so keep those writes disabled.
    # Slack is always disabled here, including live SMTP mode. A real Slack
    # card requires a separate explicit channel-approved smoke test.
    inbound_lead_orchestrator._post_closer_alert = lambda *args, **kwargs: None
    inbound_lead_orchestrator.log_event = lambda *args, **kwargs: None
    speed_to_lead_sweep.log_event = lambda *args, **kwargs: None

    cases = (
        ("apm-plain", "APM Leads <leads@allpropertymanagement.com>", APM_BODY, "", "APM", "Ava APM", "ava.apm@example.test"),
        ("mmp-plain", "Leads <notify@managemyproperty.com>", MMP_BODY, "", "MANAGE_MY_PROPERTY", "Milo MMP", "milo.mmp@example.test"),
        ("thumbtack-plain", "Thumbtack <no-reply@thumbtack.com>", THUMBTACK_BODY, "", "THUMBTACK", "Tia Thumbtack", "tia.thumbtack@example.test"),
        ("apm-html", "APM Leads <leads@allpropertymanagement.com>", "", APM_HTML, "APM", "Holly HTML", "holly.html@example.test"),
    )

    print("[portal ingress] recognized payloads")
    with ThreadPoolExecutor(max_workers=len(cases)) as executor:
        futures = {
            message_id: executor.submit(
                deliver, app, message_id=message_id, sender=sender,
                body_plain=plain, body_html=html,
            )
            for message_id, sender, plain, html, _source, _name, _email in cases
        }
        responses = {message_id: future.result() for message_id, future in futures.items()}
    for message_id, _sender, _plain, _html, _source, _name, _email in cases:
        response = responses[message_id]
        R.check(f"{message_id} webhook accepts signed mail", response.status_code == 200, str(response.status_code))
    rows_by_key = {row.idempotency_key: row for row in message_rows()}
    for message_id, _sender, _plain, _html, source, name, email in cases:
        row = next((row for key, row in rows_by_key.items() if key.endswith(f":{message_id}@e2e.test")), None)
        R.check(f"{message_id} writes one lead", row is not None)
        if row:
            R.check(f"{message_id} tags portal", row.source_channel == source, str(row.source_channel))
            R.check(f"{message_id} stores owner name", row.sender_name == name, str(row.sender_name))
            R.check(f"{message_id} stores owner email", row.sender_email == email, str(row.sender_email))
            R.check(f"{message_id} is auto-dispatchable", row.requires_human_review is False, str(row.requires_human_review))

    print("[portal ingress] unrecognized notification fails closed")
    response = deliver(
        app,
        message_id="unknown-review",
        sender="Unknown Portal <notifications@unknown-portal.example>",
        body_plain="A lead might exist, but this notification has no parseable owner details.",
    )
    R.check("unrecognized notification is retained", response.status_code == 200, str(response.status_code))
    rows_by_key = {row.idempotency_key: row for row in message_rows()}
    review_row = next((row for key, row in rows_by_key.items() if key.endswith(":unknown-review@e2e.test")), None)
    R.check("unrecognized notification is review-required", bool(review_row and review_row.requires_human_review))
    R.check(
        "unrecognized notification has no portal sender as owner email",
        bool(review_row and not review_row.sender_email),
        str(review_row.sender_email if review_row else None),
    )

    print("[sweep] only parsed owners receive the response")
    if LIVE_INTEGRATIONS:
        # The portal-parser assertions above prove the original owner emails.
        # Replace only the response destination just before live SMTP dispatch
        # so no fixture address can receive a real message.
        owner_exec(
            "UPDATE inbound_messages SET sender_email = :recipient "
            "WHERE client_id = :client_id AND requires_human_review = FALSE",
            recipient=LIVE_RECIPIENT,
            client_id=CLIENT_ID,
        )
    owner_exec(
        "UPDATE inbound_messages SET send_at = NOW() - INTERVAL '1 minute' "
        "WHERE client_id = :client_id AND status = 'RECEIVED'",
        client_id=CLIENT_ID,
    )
    dispatched = speed_to_lead_sweep.run_sweep(limit=20)
    R.check("sweep dispatches all four parsed leads", dispatched == 4, str(dispatched))
    dispatched_rows = {row.idempotency_key: row.status for row in message_rows()}
    for message_id, *_ in cases:
        key = next((key for key in dispatched_rows if key.endswith(f":{message_id}@e2e.test")), None)
        R.check(f"{message_id} becomes RESPONDED", key is not None and dispatched_rows[key] == "RESPONDED", str(dispatched_rows.get(key)))
    review_after_sweep = row_for("unknown-review")
    R.check("review-required notification remains RECEIVED", bool(review_after_sweep and review_after_sweep.status == "RECEIVED"), str(review_after_sweep.status if review_after_sweep else None))

    print(f"\nResults: {R.passed} passed, {R.failed} failed")
    return 1 if R.failed else 0


if __name__ == "__main__":
    try:
        exit_code = main()
    finally:
        print("[teardown] cleanup")
        cleanup()
    raise SystemExit(exit_code)
