"""SLA escalation sweep for the Respond Reply Triage Agent (Subtask 2.1.2).

Three escalation tiers, measured from inbound_messages.received_at:

  Tier 1 (escalation_level 0 → 1):  sla_due_at has passed AND card was posted
      → second high-priority ping in #blackink-setter

  Tier 2 (escalation_level 1 → 2):  received_at + 60 min has passed
      → executive alert to #blackink-command

  Tier 3 (escalation_level 2 → 3, or reset to 0):  received_at + 240 min has passed
      → S-13: reallocated to the client's own backup_closer_roster
        (round-robin, least-recently-assigned) — escalation_level resets to
        0 and a fresh 60-minute SLA window starts under the new assignee,
        so a reallocated lead is tracked again, not left as a dead terminal
        status. If the client has no active roster configured, falls back
        to the pre-S-13 terminal REALLOCATED status with a manual-assign
        note (fail-closed — never invents an assignee).

Each tier atomically sets escalation_level before posting — if the Slack post
fails the level is already written, so the next sweep tick won't re-fire.

Runs as blackink_system (BYPASSRLS) to see all tenants' ROUTED rows.

    python -m src.tasks.respond_sla_sweep
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
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


async def _alert_reallocated(msg: dict, closer: Optional[dict]) -> None:
    sender = msg.get("sender_name") or msg.get("sender_email") or "unknown sender"
    intent = msg.get("intent") or "reply"
    if closer:
        text_body = (
            f":arrows_counterclockwise: *Lead reallocated — 240 minutes unclaimed*\n"
            f"Client: `{msg['client_id']}` | From: `{sender}` | ID: `{msg['id']}`\n"
            f"Assigned to <@{closer['slack_user_id']}> "
            f"({closer.get('display_name') or closer['slack_user_id']}). New 60-minute SLA window started."
        )
    else:
        text_body = (
            f":no_entry: *Lead reallocated — 240 minutes unclaimed*\n"
            f"Client: `{msg['client_id']}` | From: `{sender}` | ID: `{msg['id']}`\n"
            f"No active backup closer roster configured for this client — "
            f"marked REALLOCATED. Assign manually and provision backup_closer_roster."
        )
    await post_notice(channel_key="command", text=text_body)


def _pick_backup_closer(db, client_id: str, as_of: datetime) -> Optional[dict]:
    """Round-robin: claims the least-recently-assigned active roster row for
    this client (SELECT ... FOR UPDATE SKIP LOCKED so two concurrent sweep
    ticks can never assign the same slot to two different leads at once).
    Returns None if the client has no active roster row — the caller must
    fail closed on that, not invent an assignee."""
    row = db.execute(
        text(
            "UPDATE backup_closer_roster "
            "SET last_assigned_at = :now, updated_at = :now "
            "WHERE id = ("
            "    SELECT id FROM backup_closer_roster "
            "    WHERE client_id = :client_id AND is_active = TRUE "
            "    ORDER BY last_assigned_at ASC NULLS FIRST, id "
            "    FOR UPDATE SKIP LOCKED LIMIT 1"
            ") "
            "RETURNING slack_user_id, display_name"
        ),
        {"now": as_of, "client_id": client_id},
    ).mappings().first()
    return dict(row) if row else None


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
    """60 minutes elapsed, fire command alert.

    Code-review fix (S-13): measures elapsed time from
    COALESCE(reallocated_at, received_at), not bare received_at. A lead
    that has already been through one reallocation has a received_at that
    is, by definition, already more than 240 minutes old — anchoring to the
    original received_at would make tier2's 60-minute gate permanently
    satisfied the instant the lead re-enters escalation_level=1, collapsing
    the "fresh SLA window" tier3 just started down to zero. Anchoring to
    reallocated_at (when set) gives the reallocated lead a genuine 60
    minutes before tier2 re-fires, mirroring how a never-reallocated lead's
    tier2 is timed from its own received_at."""
    rows = db.execute(
        text(
            "SELECT id, client_id, intent, sender_email, sender_name "
            "FROM inbound_messages "
            "WHERE status = 'ROUTED' "
            "  AND claimed_at IS NULL "
            "  AND escalation_level = 1 "
            "  AND COALESCE(reallocated_at, received_at) < :now - INTERVAL '60 minutes' "
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
    """240 minutes elapsed — reallocate.

    Code-review fix (S-13): same COALESCE(reallocated_at, received_at)
    anchor as tier2, for the identical reason — without it, a lead already
    once-reallocated would immediately re-satisfy this 240-minute gate
    (its received_at is already hours old) the moment it cycles back to
    escalation_level=2, defeating the fresh-window promise and causing the
    lead to reallocate to a NEW closer every ~60-120 seconds instead of
    every 240 minutes. Anchoring to reallocated_at gives each successive
    backup closer a genuine 240-minute window before the next handoff."""
    rows = db.execute(
        text(
            "SELECT id, client_id, intent, sender_email, sender_name "
            "FROM inbound_messages "
            "WHERE status = 'ROUTED' "
            "  AND claimed_at IS NULL "
            "  AND escalation_level = 2 "
            "  AND COALESCE(reallocated_at, received_at) < :now - INTERVAL '240 minutes' "
            "ORDER BY received_at "
            "LIMIT 50"
        ),
        {"now": as_of},
    ).mappings().fetchall()

    fired = 0
    for row in rows:
        row = dict(row)
        closer = _pick_backup_closer(db, row["client_id"], as_of)
        if closer:
            # Reallocated: fresh SLA window under the new assignee — reset
            # to escalation_level 0 so tier1/tier2/tier3 can fire again for
            # THIS assignment if they also fail to claim it in time, rather
            # than the lead going dark in a dead terminal state.
            db.execute(
                text(
                    "UPDATE inbound_messages "
                    "SET escalation_level = 0, "
                    "    assigned_closer_slack_user_id = :closer_id, "
                    "    reallocated_at = :now, "
                    "    sla_due_at = :new_sla, "
                    "    claimed_at = NULL "
                    "WHERE id = :id AND escalation_level = 2"
                ),
                {
                    "closer_id": closer["slack_user_id"],
                    "now": as_of,
                    "new_sla": as_of + timedelta(minutes=60),
                    "id": row["id"],
                },
            )
        else:
            # Fail closed: no active roster configured for this client —
            # keep the pre-S-13 terminal behavior rather than guessing an
            # assignee.
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
            asyncio.run(_alert_reallocated(row, closer))
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
