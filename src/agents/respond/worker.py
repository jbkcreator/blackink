"""Respond triage worker — classify and route inbound owner-reply emails.

Reads from the respond:inbound Redis Stream, loads the inbound_messages DB
row, classifies the intent, writes the result back, then routes:

  UNSUBSCRIBE  → SUPPRESSED  + contact suppressed + Slack #command alert
  LEGAL_GRIEF  → ESCALATED   + CLIENT relay halt  + Slack #command P0 alert
  COMPLAINT    → ROUTED      + domain suppressed  + sequence halted + #blackink-qa alert
  HOT_LEAD     → ROUTED      + context card       + sequence halted
  WHALE_OWNER  → ROUTED      + context card
  OBJECTION    → ROUTED      + context card
  QUESTION     → ROUTED      + requires_human_review=TRUE if confidence < 0.90
  PARTNER      → ROUTED      + Slack #client-growth notice
  LATER        → DEFERRED
  NURTURE      → ROUTED

Control flow per iteration:
  1. read_batch()      — claim one message from the stream.
  2. Load row          — fetch inbound_messages by db_id; status must be PENDING.
  3. Mark PROCESSING   — optimistic status update before the LLM call.
  4. classify()        — deterministic fast-path or single Haiku call.
  5. _route()          — write terminal/intermediate status + fire Slack if needed.
  6. log_event()       — inbound_reply_classified event.
  7. ack()             — only after the DB write commits.

Stale-message sweep runs every CLAIM_SWEEP_EVERY_N_LOOPS iterations.
SIGINT/SIGTERM: finish in-flight message, then exit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text

from src.agents.respond import queue
from src.agents.respond.classifier import ClassificationResult, classify
from src.agents.respond.context_cards import CONTEXT_CARD_INTENTS, post_context_card
from src.agents.respond.intents import Intent
from src.core.database import Database
from src.services.events import log_event

logger = logging.getLogger(__name__)

CLAIM_MIN_IDLE_MS = 60_000
CLAIM_SWEEP_EVERY_N_LOOPS = 12
DB_SCAN_SWEEP_EVERY_N_LOOPS = 60   # ~1 min at 1s block_ms
DB_SCAN_MIN_AGE_SECONDS = 120       # only rows stuck for 2+ minutes
IDLE_SLEEP_SECONDS = 5

_INTENT_TO_STATUS: Dict[Intent, str] = {
    Intent.UNSUBSCRIBE: "SUPPRESSED",
    Intent.LEGAL_GRIEF: "ESCALATED",
    Intent.LATER: "DEFERRED",
}
_DEFAULT_ROUTED_STATUS = "ROUTED"


def _consumer_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _load_row(db: Any, db_id: int) -> Optional[Dict[str, Any]]:
    row = db.execute(
        text(
            "SELECT id, client_id, sender_email, subject, body_text, status, received_at "
            "FROM inbound_messages WHERE id = :id"
        ),
        {"id": db_id},
    ).mappings().first()
    return dict(row) if row else None


def _mark_processing(db: Any, db_id: int) -> None:
    db.execute(
        text("UPDATE inbound_messages SET status = 'PROCESSING' WHERE id = :id AND status = 'PENDING'"),
        {"id": db_id},
    )


def _compute_sla(intent: Intent, received_at: datetime) -> datetime:
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    delta = timedelta(minutes=15) if intent in (Intent.HOT_LEAD, Intent.WHALE_OWNER) else timedelta(minutes=60)
    return received_at + delta


def _write_result(
    db: Any,
    db_id: int,
    result: ClassificationResult,
    final_status: str,
    sla_due_at: Optional[datetime] = None,
    requires_human_review: bool = False,
) -> None:
    db.execute(
        text(
            "UPDATE inbound_messages "
            "SET status = :status, "
            "    intent = :intent, "
            "    intent_confidence = :confidence, "
            "    classified_at = NOW(), "
            "    classification_meta = :meta ::jsonb, "
            "    sla_due_at = :sla_due_at, "
            "    requires_human_review = :human_review "
            "WHERE id = :id"
        ),
        {
            "id": db_id,
            "status": final_status,
            "intent": result.intent.value,
            "confidence": result.confidence,
            "meta": json.dumps({
                "reasoning": result.reasoning,
                "objection_subtype": result.objection_subtype,
                **result.meta,
            }),
            "sla_due_at": sla_due_at,
            "human_review": requires_human_review,
        },
    )


async def _post_slack_alert(channel_key: str, text_body: str) -> None:
    try:
        from src.services.slack.post import post_action_card

        await post_action_card(
            channel_key=channel_key,
            text=text_body,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text_body}}],
        )
    except Exception as exc:
        logger.warning("respond.worker: Slack alert failed: %s", exc)


def _halt_sequence(db: Any, sender_email: str, client_id: str) -> None:
    """Set HALTED on the contact's active sequence run (if any)."""
    db.execute(
        text(
            "UPDATE sequence_runs sr "
            "SET status = 'HALTED', updated_at = NOW() "
            "FROM contacts c "
            "WHERE c.contact_id = sr.contact_id "
            "  AND lower(c.email) = :email "
            "  AND sr.client_id = :client_id "
            "  AND sr.status = 'ACTIVE'"
        ),
        {"email": sender_email.strip().lower(), "client_id": client_id},
    )


