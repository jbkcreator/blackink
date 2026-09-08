"""
E2E test for Subtask 2.1.2 — Context Cards & SLA Escalation.

Runs against the live server DB + local Redis.
Slack is mocked in-process — no real Slack calls are made.

Usage:
    PYTHONPATH=. python scripts/e2e_context_cards.py

Cleans up after itself (deletes the test inbound_messages row).
"""
from __future__ import annotations

import asyncio
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import text

from src.agents.respond import queue
from src.agents.respond.context_cards import CONTEXT_CARD_INTENTS, compute_card_hash
from src.agents.respond.intents import Intent
from src.agents.respond.queue import InboundQueueMessage
from src.agents.respond.worker import _compute_sla, _process_message
from src.core.database import get_system_db_context
from src.tasks.respond_sla_sweep import run_sweep

# ── Config ────────────────────────────────────────────────────────────────────

CLIENT_ID    = "DEMO_FRIDAY_SANDBOX"
SENDER_EMAIL = "e2e-test@blackink-test.internal"
SENDER_NAME  = "E2E Test Owner"

INTENTS_TO_TEST = [
    (
        "HOT_LEAD",
        "Hi, I would love to schedule a call this week to move forward with property management.",
        15,
    ),
    (
        "OBJECTION",
        "We are happy with our current property management company and are not looking to make any changes.",
        60,
    ),
]

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "  -->"

_errors: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {label}")
    else:
        msg = f"  {FAIL} {label}" + (f"  ({detail})" if detail else "")
        print(msg)
        _errors.append(label)


# ── Slack mock ────────────────────────────────────────────────────────────────

FAKE_TS         = "1725700000.123456"
FAKE_CHANNEL_ID = "C_MOCK_SETTER"

def _make_slack_mock():
    """Returns a mock for src.services.slack.post with enough surface to satisfy
    post_context_card() and the SLA sweep's post_notice()."""
    mock = MagicMock()
    mock.post_action_card = AsyncMock(return_value={
        "message_ts":  FAKE_TS,
        "channel_id":  FAKE_CHANNEL_ID,
    })
    mock.post_notice = AsyncMock(return_value=None)
    return mock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_row(db, body_text: str) -> tuple[int, str]:
    idem_key = f"e2e-{uuid.uuid4()}"
    row_id = db.execute(text("""
        INSERT INTO inbound_messages
          (client_id, sender_email, sender_name, subject, body_text,
           idempotency_key, destination_address, status, received_at)
        VALUES
          (:cid, :email, :name, :subj, :body,
           :ikey, :dest, 'PENDING', NOW())
        RETURNING id
    """), {
        "cid":   CLIENT_ID,
        "email": SENDER_EMAIL,
        "name":  SENDER_NAME,
        "subj":  "Re: Managing your property",
        "body":  body_text,
        "ikey":  idem_key,
        "dest":  f"respond+{CLIENT_ID.lower()}@getblackink.com",
    }).scalar()
    db.commit()
    return row_id, idem_key


def _fetch_row(db_id: int) -> dict:
    with get_system_db_context() as db:
        row = db.execute(text("""
            SELECT id, status, intent, sla_due_at, card_ts, card_channel_id,
                   card_posted_at, escalation_level, claimed_at, claimed_by,
                   received_at
            FROM inbound_messages WHERE id = :id
        """), {"id": db_id}).mappings().first()
        return dict(row) if row else {}


def _delete_row(db_id: int) -> None:
    with get_system_db_context() as db:
        db.execute(text("DELETE FROM inbound_messages WHERE id = :id"), {"id": db_id})
        db.commit()


def _backdate(db_id: int, received_at_offset: timedelta, escalation_level: int,
              unclaim: bool = False) -> None:
    with get_system_db_context() as db:
        db.execute(text("""
            UPDATE inbound_messages
            SET received_at      = NOW() + :offset,
                sla_due_at       = NOW() - INTERVAL '1 minute',
                escalation_level = :lvl
                """ + (", claimed_at = NULL, claimed_by = NULL" if unclaim else "") + """
            WHERE id = :id
        """), {"offset": received_at_offset, "lvl": escalation_level, "id": db_id})
        db.commit()


# ── Per-intent E2E run ────────────────────────────────────────────────────────

