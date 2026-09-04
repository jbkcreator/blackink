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
import logging
import sys

sys.path.insert(0, ".")

from src.services import work_orders as wo

logger = logging.getLogger(__name__)

_EMAIL_TOUCH_ACTION = "DISPATCH_EMAIL_TOUCH"

# Every touch action class the sweep surfaces, mapped to its Slack channel key
# (config/slack_channels.py). Without DIAL_TASK/LINKEDIN_TASK here they would
# sit QUEUED forever, never shown to a human (finding #4). LinkedIn has no
# dedicated channel in the blueprint's fixed vocabulary, so it goes to the
# setter channel alongside the email cards.
_ACTION_CHANNEL = {
    "DISPATCH_EMAIL_TOUCH": "setter",
    "DIAL_TASK": "dial",
    "LINKEDIN_TASK": "setter",
}


async def _post_due_card(order, channel_key: str) -> bool:
    from src.services.slack.listeners import post_work_order_card

    posted = await post_work_order_card(order, channel_key=channel_key)
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
    """Fetch due QUEUED orders and post their approval cards. Returns cards posted."""
    batch = wo.due_batch(client_id=client_id, limit=limit)
    touch_orders = [o for o in batch if o.action_class in _ACTION_CHANNEL]

    if not touch_orders:
        logger.info("sequence_sweep: no due touch orders")
        return 0

    posted = 0
    for order in touch_orders:
        if order.slack_message_ts:
            # Card already posted — skip to avoid duplicate cards.
            logger.debug("sequence_sweep: action_id=%s already has a card, skipping", order.action_id)
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