def _route(
    db: Any,
    db_id: int,
    client_id: str,
    sender_email: str,
    received_at: Any,
    result: ClassificationResult,
) -> str:
    """Determine terminal status, write it, fire any side-effects. Returns final status."""
    if isinstance(received_at, datetime):
        received_at_dt = received_at if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
    else:
        received_at_dt = datetime.now(timezone.utc)

    final_status = _INTENT_TO_STATUS.get(result.intent, _DEFAULT_ROUTED_STATUS)
    sla_due_at   = _compute_sla(result.intent, received_at_dt) if final_status == _DEFAULT_ROUTED_STATUS else None

    # QUESTION: flag for human review when confidence is below threshold.
    requires_human_review = (
        result.intent == Intent.QUESTION and result.confidence < 0.90
    )

    # Pre-commit side-effects that must be atomic with the status write.
    if result.intent == Intent.UNSUBSCRIBE:
        from src.services.email_suppression import suppress_by_email
        suppress_by_email(db, sender_email, reason="inbound_opt_out")

    elif result.intent == Intent.COMPLAINT:
        from src.services.email_suppression import suppress_by_domain
        domain = sender_email.split("@")[-1].strip().lower() if "@" in sender_email else ""
        if domain:
            suppress_by_domain(db, domain, reason="inbound_complaint")
        _halt_sequence(db, sender_email, client_id)

    elif result.intent == Intent.HOT_LEAD:
        _halt_sequence(db, sender_email, client_id)

    # Any reply from a winback owner — regardless of intent — means a human
    # is now in the loop; Touch 2/3 must not auto-fire. winback_rows is a
    # standalone table (never joined to contacts/sequence_runs, see
    # winback_sequencer.py's docstring), so the suppress/halt calls above
    # can't reach it. stop_active_winback_runs() is a no-op for a sender who
    # isn't in winback_rows for this client, so this is safe to call for
    # every reply, not just ones from known winback owners.
    from src.services.winback_sequencer import stop_active_winback_runs
    stop_reason = "OPT_OUT" if result.intent == Intent.UNSUBSCRIBE else "REPLY"
    stop_active_winback_runs(db, client_id, sender_email, stop_reason)

    _write_result(db, db_id, result, final_status, sla_due_at=sla_due_at,
                  requires_human_review=requires_human_review)
    db.commit()

    # Post-commit Slack side-effects (non-transactional).
    if result.intent == Intent.UNSUBSCRIBE:
        asyncio.run(_post_slack_alert(
            "command",
            f":no_entry: *Opt-out received*\n"
            f"Client: `{client_id}` | Sender: `{sender_email}`\n"
            f"Message ID: `{db_id}` — marked SUPPRESSED, contact suppressed.",
        ))

    elif result.intent == Intent.LEGAL_GRIEF:
        from src.agents.relay.halt_service import issue_halt
        try:
            issue_halt(
                scope="CLIENT",
                scope_id=client_id,
                reason=f"Legal threat from {sender_email} — inbound_message id={db_id}",
                issued_by="respond_worker",
            )
        except Exception:
            logger.exception("respond.worker: relay halt failed for LEGAL_GRIEF db_id=%s", db_id)
        asyncio.run(_post_slack_alert(
            "command",
            f":rotating_light: *LEGAL THREAT — immediate review required*\n"
            f"Client: `{client_id}` | Sender: `{sender_email}`\n"
            f"Message ID: `{db_id}` — CLIENT halt issued. All outreach halted.",
        ))

    elif result.intent == Intent.COMPLAINT:
        asyncio.run(_post_slack_alert(
            "qa",
            f":loudspeaker: *Complaint received*\n"
            f"Client: `{client_id}` | Sender: `{sender_email}`\n"
            f"Message ID: `{db_id}` — domain suppressed, sequence halted.",
        ))

    elif result.intent == Intent.PARTNER:
        asyncio.run(_post_slack_alert(
            "client-growth",
            f":handshake: *Partner inquiry*\n"
            f"Client: `{client_id}` | Sender: `{sender_email}`\n"
            f"Message ID: `{db_id}` — route to Referral Agent.",
        ))

    elif result.intent in CONTEXT_CARD_INTENTS and sla_due_at:
        card_meta = asyncio.run(post_context_card(
            db_id=db_id,
            client_id=client_id,
            sender_email=sender_email,
            received_at=received_at_dt,
            intent=result.intent,
            result=result,
            sla_due_at=sla_due_at,
        ))
        if card_meta:
            _write_card_meta(db_id, card_meta)

    return final_status


