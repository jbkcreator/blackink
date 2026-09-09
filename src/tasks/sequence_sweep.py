"""Sequence sweep — posts Slack approval cards for due QUEUED email-touch orders.

Reads agent_work_orders rows whose due_at has arrived (via due_batch) and
posts the approval card to #blackink-setter. A card-less QUEUED row is never
acted on: the sweep is the only thing that surfaces them to a human.

Runs on a minute-grain cron (every 2–5 min is sufficient — touches are
day-grain). Does NOT process APPROVED rows — that is cmd_sweep in
src/services/work_orders/__main__.py.

    python -m src.tasks.sequence_sweep [--client-id CLIENT_ID] [--limit N]

--client-id is optional; omitting it runs cross-tenant (system role) which is
the normal production mode. Providing it is useful for per-client debugging.
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from src.services import work_orders as wo

logger = logging.getLogger(__name__)

_EMAIL_TOUCH_ACTION = "DISPATCH_EMAIL_TOUCH"
_LINKEDIN_TASK_ACTION = "LINKEDIN_TASK"

# Compliance-block handling for LinkedIn touches (PR #26 finding 3). A block is
# either PERMANENT (opt-out / suppression — never self-resolves) or RETRYABLE
# (e.g. a DNC ABSTAIN that may clear once a vendor is wired). A permanent block
# is terminally SKIPPED so due_batch never re-selects it; a retryable block is
# deferred by a bounded backoff so it is re-checked at most once per interval
# (not every sweep tick), and terminally SKIPPED once the retry window is spent.
_LINKEDIN_RETRY_BACKOFF = timedelta(hours=6)
_LINKEDIN_MAX_RETRY_AGE = timedelta(days=3)

# Every touch action class the sweep surfaces, mapped to its Slack channel key
# (config/slack_channels.py). Without DIAL_TASK/LINKEDIN_TASK here they would
# sit QUEUED forever, never shown to a human (finding #4). LinkedIn has no
# dedicated channel in the blueprint's fixed vocabulary, so it goes to the
# setter channel alongside the email cards.
_ACTION_CHANNEL = {
    "DISPATCH_EMAIL_TOUCH": "setter",
    "DIAL_TASK": "dial",
    "LINKEDIN_TASK": "setter",
    "DISPATCH_WINBACK_TOUCH": "setter",       # Subtask 3.1.2
    "DISPATCH_STL_CADENCE_TOUCH": "setter",   # Task 4.2.2
}

_WINBACK_ACTION = "DISPATCH_WINBACK_TOUCH"
_STL_ARM_ACTION = "STL_CADENCE_ARM"
_STL_TOUCH_ACTION = "DISPATCH_STL_CADENCE_TOUCH"

# Action classes the sweep actually surfaces. DIAL_TASK is excluded on purpose
# (event-driven on Touch 1 approval, ADR 0001) — it is in _ACTION_CHANNEL only
# for channel resolution, never swept.
# STL_CADENCE_ARM is auto-executed (no card) — listed separately in _ARM_ACTIONS.
_SWEPT_ACTIONS = (_EMAIL_TOUCH_ACTION, _LINKEDIN_TASK_ACTION, _WINBACK_ACTION, _STL_TOUCH_ACTION)
_ARM_ACTIONS = (_STL_ARM_ACTION,)


async def _post_due_card(order, channel_key: str) -> bool:
    from src.services.slack.listeners import post_work_order_card

    posted = await post_work_order_card(order, channel_key=channel_key)
    return posted is not None


async def _post_due_linkedin_card(order) -> bool:
    """Post a due LINKEDIN_TASK card, but only after the per-touch compliance
    gate passes. On a block (including a global opt-out), skip the post and
    record a touch_skipped_compliance event instead. See
    docs/adr/0001-non-email-touch-posting-model.md.

    DB access is synchronous (get_db_context) inside this async function — same
    pattern as listeners._post_dial_task_after_touch1_approval."""
    from sqlalchemy import text

    from src.core.database import get_db_context
    from src.services.compliance_gate import evaluate_touch_gate
    from src.services.slack.listeners import post_work_order_card

    contact_id = int(order.entity_id)
    touch_step = int(order.payload.get("touch_step", 4)) if isinstance(order.payload, dict) else 4

    with get_db_context(client_id=order.client_id) as session:
        contact = session.execute(
            text("SELECT * FROM contacts WHERE contact_id = :cid"),
            {"cid": contact_id},
        ).fetchone()
        if contact is None:
            logger.error("sequence_sweep: LINKEDIN_TASK contact_id=%s not found — skipping", contact_id)
            return False

        gate = evaluate_touch_gate(session, contact, order.client_id)
        if not gate.ready:
            # Permanent = opt-out / suppression (never self-resolves). Everything
            # else is treated as retryable, but bounded: once the order has been
            # around longer than _LINKEDIN_MAX_RETRY_AGE it is terminally skipped
            # too, so nothing loops forever.
            now = datetime.now(timezone.utc)
            permanent = bool(getattr(contact, "is_opted_out", False) or getattr(contact, "suppression_state", False))
            order_created = getattr(order, "created_at", None)
            exhausted = order_created is not None and (now - order_created) > _LINKEDIN_MAX_RETRY_AGE
            terminal = permanent or exhausted
            disposition = "SKIPPED_PERMANENT" if permanent else ("SKIPPED_RETRY_EXHAUSTED" if exhausted else "DEFERRED")

            logger.info(
                "sequence_sweep: LINKEDIN_TASK compliance block contact_id=%s reasons=%s disposition=%s",
                contact_id, gate.blocked_reasons, disposition,
            )
            # Status change and the audit event share ONE transaction, so a
            # terminally-skipped order can never re-emit the event on a later
            # tick (the guarded WHERE status='QUEUED' makes the SKIP idempotent).
            if terminal:
                session.execute(
                    text(
                        "UPDATE agent_work_orders "
                        "SET status = 'SKIPPED', decided_by = 'sequence_sweep:compliance', "
                        "    decided_at = NOW(), updated_at = NOW() "
                        "WHERE action_id = :action_id AND client_id = :client_id AND status = 'QUEUED'"
                    ),
                    {"action_id": str(order.action_id), "client_id": order.client_id},
                )
            else:
                session.execute(
                    text(
                        "UPDATE agent_work_orders SET due_at = :next_due, updated_at = NOW() "
                        "WHERE action_id = :action_id AND client_id = :client_id AND status = 'QUEUED'"
                    ),
                    {"next_due": now + _LINKEDIN_RETRY_BACKOFF, "action_id": str(order.action_id), "client_id": order.client_id},
                )
            session.execute(
                text(
                    "INSERT INTO events (client_id, event_type, entity_type, entity_id, actor, payload) "
                    "VALUES (:client_id, 'touch_skipped_compliance', 'contact', :entity_id, 'sequence_sweep', :payload)"
                ),
                {
                    "client_id": order.client_id,
                    "entity_id": str(contact_id),
                    "payload": json.dumps({
                        "action_id": str(order.action_id),
                        "touch_step": touch_step,
                        "action_class": _LINKEDIN_TASK_ACTION,
                        "blocked_reasons": gate.blocked_reasons,
                        "disposition": disposition,
                    }),
                },
            )
            session.commit()
            return False

    posted = await post_work_order_card(order, channel_key="setter")
    if posted is not None:
        # Manual-task log (v2 §3.1.2 line 381): no browser/LinkedIn API involved.
        with get_db_context(client_id=order.client_id) as session:
            session.execute(
                text(
                    "INSERT INTO events (client_id, event_type, entity_type, entity_id, actor, payload) "
                    "VALUES (:client_id, 'linkedin_task_created', 'contact', :entity_id, 'sequence_sweep', :payload)"
                ),
                {
                    "client_id": order.client_id,
                    "entity_id": str(contact_id),
                    "payload": json.dumps({
                        "action_id": str(order.action_id),
                        "touch_step": touch_step,
                        "run_id": order.payload.get("run_id") if isinstance(order.payload, dict) else None,
                    }),
                },
            )
            session.commit()
    return posted is not None


def alert_stuck_dispatches(older_than_minutes: int = 30) -> int:
    """Find dispatch rows stuck in SENDING past the reclaim window and alert
    #blackink-qa. Does NOT retry them — a retry risks a double-send (ticket 22).
    Returns the number of stuck rows found. Cross-client (system session)."""
    from src.core.database import get_system_db_context
    from src.services.sequence_dispatcher import find_stuck_dispatches
    from src.services.slack.post import post_notice

    with get_system_db_context() as session:
        stuck = find_stuck_dispatches(session, older_than_minutes=older_than_minutes)

    if not stuck:
        return 0

    lines = [
        f"• dispatch_id={r.dispatch_id} client={r.client_id} run={r.run_id} "
        f"touch={r.touch_step} stuck since {r.created_at.isoformat()}"
        for r in stuck
    ]
    message = (
        f":warning: {len(stuck)} sequence touch dispatch(es) stuck in SENDING "
        f"past {older_than_minutes}m — a worker likely died mid-send. NOT auto-retried "
        f"(double-send risk); reconcile manually:\n" + "\n".join(lines)
    )
    asyncio.run(post_notice(channel_key="qa", text=message))
    logger.warning("sequence_sweep: %d stuck SENDING dispatch(es) alerted to #blackink-qa", len(stuck))
    return len(stuck)


def _winback_touch_still_ready(order) -> bool:
    """Re-check evaluate_winback_touch_gate BEFORE posting a Win-Back
    approval card — dispatch-time gating alone (winback_sequencer's own
    gate, run when a human clicks Approve) isn't enough: a reply/opt-out/
    booking landing between Touch 1 and Touch 2's due_at would otherwise
    still surface a stale card in #blackink-setter. Marks the order SKIPPED
    (not just silently un-posted) so due_batch never re-selects it and the
    decision is visible in the row's own history."""
    from sqlalchemy import text

    from src.core.database import get_db_context
    from src.services.winback_sequencer import evaluate_winback_touch_gate

    winback_row_id = int(order.payload.get("winback_row_id", order.entity_id))
    with get_db_context(client_id=order.client_id) as session:
        row = session.execute(
            text("SELECT * FROM winback_rows WHERE winback_row_id = :id"),
            {"id": winback_row_id},
        ).fetchone()
        if row is None:
            logger.error("sequence_sweep: winback_row_id=%s not found — skipping card", winback_row_id)
            wo.record_decision(order.client_id, order.action_id, decision="SKIPPED", decided_by="system:winback_gate")
            return False
        gate = evaluate_winback_touch_gate(session, row, order.client_id)

    if not gate.ready:
        wo.record_decision(order.client_id, order.action_id, decision="SKIPPED", decided_by="system:winback_gate")
        logger.info(
            "sequence_sweep: SKIPPED winback touch action_id=%s winback_row_id=%s reasons=%s",
            order.action_id, winback_row_id, gate.blocked_reasons,
        )
        return False
    return True