def run_intent(label: str, body_text: str, expected_sla_min: int) -> None:
    print(f"\n{'='*60}")
    print(f"  {INFO} Intent: {label}  (expected SLA: {expected_sla_min} min)")
    print(f"{'='*60}")

    slack_mock = _make_slack_mock()
    db_id: int | None = None

    try:
        # ── Step 1: Seed ──────────────────────────────────────────────────────
        print(f"\n{INFO} Step 1 — Seed inbound_messages row")
        with get_system_db_context() as db:
            db_id, idem_key = _seed_row(db, body_text)
        print(f"  db_id = {db_id}")
        check("Row created with status=PENDING", _fetch_row(db_id)["status"] == "PENDING")

        # ── Step 2: Publish to Redis (smoke-test only — live worker may consume) ──
        print(f"\n{INFO} Step 2 — Publish to Redis stream")
        stream_id = queue.publish(db_id=db_id, client_id=CLIENT_ID, idempotency_key=idem_key)
        check("Published to Redis stream", bool(stream_id), f"stream_id={stream_id}")

        # ── Step 3: Worker (direct call, Slack mocked) ────────────────────────
        # We drive _process_message directly so we don't race a live worker.
        # The row may already be PROCESSING if the live worker got there first;
        # _process_message handles that gracefully (idempotent ack path).
        print(f"\n{INFO} Step 3 — Run worker (direct call, Slack mocked)")
        fake_msg = InboundQueueMessage(
            message_id="0-0",          # synthetic; ack is a no-op for unknown IDs
            db_id=db_id,
            client_id=CLIENT_ID,
            idempotency_key=idem_key,
        )
        with patch("src.agents.respond.context_cards.slack_post", slack_mock), \
             patch("src.agents.respond.context_cards.log_event", MagicMock()):
            _process_message(fake_msg)

        row = _fetch_row(db_id)
        print(f"  status={row.get('status')}  intent={row.get('intent')}  "
              f"escalation_level={row.get('escalation_level')}")
        print(f"  sla_due_at={row.get('sla_due_at')}  card_ts={row.get('card_ts')}")

        check("status=ROUTED",            row.get("status") == "ROUTED")
        check(f"intent={label}",          row.get("intent") == label)
        check("sla_due_at set",           row.get("sla_due_at") is not None)
        check("card_ts set (mock)",       row.get("card_ts") == FAKE_TS)
        check("card_channel_id set",      row.get("card_channel_id") == FAKE_CHANNEL_ID)
        check("card_posted_at set",       row.get("card_posted_at") is not None)
        check("escalation_level=0",       row.get("escalation_level") == 0)
        check("claimed_at is NULL",       row.get("claimed_at") is None)
        check("Slack post_action_card called once",
              slack_mock.post_action_card.call_count == 1)

        # Verify SLA window
        if row.get("sla_due_at") and row.get("status") == "ROUTED":
            diff = (row["sla_due_at"] - row["received_at"]).total_seconds() / 60
            check(
                f"SLA = {expected_sla_min} min",
                abs(diff - expected_sla_min) < 1,
                f"actual={diff:.1f} min",
            )

        # Verify card hash is stable
        if row.get("card_posted_at"):
            posted_iso = row["card_posted_at"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            h1 = compute_card_hash(db_id, CLIENT_ID, label, posted_iso)
            h2 = compute_card_hash(db_id, CLIENT_ID, label, posted_iso)
            check("Card hash is deterministic", h1 == h2 and len(h1) == 64)

        # ── Step 4: SLA sweep — tier 1 ───────────────────────────────────────
        print(f"\n{INFO} Step 4 — SLA sweep tier 1 (back-date to simulate breach)")
        _backdate(db_id, timedelta(minutes=-20), escalation_level=0, unclaim=True)

        with patch("src.tasks.respond_sla_sweep.post_notice", AsyncMock()) as mock_notice:
            counts = run_sweep()

        check("Tier 1 fired",             counts["tier1"] >= 1, str(counts))
        check("escalation_level --> 1",     _fetch_row(db_id)["escalation_level"] == 1)

        # ── Step 5: SLA sweep — tier 2 ───────────────────────────────────────
        print(f"\n{INFO} Step 5 — SLA sweep tier 2 (+65 min)")
        _backdate(db_id, timedelta(minutes=-65), escalation_level=1)

        with patch("src.tasks.respond_sla_sweep.post_notice", AsyncMock()):
            counts = run_sweep()

        check("Tier 2 fired",             counts["tier2"] >= 1, str(counts))
        check("escalation_level --> 2",     _fetch_row(db_id)["escalation_level"] == 2)

        # ── Step 6: SLA sweep — tier 3 (REALLOCATED) ─────────────────────────
        print(f"\n{INFO} Step 6 — SLA sweep tier 3 (+245 min --> REALLOCATED)")
        _backdate(db_id, timedelta(minutes=-245), escalation_level=2)

        with patch("src.tasks.respond_sla_sweep.post_notice", AsyncMock()):
            counts = run_sweep()

        final = _fetch_row(db_id)
        check("Tier 3 fired",             counts["tier3"] >= 1, str(counts))
        check("escalation_level --> 3",     final["escalation_level"] == 3)
        check("status=REALLOCATED",       final["status"] == "REALLOCATED")

    except Exception:
        traceback.print_exc()
        _errors.append(f"{label}: unexpected exception")
    finally:
        if db_id is not None:
            _delete_row(db_id)
            print(f"\n  {INFO} Cleaned up db_id={db_id}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "="*60)
    print("  Subtask 2.1.2 — E2E Test (Slack mocked, live DB)")
    print("="*60)

    for label, body, sla_min in INTENTS_TO_TEST:
        run_intent(label, body, sla_min)

    print("\n" + "="*60)
    if _errors:
        print(f"  {FAIL} {len(_errors)} check(s) failed:")
        for e in _errors:
            print(f"       • {e}")
        sys.exit(1)
    else:
        print(f"  {PASS} All checks passed.")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