def _write_card_meta(db_id: int, card_meta: dict) -> None:
    """Persist Slack card identifiers back to the row after a successful post."""
    db_obj = Database()
    with db_obj.system_session_scope() as db:
        db.execute(
            text(
                "UPDATE inbound_messages "
                "SET card_ts = :ts, card_channel_id = :channel, card_posted_at = :posted_at "
                "WHERE id = :id"
            ),
            {
                "id": db_id,
                "ts": card_meta["card_ts"],
                "channel": card_meta["card_channel_id"],
                "posted_at": card_meta["card_posted_at_iso"],
            },
        )
        db.commit()


def _process_message(msg: queue.InboundQueueMessage) -> None:
    db_obj = Database()
    with db_obj.system_session_scope() as db:
        row = _load_row(db, msg.db_id)
        if row is None:
            logger.warning(
                "respond.worker: db_id=%s not found — acking to clear queue",
                msg.db_id,
            )
            queue.ack(msg.message_id)
            return

        if row["status"] not in ("PENDING", "PROCESSING"):
            # Already handled by a previous delivery — idempotent ack.
            logger.info(
                "respond.worker: db_id=%s already in status=%s — skipping",
                msg.db_id, row["status"],
            )
            queue.ack(msg.message_id)
            return

        _mark_processing(db, msg.db_id)
        db.commit()

    # Classification runs outside the DB transaction — LLM call has its own latency.
    result = classify(
        body_text=row.get("body_text") or "",
        subject=row.get("subject") or "",
        sender_email=row.get("sender_email") or "",
    )

    with db_obj.system_session_scope() as db:
        final_status = _route(
            db=db,
            db_id=msg.db_id,
            client_id=row["client_id"],
            sender_email=row.get("sender_email") or "",
            received_at=row.get("received_at") or datetime.now(timezone.utc),
            result=result,
        )

    try:
        log_event(
            row["client_id"],
            "inbound_reply_classified",
            entity_type="inbound_message",
            entity_id=str(msg.db_id),
            payload={
                "detected_intent":   result.intent.value,
                "confidence_score":  result.confidence,
                "final_status":      final_status,
                "objection_subtype": result.objection_subtype,
                "path":              result.meta.get("path", "llm"),
            },
            actor="respond_worker",
        )
    except Exception:
        logger.exception("respond.worker: log_event failed for db_id=%s", msg.db_id)

    logger.info(
        "respond.worker: db_id=%s client_id=%s intent=%s confidence=%.2f status=%s",
        msg.db_id, row["client_id"], result.intent.value, result.confidence, final_status,
    )
    queue.ack(msg.message_id)