def _run_arm_orders(arm_orders: list) -> int:
    """Auto-execute STL_CADENCE_ARM orders — no Slack card, no human approval.
    These are system-internal: check stop state at +24h then enqueue 5 touches."""
    from src.services.stl_cadence import run_arm_check

    executed = 0
    for order in arm_orders:
        try:
            run_arm_check(order)
            executed += 1
        except Exception:
            logger.exception("sequence_sweep: STL arm_check failed action_id=%s entity=%s", order.action_id, order.entity_id)
    return executed


def run_sweep(client_id=None, limit: int = 100) -> int:
    """Fetch due QUEUED orders and post their approval cards. Returns cards posted.

    Handles email touches (1/3/5) and the day-7 LinkedIn touch (4). The dial
    touch (2) is NOT swept — it is posted event-driven on Touch 1 approval
    (docs/adr/0001-non-email-touch-posting-model.md)."""
    batch = wo.due_batch(client_id=client_id, limit=limit)

    # STL_CADENCE_ARM orders are system actions — auto-execute, no card.
    arm_orders = [o for o in batch if o.action_class in _ARM_ACTIONS]
    if arm_orders:
        executed = _run_arm_orders(arm_orders)
        logger.info("sequence_sweep: %d STL arm-check(s) executed", executed)

    # DIAL_TASK is deliberately NOT swept — it is posted event-driven on Touch 1
    # approval (docs/adr/0001-non-email-touch-posting-model.md); only email,
    # LinkedIn, winback, and STL cadence touches surface here.
    touch_orders = [o for o in batch if o.action_class in _SWEPT_ACTIONS]

    if not touch_orders:
        logger.info("sequence_sweep: no due touch orders")
        return 0

    posted = 0
    for order in touch_orders:
        if order.slack_message_ts:
            # Card already posted — skip to avoid duplicate cards.
            logger.debug("sequence_sweep: action_id=%s already has a card, skipping", order.action_id)
            continue
        if order.action_class == _WINBACK_ACTION and not _winback_touch_still_ready(order):
            # DoD requires the card never be QUEUED after a stop — not just
            # blocked when someone later clicks Approve. Skip posting and
            # mark the order terminal so due_batch never re-selects it.
            continue
        if order.action_class == _STL_TOUCH_ACTION:
            from src.services.stl_cadence import stl_cadence_touch_still_ready
            if not stl_cadence_touch_still_ready(order):
                continue
        # LINKEDIN_TASK is posted through its own poster, which re-checks the
        # per-touch compliance gate before showing the card and logs a
        # touch_skipped_compliance / linkedin_task_created event (v2 §3.1.2).
        # Everything else goes through the generic card poster.
        if order.action_class == _LINKEDIN_TASK_ACTION:
            ok = asyncio.run(_post_due_linkedin_card(order))
            if ok:
                posted += 1
                logger.info("sequence_sweep: LinkedIn card posted action_id=%s contact=%s", order.action_id, order.entity_id)
            else:
                logger.info("sequence_sweep: LinkedIn card NOT posted action_id=%s (compliance skip or Slack error)", order.action_id)
            continue
        channel_key = _ACTION_CHANNEL[order.action_class]
        ok = asyncio.run(_post_due_card(order, channel_key))
        if ok:
            posted += 1
            logger.info(
                "sequence_sweep: card posted action_id=%s contact=%s class=%s -> #%s",
                order.action_id, order.entity_id, order.action_class, channel_key,
            )
        else:
            logger.warning("sequence_sweep: card NOT posted action_id=%s — Slack error", order.action_id)

    logger.info("sequence_sweep: %d/%d cards posted", posted, len(touch_orders))
    return posted


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Post Slack approval cards for due email-touch orders")
    parser.add_argument("--client-id", default=None, help="Scope to one client (omit for cross-tenant)")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)

    posted = run_sweep(client_id=args.client_id, limit=args.limit)
    stuck = alert_stuck_dispatches()
    print(f"sequence_sweep: {posted} card(s) posted, {stuck} stuck dispatch(es) alerted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
