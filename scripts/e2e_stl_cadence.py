"""Staging E2E harness for Task 4.2.2 — the six-attempt STL cadence.

This is deliberately a production-readiness test, not a seed script.  It
drives the real inbound webhook, Speed-to-Lead sweep, cadence arm sweep,
work-order approval state, and dispatcher against an isolated client.  It is
safe with ``EMAIL_SENDER_MODE=stub`` by default; pass ``--real`` only with
the dedicated SMTP recipient configured in .env.test.

Prerequisites:
  * a disposable database with all migrations, including apply_stl_cadence.py
  * ENV_FILE=.env.test (or the equivalent test environment variables)
  * for --real: SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD and the test recipient

Usage:
  $env:ENV_FILE = '.env.test'
  $env:PYTHONPATH = '.'
  python scripts/e2e_stl_cadence.py run
  python scripts/e2e_stl_cadence.py run --real

``run`` intentionally fails while any exercised acceptance criterion is
unmet.  Coverage:
  * acknowledgement, arm, five scheduled touches, event audit trail;
  * approved dispatch driven through the deployed all-tenant execution worker
    (work_order_execution_sweep), not cmd_sweep directly;
  * at-most-once claim (a second claim for the same touch is refused);
  * full five-touch run to the COMPLETED terminal state;
  * stop signals through the REAL ingest paths — inbound reply (REPLY) and
    booking (BOOKED) — including eager cancellation of still-queued touches;
  * one-click unsubscribe stop (OPT_OUT) with queued-touch cancellation;
  * post-SMTP write failure → terminal SENT_UNCONFIRMED, no resend;
  * a mailbox at its rolling-24h cap defers the touch (STL counts against caps).

Stop scenarios drive the real service functions (ingest_inbound_reply,
booking_ingest._process_event) so the wired hooks actually fire — this harness
never manufactures a STOPPED write directly.
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, ".")

_REAL = "--real" in sys.argv[1:]
if _REAL:
    from dotenv import load_dotenv

    load_dotenv(".env.test", override=True)

# Stub delivery is the safe default even if a developer's shell contains SMTP
# credentials.  ``--real`` is the explicit opt-in for the dedicated test inbox.
if not _REAL:
    os.environ["EMAIL_SENDER_MODE"] = "stub"

# This token signer is only used to exercise the local/public unsubscribe
# round-trip for the isolated E2E tenant.  Production must provide its own
# secret, but an absent test-only signer must not prevent cadence testing.
os.environ.setdefault("EMAIL_UNSUBSCRIBE_SECRET", "e2e-stl-cadence-unsubscribe-secret")

from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context


CLIENT_ID = "E2E_STL_CADENCE_CLIENT"
CLIENT_SECRET = "e2e-stl-cadence-secret"
SECRET_HASH = hashlib.sha256(CLIENT_SECRET.encode()).hexdigest()
SENDING_DOMAIN = "e2e-stl-cadence.test"
MAILBOX = "sender@e2e-stl-cadence.test"
PROSPECT_EMAIL = os.environ.get("E2E_STL_TEST_RECIPIENT", "lead@e2e-stl-cadence.test")


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def require(self, name: str, actual: bool, detail: str = "") -> None:
        marker = "PASS" if actual else "FAIL"
        print(f"{marker:4} {name}" + (f" — {detail}" if detail else ""))
        if not actual:
            self.failed += 1


def _exec(sql: str, **params) -> None:
    with get_owner_db_context() as session:
        session.execute(text(sql), params)


def _one(sql: str, **params):
    with get_owner_db_context() as session:
        return session.execute(text(sql), params).mappings().first()


def cleanup() -> None:
    """Remove only rows owned by this harness, in FK-safe order."""
    with get_owner_db_context() as session:
        session.execute(text("DELETE FROM stl_cadence_dispatches WHERE client_id = :c"), {"c": CLIENT_ID})
        session.execute(text("DELETE FROM bookings WHERE client_id = :c"), {"c": CLIENT_ID})
        session.execute(text("DELETE FROM owner_contacts WHERE client_id = :c"), {"c": CLIENT_ID})
        session.execute(text("DELETE FROM calendar_connections WHERE client_id = :c"), {"c": CLIENT_ID})
        session.execute(text("DELETE FROM agent_work_orders WHERE client_id = :c"), {"c": CLIENT_ID})
        session.execute(text("DELETE FROM events WHERE client_id = :c"), {"c": CLIENT_ID})
        session.execute(text("DELETE FROM inbound_messages WHERE client_id = :c"), {"c": CLIENT_ID})
        session.execute(text("DELETE FROM mailboxes WHERE client_id = :c"), {"c": CLIENT_ID})
        session.execute(text("DELETE FROM sending_domains WHERE client_id = :c"), {"c": CLIENT_ID})
        session.execute(text("DELETE FROM clients WHERE client_id = :c"), {"c": CLIENT_ID})


def _require_schema() -> None:
    """Fail before mutating fixtures when the test DB has not been migrated."""
    with get_owner_db_context() as session:
        present = session.execute(text("SELECT to_regclass('public.stl_cadence_dispatches')")).scalar()
    if present is None:
        raise RuntimeError(
            "Test database is missing the STL cadence migration. Run: "
            "python -c \"from dotenv import load_dotenv; load_dotenv('.env.test', override=True); "
            "from migrations.apply_stl_cadence import run; run()\""
        )


def seed() -> None:
    with get_owner_db_context() as session:
        session.execute(
            text(
                "INSERT INTO clients (client_id, display_name, is_active, inbound_webhook_secret_hash) "
                "VALUES (:c, 'E2E STL cadence', TRUE, :secret)"
            ),
            {"c": CLIENT_ID, "secret": SECRET_HASH},
        )
        domain_id = session.execute(
            text(
                "INSERT INTO sending_domains (domain, client_id, cluster_label, quarantine_state, is_reserve) "
                "VALUES (:domain, :c, 'e2e', 'active', FALSE) RETURNING id"
            ),
            {"domain": SENDING_DOMAIN, "c": CLIENT_ID},
        ).scalar_one()
        session.execute(
            text(
                "INSERT INTO mailboxes (domain_id, mailbox_address, client_id, warmup_status, quarantine_state) "
                "VALUES (:domain_id, :mailbox, :c, 'warmed', 'active')"
            ),
            {"domain_id": domain_id, "mailbox": MAILBOX, "c": CLIENT_ID},
        )


def _ingest(external_id: str = "e2e-stl-cadence-1", email: str = PROSPECT_EMAIL) -> int:
    from src.api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/v1/webhooks/inbound-lead",
        json={
            "client_secret": CLIENT_SECRET,
            "prospect_name": "Cadence Test Lead",
            "email": email,
            "inquiry_text": "Please contact me about property management.",
            "property_address": "12 E2E Way",
            "source": "WEBSITE_FORM",
            "external_id": external_id,
        },
    )
    if response.status_code != 202:
        raise RuntimeError(f"inbound webhook returned {response.status_code}: {response.text}")
    row = _one(
        "SELECT id FROM inbound_messages WHERE client_id = :c AND idempotency_key = :key",
        c=CLIENT_ID,
        key=f"{CLIENT_ID}:{external_id}",
    )
    if row is None:
        raise RuntimeError("inbound webhook returned 202 without a durable message row")
    return int(row["id"])


def _fresh_armed_lead(external_id: str, email: str) -> int:
    """Ingest → acknowledge → arm a NEW lead, returning its message_id in the
    ARMED state with five QUEUED touch orders. Each stop-scenario uses its own
    lead + email so latching one never bleeds into another."""
    message_id = _ingest(external_id=external_id, email=email)
    _run_ack()
    _arm(message_id)
    return message_id


def _run_ack() -> None:
    from src.tasks.speed_to_lead_sweep import run_sweep

    _exec(
        "UPDATE inbound_messages SET send_at = NOW() - INTERVAL '1 minute' "
        "WHERE client_id = :c AND status = 'RECEIVED'",
        c=CLIENT_ID,
    )
    run_sweep(limit=50)


def _arm(message_id: int) -> None:
    from src.tasks.sequence_sweep import run_sweep

    _exec(
        "UPDATE agent_work_orders SET due_at = NOW() - INTERVAL '1 minute' "
        "WHERE client_id = :c AND entity_id = :message_id AND action_class = 'STL_CADENCE_ARM'",
        c=CLIENT_ID,
        message_id=str(message_id),
    )
    run_sweep(client_id=CLIENT_ID)


def _touch_order_id(message_id: int, touch_step: int, status: str = "QUEUED"):
    row = _one(
        "SELECT action_id FROM agent_work_orders WHERE client_id = :c "
        "AND action_class = 'DISPATCH_STL_CADENCE_TOUCH' AND entity_id = :mid "
        "AND status = :status AND (payload->>'touch_step')::int = :step LIMIT 1",
        c=CLIENT_ID,
        mid=str(message_id),
        status=status,
        step=touch_step,
    )
    return row["action_id"] if row else None


def _approve(action_id) -> None:
    from src.services import work_orders as wo

    approved = wo.record_decision(CLIENT_ID, str(action_id), decision="APPROVED", decided_by="e2e")
    if approved is None:
        raise RuntimeError(f"could not approve STL touch {action_id}")


def _execute_all_tenants() -> None:
    """Drive the SAME all-tenant execution worker the deployed process runs —
    NOT cmd_sweep directly. This is what proves an approved touch actually
    dispatches in production (the runtime gap the review flagged)."""
    from src.tasks.work_order_execution_sweep import run_sweep

    run_sweep()


def _approve_and_execute_touch(message_id: int, touch_step: int) -> None:
    action_id = _touch_order_id(message_id, touch_step)
    if action_id is None:
        raise RuntimeError(f"no queued STL touch {touch_step} for message {message_id}")
    _approve(action_id)
    _execute_all_tenants()


def _drive_reply(email: str) -> None:
    """Drive the REAL inbound-reply ingest so the stop hook wired into
    inbound_ingest actually fires — never a fabricated STOPPED write.

    The STL stop is committed up front in ingest_inbound_reply, BEFORE the
    Task-3.1.3 cold-reply persistence that follows. That persistence targets an
    older inbound_messages shape and is independently broken on the deployed
    schema (a documented pre-existing issue, out of 4.2.2 scope), so it raises
    an UndefinedColumn AFTER our stop has already committed. We swallow only
    that specific, known downstream error — anything else propagates."""
    import asyncio
    import uuid as _uuid

    from sqlalchemy.exc import ProgrammingError

    from src.services.inbound_ingest import InboundParsed, ingest_inbound_reply

    parsed = InboundParsed(
        to_alias=f"{CLIENT_ID}@inbound.getblackink.com",
        from_raw=email,
        subject="Re: your inquiry",
        raw_body="Yes, I'm interested — please call me.",
        inbound_message_id=f"<e2e-reply-{_uuid.uuid4()}@mail.test>",
        in_reply_to=None,
    )
    try:
        asyncio.run(ingest_inbound_reply(parsed))
    except ProgrammingError as exc:
        if "does not exist" not in str(exc):
            raise
        print("     (note: Task-3.1.3 cold-reply persistence is broken on this "
              "schema — expected; STL stop already committed before it)")


def _drive_booking(email: str) -> None:
    """Drive the REAL booking ingest pipeline (_process_event) so the stop
    hook wired into booking_ingest fires — seeds a CLIENT_OWNER_BOOKING
    calendar connection and pushes one tagged CONFIRMED event."""
    import types
    import uuid as _uuid

    from src.services.booking_ingest import NormalizedEvent, SyncOutcome, _process_event

    with get_owner_db_context() as session:
        connection_id = session.execute(
            text(
                "INSERT INTO calendar_connections "
                "(client_id, provider, external_calendar_id, subscription_id, verification_secret, "
                " initial_sync_done, connection_scope) "
                "VALUES (:c, 'GOOGLE', 'primary', :sub, 'secret', TRUE, 'CLIENT_OWNER_BOOKING') "
                "RETURNING connection_id"
            ),
            {"c": CLIENT_ID, "sub": f"sub-{_uuid.uuid4().hex[:12]}"},
        ).scalar_one()

    connection = types.SimpleNamespace(
        client_id=CLIENT_ID, provider="GOOGLE", connection_scope="CLIENT_OWNER_BOOKING",
    )
    event = NormalizedEvent(
        external_event_id=f"evt-{_uuid.uuid4().hex[:12]}",
        tagged=True,
        event_status="CONFIRMED",
        scheduled_at=datetime.now(timezone.utc) + timedelta(days=2),
        created_at=datetime.now(timezone.utc),
        client_rep_name=None,
        client_rep_email=None,
        owner_name="Cadence Test Lead",
        owner_email=email,
        owner_phone=None,
        raw_payload={},
    )
    with get_system_db_context() as session:
        _process_event(session, connection, connection_id, event, SyncOutcome(), connect_boundary=None)
        session.commit()


def _assert_core(checks: Checks, message_id: int) -> None:
    message = _one(
        "SELECT status, cadence_state, mailbox_id FROM inbound_messages WHERE id = :id",
        id=message_id,
    )
    orders = _one(
        "SELECT COUNT(*) AS count FROM agent_work_orders WHERE client_id = :c "
        "AND entity_id = :message_id AND action_class = 'DISPATCH_STL_CADENCE_TOUCH'",
        c=CLIENT_ID,
        message_id=str(message_id),
    )
    dispatch = _one(
        "SELECT status, mailbox_id FROM stl_cadence_dispatches WHERE client_id = :c "
        "AND message_id = :message_id AND touch_step = 1",
        c=CLIENT_ID,
        message_id=message_id,
    )
    event = _one(
        "SELECT 1 FROM events WHERE client_id = :c AND event_type = 'outbound_touch_dispatched' "
        "AND entity_type = 'inbound_message' AND entity_id = :message_id",
        c=CLIENT_ID,
        message_id=str(message_id),
    )
    checks.require("initial acknowledgement was sent", message is not None and message["status"] == "RESPONDED")
    checks.require("cadence armed after 24h quiet period", message is not None and message["cadence_state"] == "ARMED")
    checks.require("five follow-up work orders were scheduled", orders is not None and orders["count"] == 5)
    checks.require("approved touch dispatched once", dispatch is not None and dispatch["status"] == "SENT")
    checks.require("cadence touch records its mailbox", dispatch is not None and dispatch["mailbox_id"] is not None)
    checks.require("cadence touch event logged", event is not None)


def _assert_unsubscribe_stop(checks: Checks, message_id: int) -> None:
    from src.services.email_unsubscribe import unsubscribe_url
    from src.api.main import app

    token = parse_qs(urlparse(unsubscribe_url(CLIENT_ID, PROSPECT_EMAIL)).query)["token"][0]
    response = TestClient(app).get(f"/api/v1/public/unsubscribe?token={token}")
    row = _one("SELECT cadence_state, cadence_stop_reason FROM inbound_messages WHERE id = :id", id=message_id)
    checks.require("one-click unsubscribe succeeds", response.status_code == 200, str(response.status_code))
    checks.require(
        "unsubscribe stops the active cadence",
        row is not None and row["cadence_state"] == "STOPPED" and row["cadence_stop_reason"] == "OPT_OUT",
    )
    remaining = _one(
        "SELECT COUNT(*) AS n FROM agent_work_orders WHERE client_id = :c AND entity_id = :mid "
        "AND action_class = 'DISPATCH_STL_CADENCE_TOUCH' AND status = 'QUEUED'",
        c=CLIENT_ID, mid=str(message_id),
    )
    checks.require(
        "opt-out eagerly cancels every still-queued touch",
        remaining is not None and remaining["n"] == 0,
        f"{remaining['n'] if remaining else '?'} still QUEUED",
    )


def _assert_at_most_once(checks: Checks, message_id: int) -> None:
    """Touch 1 already SENT — a second claim for the same (message, step) must
    be refused, so a re-dispatch can never resend."""
    from src.services.stl_cadence import _claim_dispatch

    with get_system_db_context() as session:
        second = _claim_dispatch(session, message_id, 1, CLIENT_ID, status="SENDING")
        session.rollback()
    checks.require("touch 1 cannot be claimed twice (at-most-once)", second is False)


def _assert_all_touches_completed(checks: Checks) -> None:
    message_id = _fresh_armed_lead("e2e-stl-complete", "complete@e2e-stl-cadence.test")
    for step in (1, 2, 3, 4, 5):
        _approve_and_execute_touch(message_id, step)
    message = _one("SELECT cadence_state FROM inbound_messages WHERE id = :id", id=message_id)
    sent = _one(
        "SELECT COUNT(*) AS n FROM stl_cadence_dispatches WHERE client_id = :c AND message_id = :mid "
        "AND status = 'SENT'",
        c=CLIENT_ID, mid=message_id,
    )
    checks.require("all five touches dispatched", sent is not None and sent["n"] == 5, f"{sent['n'] if sent else '?'}/5 SENT")
    checks.require("cadence marked COMPLETED after touch 5", message is not None and message["cadence_state"] == "COMPLETED")


def _assert_reply_stop(checks: Checks) -> None:
    email = "reply@e2e-stl-cadence.test"
    message_id = _fresh_armed_lead("e2e-stl-reply", email)
    _drive_reply(email)
    row = _one("SELECT cadence_state, cadence_stop_reason FROM inbound_messages WHERE id = :id", id=message_id)
    skipped = _one(
        "SELECT COUNT(*) AS n FROM agent_work_orders WHERE client_id = :c AND entity_id = :mid "
        "AND action_class = 'DISPATCH_STL_CADENCE_TOUCH' AND status = 'SKIPPED'",
        c=CLIENT_ID, mid=str(message_id),
    )
    checks.require(
        "reply stops the cadence (REPLY)",
        row is not None and row["cadence_state"] == "STOPPED" and row["cadence_stop_reason"] == "REPLY",
    )
    checks.require("reply cancels all five queued touches", skipped is not None and skipped["n"] == 5,
                   f"{skipped['n'] if skipped else '?'}/5 SKIPPED")


def _assert_booking_stop(checks: Checks) -> None:
    email = "booking@e2e-stl-cadence.test"
    message_id = _fresh_armed_lead("e2e-stl-booking", email)
    _drive_booking(email)
    row = _one("SELECT cadence_state, cadence_stop_reason FROM inbound_messages WHERE id = :id", id=message_id)
    checks.require(
        "booking stops the cadence (BOOKED)",
        row is not None and row["cadence_state"] == "STOPPED" and row["cadence_stop_reason"] == "BOOKED",
    )


def _assert_post_send_guard(checks: Checks) -> None:
    """Force a post-SMTP write failure and prove the touch goes terminal
    SENT_UNCONFIRMED (never reset) so no retry can resend it."""
    import types
    from unittest.mock import patch

    from src.services import stl_cadence

    message_id = _fresh_armed_lead("e2e-stl-postsend", "postsend@e2e-stl-cadence.test")
    payload = _one(
        "SELECT payload FROM agent_work_orders WHERE client_id = :c AND entity_id = :mid "
        "AND action_class = 'DISPATCH_STL_CADENCE_TOUCH' AND (payload->>'touch_step')::int = 1 LIMIT 1",
        c=CLIENT_ID, mid=str(message_id),
    )
    order = types.SimpleNamespace(client_id=CLIENT_ID, entity_id=str(message_id), payload=payload["payload"])

    raised = False
    with patch("src.services.stl_cadence.log_event", side_effect=RuntimeError("forced post-send failure")):
        try:
            stl_cadence.dispatch_stl_cadence_touch(order)
        except stl_cadence._PostSendError:
            raised = True

    dispatch = _one(
        "SELECT status FROM stl_cadence_dispatches WHERE client_id = :c AND message_id = :mid AND touch_step = 1",
        c=CLIENT_ID, mid=message_id,
    )
    with get_system_db_context() as session:
        reclaim = stl_cadence._claim_dispatch(session, message_id, 1, CLIENT_ID, status="SENDING")
        session.rollback()

    checks.require("post-send failure raises _PostSendError", raised)
    checks.require("dispatch left terminal SENT_UNCONFIRMED (not reset)",
                   dispatch is not None and dispatch["status"] == "SENT_UNCONFIRMED",
                   dispatch["status"] if dispatch else "missing")
    checks.require("no resend possible after post-send failure", reclaim is False)


def _assert_cap_defer(checks: Checks) -> None:
    """Seed the mailbox to its 24h cap with STL dispatches and prove the next
    touch DEFERS (SNOOZED work order, no send) — Fix 4, STL counting against caps."""
    from src.services.mailbox_dispatcher import MAX_DAILY_SEND_CAP

    message_id = _fresh_armed_lead("e2e-stl-cap", "cap@e2e-stl-cadence.test")
    mailbox = _one("SELECT id FROM mailboxes WHERE client_id = :c LIMIT 1", c=CLIENT_ID)
    mailbox_id = mailbox["id"]
    with get_owner_db_context() as session:
        for i in range(MAX_DAILY_SEND_CAP):
            session.execute(
                text(
                    "INSERT INTO stl_cadence_dispatches (client_id, message_id, touch_step, status, mailbox_id, sent_at, created_at) "
                    "VALUES (:c, :mid, 1, 'SENT', :mb, NOW(), NOW())"
                ),
                {"c": CLIENT_ID, "mid": 900000 + i, "mb": mailbox_id},
            )

    _approve_and_execute_touch(message_id, 1)

    order = _one(
        "SELECT status FROM agent_work_orders WHERE client_id = :c AND entity_id = :mid "
        "AND action_class = 'DISPATCH_STL_CADENCE_TOUCH' AND (payload->>'touch_step')::int = 1 LIMIT 1",
        c=CLIENT_ID, mid=str(message_id),
    )
    sent = _one(
        "SELECT COUNT(*) AS n FROM stl_cadence_dispatches WHERE client_id = :c AND message_id = :mid AND touch_step = 1 AND status = 'SENT'",
        c=CLIENT_ID, mid=message_id,
    )
    checks.require("capped mailbox defers the touch (SNOOZED)", order is not None and order["status"] == "SNOOZED",
                   order["status"] if order else "missing")
    checks.require("no send occurred while capped", sent is not None and sent["n"] == 0)


def run() -> int:
    checks = Checks()
    _require_schema()
    cleanup()
    try:
        seed()

        # ── Core happy path (lead 1): ack → arm → approve+dispatch touch 1 ────
        # Dispatch is driven through the all-tenant work_order_execution_sweep,
        # the SAME worker the deployed process runs — proving an approved touch
        # actually sends in production, not just that cmd_sweep works.
        message_id = _ingest()
        _run_ack()
        _arm(message_id)
        _approve_and_execute_touch(message_id, 1)
        _assert_core(checks, message_id)
        _assert_at_most_once(checks, message_id)
        # Lead 1 is still ARMED (touches 2–5 queued) — opt-out here proves the
        # stop latch + eager cancel on a live cadence.
        _assert_unsubscribe_stop(checks, message_id)

        # Each remaining scenario is isolated so one failure can't abort the
        # rest — every scenario seeds its own lead and reports independently.
        for label, fn in (
            ("full five-touch run to COMPLETED", _assert_all_touches_completed),
            ("reply stops cadence", _assert_reply_stop),
            ("booking stops cadence", _assert_booking_stop),
            ("post-send failure guard", _assert_post_send_guard),
            ("mailbox cap defer", _assert_cap_defer),
        ):
            try:
                fn(checks)
            except Exception as exc:  # noqa: BLE001 — surface as a FAIL, don't abort
                checks.require(label, False, f"scenario raised: {type(exc).__name__}: {exc}")

        print(f"RESULT: {'PASS' if checks.failed == 0 else 'FAIL'} ({checks.failed} failed checks)")
        return 0 if checks.failed == 0 else 1
    finally:
        cleanup()


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--real"]
    if args != ["run"]:
        print("usage: python scripts/e2e_stl_cadence.py run [--real]")
        return 2
    if _REAL and not (os.environ.get("SMTP_HOST") and os.environ.get("SMTP_PASSWORD")):
        print("ERROR: --real requires SMTP_HOST and SMTP_PASSWORD from .env.test")
        return 2
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