def _sweep_unpublished_pending() -> None:
    """Re-publish PENDING rows that never made it into the Redis stream.

    Targets rows older than DB_SCAN_MIN_AGE_SECONDS whose publish failed
    silently (e.g. Redis was down at intake time). Safe to run concurrently —
    duplicate publishes are deduplicated by the worker's idempotency check on
    the DB row status.
    """
    db_obj = Database()
    with db_obj.system_session_scope() as db:
        rows = db.execute(
            text(
                f"SELECT id, client_id, idempotency_key FROM inbound_messages "
                f"WHERE status = 'PENDING' "
                f"AND received_at < NOW() - INTERVAL '{DB_SCAN_MIN_AGE_SECONDS} seconds'"
            )
        ).mappings().fetchall()

    if not rows:
        return

    logger.info(
        "respond.worker: db-scan found %d unpublished PENDING row(s) — re-publishing",
        len(rows),
    )
    for row in rows:
        stream_id = queue.publish(
            db_id=row["id"],
            client_id=row["client_id"],
            idempotency_key=row["idempotency_key"],
        )
        if stream_id:
            logger.info(
                "respond.worker: re-published db_id=%s stream_id=%s",
                row["id"], stream_id,
            )
        else:
            logger.warning(
                "respond.worker: re-publish failed for db_id=%s — Redis still down?",
                row["id"],
            )


class Worker:
    def __init__(self, consumer_name: Optional[str] = None) -> None:
        self.consumer_name = consumer_name or _consumer_name()
        self._stop = False
        self._loop_count = 0
        self._group_confirmed = False

    def request_stop(self, *_args: Any) -> None:
        logger.info(
            "respond.worker: shutdown requested (consumer=%s) — finishing in-flight work",
            self.consumer_name,
        )
        self._stop = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    def _sweep_stale(self) -> None:
        reclaimed = queue.claim_stale(self.consumer_name, min_idle_ms=CLAIM_MIN_IDLE_MS)
        for msg in reclaimed:
            logger.info(
                "respond.worker: reclaimed stale message_id=%s db_id=%s delivery_count=%d",
                msg.message_id, msg.db_id, msg.delivery_count,
            )
            try:
                _process_message(msg)
            except Exception:
                logger.exception(
                    "respond.worker: stale message processing failed db_id=%s", msg.db_id
                )

    def run_forever(self, block_ms: int = 1000) -> None:
        logger.info("respond.worker: starting (consumer=%s)", self.consumer_name)

        while not self._stop:
            self._loop_count += 1
            try:
                # Lazy group creation: retry on every iteration until Redis is
                # reachable. Prevents a startup-time outage from killing the
                # thread permanently. _group_confirmed is reset to False if any
                # loop exception fires, so a mid-run Redis bounce is also handled.
                if not self._group_confirmed:
                    queue.ensure_group()
                    self._group_confirmed = True

                if self._loop_count % CLAIM_SWEEP_EVERY_N_LOOPS == 0:
                    self._sweep_stale()

                if self._loop_count % DB_SCAN_SWEEP_EVERY_N_LOOPS == 0:
                    try:
                        _sweep_unpublished_pending()
                    except Exception:
                        logger.exception("respond.worker: db-scan sweep failed")

                messages = queue.read_batch(self.consumer_name, count=1, block_ms=block_ms)
                for msg in messages:
                    if self._stop:
                        break
                    try:
                        _process_message(msg)
                    except Exception:
                        logger.exception(
                            "respond.worker: _process_message raised for db_id=%s — leaving unacked",
                            msg.db_id,
                        )
            except Exception:
                logger.exception("respond.worker: main loop iteration failed — continuing")
                self._group_confirmed = False
                time.sleep(IDLE_SLEEP_SECONDS)

        logger.info("respond.worker: stopped (consumer=%s)", self.consumer_name)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    worker = Worker()
    worker.install_signal_handlers()
    worker.run_forever()


if __name__ == "__main__":
    main()
