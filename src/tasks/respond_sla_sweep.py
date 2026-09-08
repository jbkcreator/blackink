"""SLA escalation sweep for the Respond Reply Triage Agent (Subtask 2.1.2).

Three escalation tiers, measured from inbound_messages.received_at:

  Tier 1 (escalation_level 0 → 1):  sla_due_at has passed AND card was posted
      → second high-priority ping in #blackink-setter

  Tier 2 (escalation_level 1 → 2):  received_at + 60 min has passed
      → executive alert to #blackink-command

  Tier 3 (escalation_level 2 → 3):  received_at + 240 min has passed
      → status = REALLOCATED, note in #blackink-command

Each tier atomically sets escalation_level before posting — if the Slack post
fails the level is already written, so the next sweep tick won't re-fire.

Runs as blackink_system (BYPASSRLS) to see all tenants' ROUTED rows.

    python -m src.tasks.respond_sla_sweep
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.slack.post import post_notice

logger = logging.getLogger(__name__)

# ── Escalation helpers ────────────────────────────────────────────────────────

async def _ping_setter(msg: dict) -> None:
    sender = msg.get("sender_name") or msg.get("sender_email") or "unknown sender"
    intent = msg.get("intent") or "reply"
    text_body = (
        f":warning: *Unclaimed {intent} — SLA breached*\n"
        f"Client: `{msg['client_id']}` | From: `{sender}` | ID: `{msg['id']}`\n"
        f"This lead has been waiting past its SLA. Claim it in #blackink-setter."
    )
    await post_notice(channel_key="setter", text=text_body)


async def _alert_command(msg: dict) -> None:
    sender = msg.get("sender_name") or msg.get("sender_email") or "unknown sender"
    intent = msg.get("intent") or "reply"
    text_body = (
        f":rotating_light: *Unclaimed {intent} — 60 minutes elapsed*\n"
        f"Client: `{msg['client_id']}` | From: `{sender}` | ID: `{msg['id']}`\n"
        f"No rep has claimed this lead. Intervention required."
    )
    await post_notice(channel_key="command", text=text_body)


async def _alert_reallocated(msg: dict) -> None:
    sender = msg.get("sender_name") or msg.get("sender_email") or "unknown sender"
    intent = msg.get("intent") or "reply"
    text_body = (
        f":no_entry: *Lead reallocated — 240 minutes unclaimed*\n"
        f"Client: `{msg['client_id']}` | From: `{sender}` | ID: `{msg['id']}`\n"
        f"Marked REALLOCATED. Assign to backup closer queue manually."
    )
    await post_notice(channel_key="command", text=text_body)


# ── Sweep tiers ───────────────────────────────────────────────────────────────

def _run_tier1(db, as_of: datetime) -> int:
    """SLA due, not yet pinged. Re-ping #blackink-setter."""
    rows = db.execute(
        text(
            "SELECT id, client_id, intent, sender_email, sender_name "
            "FROM inbound_messages "
            "WHERE status = 'ROUTED' "
            "  AND claimed_at IS NULL "
            "  AND escalation_level = 0 "
            "  AND sla_due_at IS NOT NULL "
            "  AND sla_due_at < :now "
            "  AND card_ts IS NOT NULL "   # only escalate messages that had a card posted
            "ORDER BY sla_due_at "
            "LIMIT 50"
        ),
        {"now": as_of},
    ).mappings().fetchall()

    fired = 0
    for row in rows:
        row = dict(row)
        db.execute(
            text("UPDATE inbound_messages SET escalation_level = 1 WHERE id = :id AND escalation_level = 0"),
            {"id": row["id"]},
        )
        db.commit()
        try:
            asyncio.run(_ping_setter(row))
        except Exception:
            logger.exception("respond_sla_sweep: tier1 Slack ping failed for id=%s", row["id"])
        fired += 1

    return fired


def _run_tier2(db, as_of: datetime) -> int:
    """60 minutes elapsed, fire command alert."""
    rows = db.execute(
        text(
            "SELECT id, client_id, intent, sender_email, sender_name "
            "FROM inbound_messages "
            "WHERE status = 'ROUTED' "
            "  AND claimed_at IS NULL "
            "  AND escalation_level = 1 "
            "  AND received_at < :now - INTERVAL '60 minutes' "
            "ORDER BY received_at "
            "LIMIT 50"
        ),
        {"now": as_of},
    ).mappings().fetchall()

    fired = 0
    for row in rows:
        row = dict(row)
        db.execute(
            text("UPDATE inbound_messages SET escalation_level = 2 WHERE id = :id AND escalation_level = 1"),
            {"id": row["id"]},
        )
        db.commit()
        try:
            asyncio.run(_alert_command(row))
        except Exception:
            logger.exception("respond_sla_sweep: tier2 Slack alert failed for id=%s", row["id"])
        fired += 1

    return fired


def _run_tier3(db, as_of: datetime) -> int:
    """240 minutes elapsed — reallocate."""
    rows = db.execute(
        text(
            "SELECT id, client_id, intent, sender_email, sender_name "
            "FROM inbound_messages "
            "WHERE status = 'ROUTED' "
            "  AND claimed_at IS NULL "
            "  AND escalation_level = 2 "
            "  AND received_at < :now - INTERVAL '240 minutes' "
            "ORDER BY received_at "
            "LIMIT 50"
        ),
        {"now": as_of},
    ).mappings().fetchall()

    fired = 0
    for row in rows:
        row = dict(row)
        db.execute(
            text(
                "UPDATE inbound_messages "
                "SET escalation_level = 3, status = 'REALLOCATED' "
                "WHERE id = :id AND escalation_level = 2"
            ),
            {"id": row["id"]},
        )
        db.commit()
        try:
            asyncio.run(_alert_reallocated(row))
        except Exception:
            logger.exception("respond_sla_sweep: tier3 Slack alert failed for id=%s", row["id"])
        fired += 1

    return fired


# ── Public entry ──────────────────────────────────────────────────────────────

def run_sweep(*, as_of: Optional[datetime] = None) -> dict[str, int]:
    """Run one full pass of all three escalation tiers. Returns tier counts."""
    as_of = as_of or datetime.now(timezone.utc)
    counts = {"tier1": 0, "tier2": 0, "tier3": 0}

    with get_system_db_context() as db:
        try:
            counts["tier3"] = _run_tier3(db, as_of)
        except Exception:
            logger.exception("respond_sla_sweep: tier3 failed")
        try:
            counts["tier2"] = _run_tier2(db, as_of)
        except Exception:
            logger.exception("respond_sla_sweep: tier2 failed")
        try:
            counts["tier1"] = _run_tier1(db, as_of)
        except Exception:
            logger.exception("respond_sla_sweep: tier1 failed")

    logger.info(
        "respond_sla_sweep: tier1=%d tier2=%d tier3=%d",
        counts["tier1"], counts["tier2"], counts["tier3"],
    )
    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_sweep()
