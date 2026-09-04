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

sys.path.insert(0, ".")

from src.services import work_orders as wo

logger = logging.getLogger(__name__)

_EMAIL_TOUCH_ACTION = "DISPATCH_EMAIL_TOUCH"
_LINKEDIN_TASK_ACTION = "LINKEDIN_TASK"


async def _post_due_card(order) -> bool:
    from src.services.slack.listeners import post_work_order_card

    posted = await post_work_order_card(order, channel_key="setter")
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
            logger.info(
                "sequence_sweep: LINKEDIN_TASK compliance block contact_id=%s reasons=%s — skipping",
                contact_id, gate.blocked_reasons,
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
                    }),
                },
            )
            session.commit()
            return False

    posted = await post_work_order_card(order, channel_key="setter")
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


def run_sweep(client_id=None, limit: int = 100) -> int:
    """Fetch due QUEUED orders and post their approval cards. Returns cards posted.

    Handles email touches (1/3/5) and the day-7 LinkedIn touch (4). The dial
    touch (2) is NOT swept — it is posted event-driven on Touch 1 approval
    (docs/adr/0001-non-email-touch-posting-model.md)."""
    batch = wo.due_batch(client_id=client_id, limit=limit)
    email_orders = [o for o in batch if o.action_class == _EMAIL_TOUCH_ACTION]
    linkedin_orders = [o for o in batch if o.action_class == _LINKEDIN_TASK_ACTION]

    if not email_orders and not linkedin_orders:
        logger.info("sequence_sweep: no due email-touch or LinkedIn orders")
        return 0

    posted = 0
    for order in email_orders:
        if order.slack_message_ts:
            # Card already posted — skip to avoid duplicate cards.
            logger.debug("sequence_sweep: action_id=%s already has a card, skipping", order.action_id)
            continue
        ok = asyncio.run(_post_due_card(order))
        if ok:
            posted += 1
            logger.info("sequence_sweep: card posted action_id=%s contact=%s", order.action_id, order.entity_id)
        else:
            logger.warning("sequence_sweep: card NOT posted action_id=%s — Slack error", order.action_id)

    for order in linkedin_orders:
        if order.slack_message_ts:
            logger.debug("sequence_sweep: LINKEDIN action_id=%s already has a card, skipping", order.action_id)
            continue
        ok = asyncio.run(_post_due_linkedin_card(order))
        if ok:
            posted += 1
            logger.info("sequence_sweep: LinkedIn card posted action_id=%s contact=%s", order.action_id, order.entity_id)
        else:
            logger.info("sequence_sweep: LinkedIn card NOT posted action_id=%s (compliance skip or Slack error)", order.action_id)

    total = len(email_orders) + len(linkedin_orders)
    logger.info("sequence_sweep: %d/%d cards posted", posted, total)
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
