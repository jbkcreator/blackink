"""Respond triage worker — classify and route inbound owner-reply emails.

Reads from the respond:inbound Redis Stream, loads the inbound_messages DB
row, classifies the intent, writes the result back, then routes:

  UNSUBSCRIBE  → SUPPRESSED  + contact AND domain suppressed (S-12) + proof-ledger event + Slack #command alert
  LEGAL_GRIEF  → ESCALATED   + CLIENT relay halt  + Slack #command P0 alert
  COMPLAINT    → ROUTED      + domain suppressed  + sequence halted + #blackink-qa alert
  HOT_LEAD     → ROUTED      + context card       + sequence halted
  WHALE_OWNER  → ROUTED      + context card
  OBJECTION    → ROUTED      + context card
  QUESTION     → ROUTED      + requires_human_review=TRUE if confidence < 0.90
                             + KB-matched draft card posted to #sales-replies (Subtask 2.1.3 /
                               PR #46's knowledge_base_entries + match_kb()/post_kb_card() —
                               NOT this S-10 change; S-10's own duplicate implementation of this
                               piece was dropped when this branch was rebased onto main and the
                               collision was found)
  PARTNER      → ROUTED      + Slack #client-growth notice
  LATER        → DEFERRED    + (S-10) contact paused until an extracted target date, or
                               requires_human_review=TRUE if no date could be extracted
  NURTURE      → ROUTED      (S-10's monthly nurture stream is explicitly DEFERRED — no
                               content/enrollment exists yet; falls through unchanged)

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
from src.agents.respond.kb_cards import post_kb_card
from src.agents.respond.kb_matcher import match_kb
from src.core.database import Database
from src.services.events import log_event

logger = logging.getLogger(__name__)

CLAIM_MIN_IDLE_MS = 60_000
CLAIM_SWEEP_EVERY_N_LOOPS = 12
DB_SCAN_SWEEP_EVERY_N_LOOPS = 60   # ~1 min at 1s block_ms
DB_SCAN_MIN_AGE_SECONDS = 120       # only rows stuck for 2+ minutes
IDLE_SLEEP_SECONDS = 5

# PR #48 review finding: a sustained ANTHROPIC_API_KEY/SDK/Anthropic-API
# outage makes every queued reply hit the classifier fallback path
# (_classify_with_llm's own outer except catches ALL of those cases, not
# just a missing key at startup). Without a cooldown, this worker's
# single-consumer loop posted one synchronous Slack alert (asyncio.run +
# a blocking webhook POST) per message — serializing its own throughput
# behind Slack round-trips exactly when the queue most needs to drain
# fast, and flooding #blackink-qa with near-duplicate pings. Process-local
# state is sufficient: there is exactly one respond-worker thread per
# deployed process (see src/api/main.py's _start_background_workers).
_FALLBACK_ALERT_COOLDOWN_SECONDS = 300  # 5 minutes
_last_fallback_alert_at: Optional[datetime] = None
_fallback_suppressed_since_last_alert = 0

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
            "SELECT id, client_id, contact_id, sender_email, subject, body_text, status, received_at "
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


def _should_alert_fallback() -> tuple[bool, int]:
    """Coalesces repeated classifier-fallback alerts behind a cooldown.

    Returns (should_post_now, suppressed_count). suppressed_count is the
    number of fallback classifications that were silently suppressed since
    the last alert actually posted — surfaced in the alert text so an
    outage still reads as "N suppressed", not as a single unlabeled ping.

    requires_human_review on the DB row is set unconditionally regardless
    of this cooldown (see _route below) — only the Slack side-channel is
    throttled, so a human working the Respond queue directly still sees
    every affected row even while alerts are suppressed."""
    global _last_fallback_alert_at, _fallback_suppressed_since_last_alert
    now = datetime.now(timezone.utc)
    if (
        _last_fallback_alert_at is not None
        and (now - _last_fallback_alert_at).total_seconds() < _FALLBACK_ALERT_COOLDOWN_SECONDS
    ):
        _fallback_suppressed_since_last_alert += 1
        return False, 0

    suppressed = _fallback_suppressed_since_last_alert
    _last_fallback_alert_at = now
    _fallback_suppressed_since_last_alert = 0
    return True, suppressed


def _load_body(db: Any, db_id: int) -> str:
    """Fetch body_text for KB matching — separate query so the classifier result is already written."""
    row = db.execute(
        text("SELECT body_text FROM inbound_messages WHERE id = :id"),
        {"id": db_id},
    ).first()
    return (row[0] or "") if row else ""


def _route(
    db: Any,
    db_id: int,
    client_id: str,
    sender_email: str,
    received_at: Any,
    result: ClassificationResult,
    *,
    body_text: str = "",
    contact_id: Optional[int] = None,
) -> str:
    """Determine terminal status, write it, fire any side-effects. Returns final status."""
    if isinstance(received_at, datetime):
        received_at_dt = received_at if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
    else:
        received_at_dt = datetime.now(timezone.utc)

    final_status = _INTENT_TO_STATUS.get(result.intent, _DEFAULT_ROUTED_STATUS)
    sla_due_at   = _compute_sla(result.intent, received_at_dt) if final_status == _DEFAULT_ROUTED_STATUS else None

    # Group D / D-10: a classifier error (LLM/SDK/API-key failure) returns
    # NURTURE with meta={"path": "fallback"} — the ONLY signal distinguishing
    # it from a genuine NURTURE classification. Without this it lands ROUTED
    # with no card (NURTURE is not in CONTEXT_CARD_INTENTS), no alert, and no
    # halt — the row becomes permanently invisible: the SLA sweep requires
    # card_ts to escalate at all (respond_sla_sweep.py), which a NURTURE row
    # never gets. Flagging it for human review is what makes the row visible.
    is_fallback = result.meta.get("path") == "fallback"

    # QUESTION: run KB matcher; requires_human_review is gated on LLM classification
    # confidence (>= 0.90 means the intent is trusted enough for auto-response).
    # The KB card type (high/low/no-match) is determined separately by kb_match.
    kb_match = None
    requires_human_review = is_fallback
    if result.intent == Intent.QUESTION:
        try:
            kb_match = match_kb(db, _load_body(db, db_id))
        except Exception:
            logger.exception("respond.worker: kb match failed for db_id=%s", db_id)
        requires_human_review = result.confidence < 0.90 or is_fallback

    # S-10 — LATER: extract a target reactivation date and pause the
    # contact's outbound sequence until then. No clean date, or no known
    # contact_id (an inbound reply doesn't always resolve to one) -> flag
    # for human review rather than guessing.
    if result.intent == Intent.LATER:
        from src.services.reactivation import extract_target_date, pause_contact_until
        target_date = extract_target_date(body_text, as_of=received_at_dt)
        if target_date is not None and contact_id is not None:
            pause_contact_until(db, contact_id, target_date)
        else:
            requires_human_review = True

    # Pre-commit side-effects that must be atomic with the status write.
    if result.intent == Intent.UNSUBSCRIBE:
        from src.services.email_suppression import suppress_by_email, suppress_by_domain
        suppress_by_email(db, sender_email, reason="inbound_opt_out")
        # S-12 — an opt-out suppresses the WHOLE domain, not just the
        # sender's own address (previously only COMPLAINT did this).
        domain = sender_email.split("@")[-1].strip().lower() if "@" in sender_email else ""
        if domain:
            suppress_by_domain(db, domain, reason="inbound_opt_out")
        # S-12 — proof-ledger event. Scoped to THIS message's client_id
        # (events.client_id is NOT NULL) even though the suppression effect
        # itself is global/cross-tenant — see events.py's REQUIRED_PAYLOAD_
        # FIELDS entry for suppression_applied and email_suppression.py's
        # own docstring for why the contacts row, not events, is the
        # suppression record of truth.
        log_event(
            client_id,
            "suppression_applied",
            entity_type="inbound_message",
            entity_id=str(db_id),
            payload={
                "scope": "EMAIL_AND_DOMAIN" if domain else "EMAIL",
                "sender_email": sender_email,
                "domain": domain,
                "reason": "inbound_opt_out",
            },
            actor="respond_worker",
            session=db,
        )

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

    if is_fallback:
        # This is the only place a fallback result becomes visible to a
        # human — the SLA sweep cannot reach it (no card, escalation_level
        # stays 0). Deliberately does NOT halt the sequence: the true intent
        # is unknown, and halting on every transient LLM error would stall
        # live campaigns for no reason; a human reviewer halts it if warranted.
        #
        # PR #48 review finding: the alert itself is cooldown-throttled (see
        # _should_alert_fallback) — requires_human_review above is NOT, so a
        # sustained outage still flags every affected row for the queue, it
        # just stops flooding #blackink-qa with one post per message.
        should_alert, suppressed = _should_alert_fallback()
        if should_alert:
            suffix = (
                f" ({suppressed} more suppressed in the last "
                f"{_FALLBACK_ALERT_COOLDOWN_SECONDS // 60} min)"
                if suppressed else ""
            )
            asyncio.run(_post_slack_alert(
                "qa",
                f":warning: *Classifier error — manual review required*{suffix}\n"
                f"Client: `{client_id}` | Sender: `{sender_email}`\n"
                f"Message ID: `{db_id}` — classification failed (LLM/SDK/API-key "
                f"error); flagged for human review rather than auto-routed.",
            ))

    if result.intent == Intent.QUESTION:
        card_meta = asyncio.run(post_kb_card(
            db_id=db_id,
            client_id=client_id,
            sender_email=sender_email,
            kb_match=kb_match,
        ))
        if card_meta:
            _write_card_meta(db_id, card_meta)

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
            body_text=row.get("body_text") or "",
            contact_id=row.get("contact_id"),
        )

    # S-20 (W2 §3.2.5 Stage 5) — receipt-to-routed latency, the evidence the
    # "<60s hot-lead routing" acceptance criterion needs. Measured from
    # received_at (set at actual intake, before any queueing) to right now
    # (after _route() has already posted the #blackink-setter context card
    # for HOT_LEAD/WHALE_OWNER/OBJECTION and persisted card_posted_at) — so
    # this captures the FULL pipeline latency including any queue wait, not
    # just this function's own processing time. Written for every intent,
    # not only hot leads: a digest/Evidence-Packet reader filters to
    # intent IN (HOT_LEAD, WHALE_OWNER) to evidence the specific SLA claim,
    # but the field costs nothing to record for every row.
    received_at_for_latency = row.get("received_at") or datetime.now(timezone.utc)
    if received_at_for_latency.tzinfo is None:
        received_at_for_latency = received_at_for_latency.replace(tzinfo=timezone.utc)
    routing_latency_seconds = (datetime.now(timezone.utc) - received_at_for_latency).total_seconds()

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
                "routing_latency_seconds": routing_latency_seconds,
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


def _assert_llm_configured() -> None:
    """Refuse to start rather than silently classifying every reply as
    NURTURE/ROUTED with zero human visibility (Group D / D-10). Before this
    check, a missing anthropic SDK or an unset ANTHROPIC_API_KEY made
    classify() return the error fallback for every single message —
    indistinguishable from the LLM genuinely deciding NURTURE — and the
    entire Respond product would no-op while every status column read
    healthy. A misconfiguration must be a loud startup failure instead."""
    from src.agents.respond.classifier import anthropic as _anthropic
    from config.settings import get_settings

    if _anthropic is None:
        raise RuntimeError(
            "respond.worker: refusing to start — the 'anthropic' package is "
            "not installed. Every inbound reply would silently classify as "
            "NURTURE/ROUTED with no alert. Install the SDK before starting "
            "this worker."
        )
    if not get_settings().anthropic_api_key:
        raise RuntimeError(
            "respond.worker: refusing to start — ANTHROPIC_API_KEY is not "
            "set. Every inbound reply would silently classify as "
            "NURTURE/ROUTED with no alert. Set the key before starting this "
            "worker."
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _assert_llm_configured()
    worker = Worker()
    worker.install_signal_handlers()
    worker.run_forever()


if __name__ == "__main__":
    main()
