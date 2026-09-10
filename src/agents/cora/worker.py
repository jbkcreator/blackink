"""Cora draft-generation worker loop.

Reads draft.requested events from the Redis Stream, checks halt/throttle
state before claiming work, and hands each event to _process_draft().

Control flow per iteration:
  1. cora_should_stop(client_id) — Relay halt or approval-backlog auto-pause:
     idle, do NOT claim new events, loop back.
  2. _maybe_resume() — check if auto-pause can lift (backlog drained) even
     while idle. Called on every idle tick so the worker self-heals as soon as
     Slack approvals clear the backlog, without waiting for an external signal.
  3. read_batch() — claim one event from the stream.
  4. _process_draft() — generate draft, post to Slack, notify_draft_queued().
  5. ack() — only after Slack post succeeds (durable side effect happened).

Stale-message sweep runs every CLAIM_SWEEP_EVERY_N_LOOPS iterations and
reclaims events that were claimed but never acked (worker crash mid-flight).

SIGINT/SIGTERM trigger graceful shutdown: stop claiming new events, let the
in-flight message finish, then exit. An unacked in-flight message is picked up
by claim_stale() on the next worker restart.

_process_draft() is intentionally minimal for week 0 — the actual LLM draft
generation and Slack posting belong to the outreach-pipeline workstream (not
Dev 2 scope). It logs the event and calls notify_draft_queued() as a
placeholder, which is enough to exercise the throttle path end-to-end.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid
from typing import Any, Dict, Optional

from src.agents.cora import kill_switch, queue
from src.agents.cora.throttle import _maybe_resume, notify_draft_queued, approval_pending_count

logger = logging.getLogger(__name__)

CLAIM_MIN_IDLE_MS = 60_000
CLAIM_SWEEP_EVERY_N_LOOPS = 12
IDLE_SLEEP_SECONDS = 5


def _consumer_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _process_draft(msg: "queue.DraftMessage") -> None:
    """Generate a 5-touch email sequence via Claude, store it, and post a Slack
    approval card to #blackink-command.

    Flow:
      1. Fetch company + contact + client metadata from DB.
      2. Call draft_composer.compose() (Claude API, falls back to hardcoded sequence).
      3. Store steps in Redis via draft_store.put().
      4. Post Slack card with Touch 1 preview + Approve/Reject buttons.
      5. Increment Cora's approval backlog counter (throttle integration).
    """
    import json as _json

    from sqlalchemy import text

    from src.agents.cora import draft_store, draft_composer
    from src.agents.ink.subagents.pdf_generator.pdf_report import _fmt_latency
    from src.core.database import get_db_context, get_system_db_context
    from config.settings import get_settings

    payload       = msg.payload
    work_order_id = payload.get("work_order_id", msg.message_id)
    company_id    = payload.get("company_id", "")
    client_id     = msg.client_id

    logger.info(
        "cora.worker: processing draft work_order_id=%s company_id=%s client_id=%s",
        work_order_id, company_id, client_id,
    )

    # 1. Fetch company + primary contact + client display name
    company_name = company_id
    city         = "your market"
    door_count   = "unknown"
    first_name   = "there"
    contact_name = "there"

    try:
        with get_system_db_context() as db:
            co_row = db.execute(
                text(
                    "SELECT c.company_name, co.county_name, c.door_count_est, "
                    "       ct.first_name, ct.last_name "
                    "FROM   companies c "
                    "LEFT JOIN counties co ON co.county_slug = c.county_slug "
                    "LEFT JOIN contacts ct ON ct.company_id = c.company_id "
                    "   AND ct.contact_role_type IN ('DECISION_MAKER','OWNER_BROKER_MD') "
                    "WHERE  c.company_id = :cid "
                    "LIMIT 1"
                ),
                {"cid": company_id},
            ).fetchone()
        if co_row:
            company_name = co_row.company_name or company_id
            city         = co_row.county_name  or "your market"
            door_count   = str(co_row.door_count_est or "unknown")
            first_name   = co_row.first_name   or "there"
            contact_name = f"{co_row.first_name or ''} {co_row.last_name or ''}".strip() or "there"
    except Exception as exc:
        logger.warning("cora.worker: DB lookup failed for company_id=%s: %s", company_id, exc)

    client_display_name = client_id
    try:
        with get_system_db_context() as db:
            cl_row = db.execute(
                text("SELECT display_name FROM clients WHERE client_id = :cid"),
                {"cid": client_id},
            ).fetchone()
        if cl_row:
            client_display_name = cl_row.display_name
    except Exception as exc:
        logger.warning("cora.worker: client lookup failed client_id=%s: %s", client_id, exc)

    # Format audit data for the composer
    latency_sec  = payload.get("latency_sec")
    loss_est     = payload.get("loss_est") or 0
    audit_speed  = _fmt_latency(latency_sec)
    loss_dollars = f"${loss_est:,}" if loss_est else "$0"

    # 2. Generate sequence
    try:
        steps = draft_composer.compose(
            company_name=company_name,
            contact_name=contact_name,
            city=city,
            door_count=door_count,
            audit_speed=audit_speed,
            loss_dollars=loss_dollars,
        )
    except draft_composer.CompositionError as exc:
        logger.error("cora.worker: draft composition failed work_order_id=%s: %s", work_order_id, exc)
        raise

    # 3. Store in Redis
    draft_store.put(work_order_id, steps)

    # 4. Post Slack card
    settings = get_settings()
    channel = settings.blackink_command_slack_channel
    if channel:
        try:
            from slack_sdk import WebClient
            slack_client = WebClient(token=settings.slack_bot_token.get_secret_value() if settings.slack_bot_token else "")
            touch1   = steps[0]
            preview_body = (touch1["body"]
                .replace("{first_name}",   first_name)
                .replace("{client_firm}",  client_display_name)
                .replace("{company}",      company_name)
                .replace("{audit_speed}",  audit_speed)
                .replace("{loss_dollars}", loss_dollars)
                .replace("{video_url}",    "[video link]")
            )
            preview_body = preview_body[:600] + ("..." if len(preview_body) > 600 else "")
            btn_value = _json.dumps({"work_order_id": work_order_id, "campaign_id": payload.get("campaign_id", "")})
            blocks = [
                {"type": "header", "text": {"type": "plain_text", "text": f"Campaign Draft — {company_name}"}},
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Response time:*\n{audit_speed}"},
                        {"type": "mrkdwn", "text": f"*Est. revenue at risk:*\n{loss_dollars}"},
                        {"type": "mrkdwn", "text": f"*Touches:*\n{len(steps)}"},
                        {"type": "mrkdwn", "text": f"*Touch 1 subject:*\n{touch1['subject']}"},
                    ],
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Touch 1 preview:*\n```{preview_body}```"}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve Campaign"},
                            "style": "primary",
                            "action_id": "approve_ink_campaign",
                            "value": btn_value,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Reject Campaign"},
                            "style": "danger",
                            "action_id": "reject_ink_campaign",
                            "value": btn_value,
                        },
                    ],
                },
            ]
            slack_client.chat_postMessage(channel=channel, blocks=blocks, text=f"Campaign draft ready — {company_name}")
            logger.info("cora.worker: Slack card posted work_order_id=%s channel=%s", work_order_id, channel)
        except Exception as exc:
            logger.error("cora.worker: Slack post failed work_order_id=%s: %s", work_order_id, exc)
            # Don't re-raise — the draft IS stored; the human can still approve via HTTP webhook
    else:
        logger.warning(
            "cora.worker: BLACKINK_COMMAND_SLACK_CHANNEL unset — draft stored but no card posted "
            "work_order_id=%s", work_order_id,
        )

    # 5. Backlog counter
    count = notify_draft_queued(work_order_id)
    logger.info("cora.worker: approval_pending=%d after queuing work_order_id=%s", count, work_order_id)


class Worker:
    def __init__(self, consumer_name: Optional[str] = None) -> None:
        self.consumer_name = consumer_name or _consumer_name()
        self._stop = False
        self._loop_count = 0

    def request_stop(self, *_args: Any) -> None:
        logger.info(
            "cora.worker: shutdown requested (consumer=%s) — finishing in-flight work",
            self.consumer_name,
        )
        self._stop = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    def _process_one(self, msg: "queue.DraftMessage") -> None:
        # Per-message CLIENT halt check — the pre-loop check in run_forever()
        # only evaluates GLOBAL halts because no message has been claimed yet.
        # Once we hold a message we know its client, so re-check here before
        # doing any work. Leave unacked so the message is recoverable via
        # claim_stale() after the halt is lifted.
        if kill_switch.cora_should_stop(client_id=msg.client_id):
            logger.warning(
                "cora.worker: CLIENT halt active for client_id=%s — "
                "leaving message_id=%s unacked for recovery",
                msg.client_id, msg.message_id,
            )
            return
        try:
            _process_draft(msg)
            queue.ack(msg.message_id)
        except Exception:
            logger.exception(
                "cora.worker: _process_draft raised for message_id=%s event_type=%s "
                "delivery_count=%d — leaving unacked for claim_stale()",
                msg.message_id, msg.event_type, msg.delivery_count,
            )

    def _sweep_stale(self) -> None:
        reclaimed = queue.claim_stale(self.consumer_name, min_idle_ms=CLAIM_MIN_IDLE_MS)
        for msg in reclaimed:
            logger.info(
                "cora.worker: reclaimed stale message_id=%s event_type=%s delivery_count=%d",
                msg.message_id, msg.event_type, msg.delivery_count,
            )
            self._process_one(msg)

    def run_forever(self, block_ms: int = 1000) -> None:
        queue.ensure_group()
        logger.info("cora.worker: starting (consumer=%s)", self.consumer_name)

        while not self._stop:
            self._loop_count += 1

            # Check halt / throttle before claiming any work.
            if kill_switch.cora_should_stop():
                # While idling, still check for throttle self-resume so the
                # worker wakes up as soon as the backlog clears without needing
                # an explicit external signal.
                _maybe_resume(count=approval_pending_count())
                time.sleep(IDLE_SLEEP_SECONDS)
                continue

            try:
                if self._loop_count % CLAIM_SWEEP_EVERY_N_LOOPS == 0:
                    self._sweep_stale()

                messages = queue.read_batch(
                    self.consumer_name, count=1, block_ms=block_ms
                )
                for msg in messages:
                    if self._stop:
                        break
                    self._process_one(msg)

            except Exception:
                logger.exception("cora.worker: main loop iteration failed — continuing")

        logger.info("cora.worker: stopped (consumer=%s)", self.consumer_name)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    worker = Worker()
    worker.install_signal_handlers()
    worker.run_forever()


if __name__ == "__main__":
    main()
