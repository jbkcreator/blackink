"""Six-Attempt Speed-to-Lead Cadence — Task 4.2.2.

After the initial 4.2.1 auto-response, if the lead goes quiet for 24h,
five daily follow-up touches (Day 1–5) are dispatched as human-approved
work orders (v2 blueprint: approval required per send in early client phase).

Key design decisions (grilled 2026-09-08; see SPEC-4.2.2.md):
  - Message-anchored: keyed on inbound_messages.id, NO contact_id (STL
    leads are renters/owners, not PM-firm prospects — contacts row absent).
  - Stop state: latch (cadence_state/cadence_stopped_at/cadence_stop_reason
    on inbound_messages) is the sweep's authoritative gate; event log is the
    audit trail + arm-check reconciliation backstop.
  - GHL-arming: OUT OF SCOPE (v2 = booking-only; no outbound GHL client).
  - Human approval per touch: v2 mandates it for early-phase clients (every
    client at Sep pilot); touches flow through sequence_sweep approval cards.
  - At-most-once: stl_cadence_dispatches (UNIQUE message_id, touch_step).
  - Post-send failure guard: same PostSendError discipline as
    speed_to_lead_sweep.py — a write failure after SMTP must NOT re-send.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.database import get_db_context, get_system_db_context
from src.services import work_orders as wo
from src.services.events import log_event

logger = logging.getLogger(__name__)

_AGENT_ID = "stl_cadence"
_ARM_ACTION = "STL_CADENCE_ARM"
_TOUCH_ACTION = "DISPATCH_STL_CADENCE_TOUCH"
_TOUCH_DAY_OFFSETS = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}   # Day N after receipt


# ── Arm ───────────────────────────────────────────────────────────────────────

def arm_cadence(
    session: Session,
    client_id: str,
    message_id: str,
    received_at: datetime,
) -> None:
    """Enqueue a deferred arm-check work order at received_at + 24h.

    Called from inbound_lead_orchestrator immediately after writing an
    inbound_messages row whose outcome is 'written' (not duplicate/suppressed).
    Uses the same session so the enqueue shares the pipeline's transaction.

    Idempotent: wo.enqueue returns the existing row on (client_id,
    idempotency_key) conflict, so a duplicate webhook never arms twice."""
    due_at = received_at + timedelta(hours=24)
    wo.enqueue(
        client_id=client_id,
        entity_type="inbound_message",
        entity_id=message_id,
        agent_id=_AGENT_ID,
        action_class=_ARM_ACTION,
        autonomy_band="BAND_0_AUTONOMOUS",   # arm check is a system action, not human-gated
        risk_class="LOW",
        payload={"message_id": message_id},
        config_fingerprint={"channel": "stl_cadence"},
        idempotency_key=f"stl-cadence-arm:{message_id}",
        due_at=due_at,
    )
    logger.info("[stl_cadence] arm-check work order enqueued for message_id=%s due=%s", message_id, due_at.isoformat())


# ── Arm check (sequence_sweep calls this when STL_CADENCE_ARM order is due) ──

def run_arm_check(order) -> None:
    """Re-check stop state at +24h; enqueue 5 touch orders if still active.

    Called by sequence_sweep auto-execution (no Slack card) when the arm-check
    work order comes due. Marks the arm order DONE regardless of outcome so
    due_batch never re-selects it."""
    message_id = order.entity_id
    client_id = order.client_id

    with get_db_context(client_id=client_id) as session:
        msg = session.execute(
            text("SELECT id, received_at, sender_email, sender_name, cadence_state "
                 "FROM inbound_messages WHERE id = :id"),
            {"id": int(message_id)},
        ).fetchone()

        if msg is None:
            logger.warning("[stl_cadence] arm_check: message_id=%s not found — skipping", message_id)
            wo.record_decision(client_id, order.action_id, decision="SKIPPED", decided_by="stl_cadence:not_found")
            return

        # 1. Stop latch check (fast, indexed).
        if msg.cadence_state == "STOPPED":
            logger.info("[stl_cadence] arm_check: message_id=%s already STOPPED — no-op", message_id)
            wo.record_decision(client_id, order.action_id, decision="SKIPPED", decided_by="stl_cadence:already_stopped")
            return
        if msg.cadence_state in ("ARMED", "COMPLETED"):
            logger.info("[stl_cadence] arm_check: message_id=%s state=%s — already armed/done", message_id, msg.cadence_state)
            wo.record_decision(client_id, order.action_id, decision="DONE", decided_by="stl_cadence:already_armed")
            return

        # 2. Event ledger reconciliation scan — belt to the stop-latch suspenders.
        # Catches any stop event that fired before the stop-writer ran.
        stop_event = session.execute(
            text(
                "SELECT event_type FROM events "
                "WHERE client_id = :client_id "
                "  AND entity_type = 'inbound_message' "
                "  AND entity_id = :entity_id "
                "  AND event_type IN ('inbound_reply_received', 'meeting_booked', "
                "                     'speed_to_lead_opted_out') "
                "LIMIT 1"
            ),
            {"client_id": client_id, "entity_id": message_id},
        ).fetchone()
        if stop_event:
            logger.info(
                "[stl_cadence] arm_check: stop event %s found via ledger scan — latching STOPPED message_id=%s",
                stop_event.event_type, message_id,
            )
            _latch_stop(session, int(message_id), _stop_reason_from_event(stop_event.event_type))
            wo.record_decision(client_id, order.action_id, decision="SKIPPED", decided_by="stl_cadence:stop_event")
            return

        # 3. Arm: set cadence_state=ARMED and enqueue 5 touch work orders.
        received_at = msg.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)

        session.execute(
            text("UPDATE inbound_messages SET cadence_state = 'ARMED' WHERE id = :id"),
            {"id": int(message_id)},
        )

        for step, day_offset in _TOUCH_DAY_OFFSETS.items():
            touch_due = received_at + timedelta(days=day_offset)
            template = _default_touch_template(step)
            wo.enqueue(
                client_id=client_id,
                entity_type="inbound_message",
                entity_id=message_id,
                agent_id=_AGENT_ID,
                action_class=_TOUCH_ACTION,
                autonomy_band="BAND_2_ONE_TAP",   # v2: human approval required
                risk_class="LOW",
                payload={
                    "message_id": message_id,
                    "touch_step": step,
                    "prospect_name": msg.sender_name,
                    "prospect_email": msg.sender_email,
                    "subject": template["subject"],
                    "body": template["body"],
                    "template_version": f"stl-cadence-touch-{step}-v1",
                    "campaign_type": "SPEED_TO_LEAD_CADENCE",
                },
                config_fingerprint={"channel": "stl_cadence"},
                idempotency_key=f"stl-cadence:{message_id}:touch:{step}",
                due_at=touch_due,
            )
            logger.info(
                "[stl_cadence] arm_check: touch %d enqueued message_id=%s due=%s",
                step, message_id, touch_due.isoformat(),
            )

        log_event(
            client_id,
            "stl_cadence_armed",
            entity_type="inbound_message",
            entity_id=message_id,
            payload={"message_id": message_id, "touch_count": 5},
            session=session,
        )
        session.commit()

    wo.record_decision(client_id, order.action_id, decision="DONE", decided_by="stl_cadence:armed")
    logger.info("[stl_cadence] arm_check: ARMED 5 touches for message_id=%s", message_id)


# ── Touch dispatch (called from dispatchers.py on human approval) ─────────────

class _PostSendError(Exception):
    """Post-SMTP write failure — must NOT be retried (resend risk)."""


def dispatch_stl_cadence_touch(order) -> dict:
    """DISPATCH_STL_CADENCE_TOUCH: pre-send gate, claim, send, log.

    Returns a dispatch receipt dict. Follows the PostSendError discipline:
    if SMTP succeeds but the post-send write fails, the dispatch row is marked
    SENT_UNCONFIRMED (terminal) — never reset so the message is never resent.
    """
    from src.services.booking_link import resolve_owner_booking_link
    from src.services.email_sender import build_email_sender
    from src.services.email_unsubscribe import mint_unsubscribe_token, unsubscribe_url
    from src.services.mailbox_dispatcher import (
        AllMailboxesCapped, NoMailboxAvailable,
        get_active_mailbox_for_client,
    )

    payload = order.payload if isinstance(order.payload, dict) else {}
    message_id_str = payload.get("message_id", order.entity_id)
    message_id = int(message_id_str)
    touch_step = int(payload.get("touch_step", 0))
    prospect_email = payload.get("prospect_email", "")
    prospect_name = payload.get("prospect_name") or "there"
    subject = payload.get("subject") or _default_touch_template(touch_step)["subject"]
    body_template = payload.get("body") or _default_touch_template(touch_step)["body"]
    client_id = order.client_id

    with get_db_context(client_id=client_id) as session:
        # ── Pre-send gate: stop-latch re-check ────────────────────────────────
        msg = session.execute(
            text("SELECT cadence_state FROM inbound_messages WHERE id = :id"),
            {"id": message_id},
        ).fetchone()
        if msg is None:
            logger.error("[stl_cadence] touch %d: message_id=%s not found", touch_step, message_id_str)
            return {"outcome": "MESSAGE_NOT_FOUND", "message_id": message_id_str, "fail": True}

        if msg.cadence_state == "STOPPED":
            logger.info("[stl_cadence] touch %d: message_id=%s cadence STOPPED — skipping send", touch_step, message_id_str)
            return {"outcome": "CADENCE_STOPPED", "message_id": message_id_str}

        if not prospect_email:
            logger.warning("[stl_cadence] touch %d: message_id=%s no email — skipping", touch_step, message_id_str)
            _claim_dispatch(session, message_id, touch_step, client_id, status="SENT")   # no send = treat as done
            session.commit()
            return {"outcome": "NO_EMAIL", "message_id": message_id_str}

        # ── Mailbox pick FIRST (before the at-most-once claim) ────────────────
        # A capped/quarantined mailbox must DEFER and be retried later, so we
        # pick the mailbox before writing any claim row. If we claimed first
        # and then found no mailbox, the ON CONFLICT guard would make every
        # retry return ALREADY_CLAIMED and the touch would never send.
        try:
            mailbox = get_active_mailbox_for_client(session, client_id)
        except (NoMailboxAvailable, AllMailboxesCapped) as exc:
            logger.warning("[stl_cadence] touch %d: no mailbox client=%s: %s", touch_step, client_id, exc)
            session.rollback()   # drop the last_used bump; nothing to persist on a defer
            return {"outcome": "NO_MAILBOX", "message_id": message_id_str, "defer": True}

        # ── At-most-once claim, COMMITTED BEFORE the SMTP send ────────────────
        # INSERT … ON CONFLICT DO NOTHING. Committing the SENDING row (together
        # with the mailbox last_used bump) BEFORE the network send is what makes
        # the at-most-once guard durable: if a post-send write later fails and
        # rolls its transaction back, this claim survives, so no retry can ever
        # re-send the same touch (it would hit ON CONFLICT → ALREADY_CLAIMED).
        claimed = _claim_dispatch(session, message_id, touch_step, client_id, status="SENDING")
        if not claimed:
            logger.info("[stl_cadence] touch %d: message_id=%s already claimed — skip", touch_step, message_id_str)
            session.rollback()
            return {"outcome": "ALREADY_CLAIMED", "message_id": message_id_str}
        session.commit()

        # ── Build email ────────────────────────────────────────────────────────
        booking = resolve_owner_booking_link(session, client_id, name=prospect_name, email=prospect_email)
        booking_url = booking.url if booking else None

        unsub_url = unsubscribe_url(client_id, prospect_email)
        # Pass unsub_url so the {{unsubscribe}} token renders a VISIBLE in-body
        # link — CLAUDE.md requires both the List-Unsubscribe header (below) AND
        # a visible body link on every outbound send.
        html_body = _render_touch_html(body_template, prospect_name, booking_url, unsub_url)
        text_body = _html_to_text(html_body)

        sender = build_email_sender()
        now = datetime.now(timezone.utc)

        # ── SMTP send ──────────────────────────────────────────────────────────
        sender.send(
            from_address=mailbox.mailbox_address,
            to_address=prospect_email,
            subject=subject,
            body=text_body,
            html_body=html_body,
            sending_domain=mailbox.sending_domain,
            list_unsubscribe_url=unsub_url,
        )

        # SMTP succeeded — post-send writes must never resend on failure. The
        # claim is already durably SENDING (committed above); if these writes
        # fail we flip that row to the terminal SENT_UNCONFIRMED in an
        # independent transaction and raise _PostSendError for manual
        # reconciliation — never a reset that would let the touch resend.
        try:
            _update_dispatch_status(session, message_id, touch_step, "SENT",
                                    mailbox_id=mailbox.mailbox_id, sent_at=now)
            if touch_step == 5:
                session.execute(
                    text("UPDATE inbound_messages SET cadence_state = 'COMPLETED' WHERE id = :id"),
                    {"id": message_id},
                )
            log_event(
                client_id,
                "outbound_touch_dispatched",
                entity_type="inbound_message",
                entity_id=message_id_str,
                payload={
                    "campaign_type": "SPEED_TO_LEAD_CADENCE",
                    "touch_step": touch_step,
                    "message_id": message_id_str,
                    "mailbox_id": mailbox.mailbox_id,
                    "sending_domain": mailbox.sending_domain,
                    "recipient_email": prospect_email,
                    "template_version": payload.get("template_version", ""),
                },
                session=session,
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            _mark_sent_unconfirmed(client_id, message_id, touch_step, mailbox.mailbox_id, now)
            raise _PostSendError(str(exc)) from exc

    return {
        "outcome": "SENT",
        "message_id": message_id_str,
        "touch_step": touch_step,
        "mailbox_id": mailbox.mailbox_id,
    }


def _mark_sent_unconfirmed(
    client_id: str,
    message_id: int,
    touch_step: int,
    mailbox_id: int,
    sent_at: datetime,
) -> None:
    """Flip a dispatch to the terminal SENT_UNCONFIRMED in its OWN transaction.

    Called only after SMTP has already succeeded but a post-send write failed.
    A separate get_db_context so this survives even when the caller's
    transaction is poisoned/rolled back — the durable proof the touch was sent,
    so no retry ever resends it."""
    try:
        with get_db_context(client_id=client_id) as session:
            _update_dispatch_status(session, message_id, touch_step, "SENT_UNCONFIRMED",
                                    mailbox_id=mailbox_id, sent_at=sent_at)
            session.commit()
    except Exception:
        logger.exception(
            "[stl_cadence] could not mark SENT_UNCONFIRMED message_id=%s touch=%s — "
            "SENDING claim still blocks a resend, but reconcile manually",
            message_id, touch_step,
        )


# ── Stop ─────────────────────────────────────────────────────────────────────

def stop_active_stl_cadences(
    session: Session,
    client_id: str,
    email: str,
    reason: str,
) -> int:
    """Latch STOPPED on all ARMED cadences for this client/email.

    Called from:
      - unsubscribe_router._do_unsubscribe (OPT_OUT)
      - booking_ingest after CLIENT_OWNER_BOOKING confirmed (BOOKED)
      - inbound_ingest (reply via Mailgun, if Reply-To is configured) (REPLY)

    Returns count of rows latched. No-op if no matching ARMED rows.
    Runs on the caller's session (RLS app session from unsubscribe_router, or
    the BYPASSRLS system session from inbound_ingest/booking_ingest); either
    way the WHERE client_id = :cid guard scopes the batch to this client.
    Does NOT commit — the caller owns the surrounding transaction."""
    if not email:
        return 0
    rows = session.execute(
        text(
            "UPDATE inbound_messages "
            "SET cadence_state = 'STOPPED', "
            "    cadence_stopped_at = NOW(), "
            "    cadence_stop_reason = :reason "
            "WHERE client_id = :client_id "
            "  AND lower(sender_email) = lower(:email) "
            "  AND cadence_state = 'ARMED' "
            "RETURNING id"
        ),
        {"client_id": client_id, "email": email, "reason": reason},
    ).fetchall()
    count = len(rows)
    for r in rows:
        message_id = r[0]
        _cancel_queued_touch_orders(session, client_id, message_id)
        log_event(
            client_id,
            "speed_to_lead_opted_out" if reason == "OPT_OUT" else "stl_cadence_stopped",
            entity_type="inbound_message",
            entity_id=str(message_id),
            payload={"email": email, "reason": reason},
            session=session,
        )
    return count


def stl_cadence_touch_still_ready(order) -> bool:
    """Pre-card gate check in sequence_sweep — mirrors _winback_touch_still_ready.

    Returns False (and marks the order SKIPPED) if the lead's cadence has been
    stopped since the touch was enqueued. Prevents a stale approval card sitting
    in #blackink-setter after the prospect has already replied or opted out."""
    message_id = int(order.payload.get("message_id", order.entity_id))
    touch_step = order.payload.get("touch_step", "?")

    try:
        with get_db_context(client_id=order.client_id) as session:
            row = session.execute(
                text("SELECT cadence_state FROM inbound_messages WHERE id = :id"),
                {"id": message_id},
            ).fetchone()
    except Exception:
        logger.exception("[stl_cadence] gate_check: DB error for message_id=%s — skipping", message_id)
        wo.record_decision(order.client_id, order.action_id, decision="SKIPPED", decided_by="stl_cadence:gate_error")
        return False

    if row is None:
        logger.error("[stl_cadence] gate_check: message_id=%s not found — skipping touch %s", message_id, touch_step)
        wo.record_decision(order.client_id, order.action_id, decision="SKIPPED", decided_by="stl_cadence:not_found")
        return False

    if row.cadence_state == "STOPPED":
        logger.info(
            "[stl_cadence] gate_check: touch %s SKIPPED — cadence STOPPED for message_id=%s",
            touch_step, message_id,
        )
        wo.record_decision(order.client_id, order.action_id, decision="SKIPPED", decided_by="stl_cadence:gate_stopped")
        return False

    return True


# ── DB helpers ────────────────────────────────────────────────────────────────

def _claim_dispatch(
    session: Session,
    message_id: int,
    touch_step: int,
    client_id: str,
    status: str,
) -> bool:
    """INSERT with ON CONFLICT DO NOTHING — returns True if this call claimed,
    False if already claimed by a prior run (at-most-once guard)."""
    result = session.execute(
        text(
            "INSERT INTO stl_cadence_dispatches "
            "  (client_id, message_id, touch_step, status) "
            "VALUES (:client_id, :message_id, :touch_step, :status) "
            "ON CONFLICT (message_id, touch_step) DO NOTHING"
        ),
        {"client_id": client_id, "message_id": message_id,
         "touch_step": touch_step, "status": status},
    )
    return bool(result.rowcount)


def _update_dispatch_status(
    session: Session,
    message_id: int,
    touch_step: int,
    status: str,
    mailbox_id: Optional[int] = None,
    sent_at: Optional[datetime] = None,
) -> None:
    session.execute(
        text(
            "UPDATE stl_cadence_dispatches "
            "SET status = :status, mailbox_id = :mb, sent_at = :sent_at, updated_at = NOW() "
            "WHERE message_id = :message_id AND touch_step = :touch_step"
        ),
        {"status": status, "mb": mailbox_id, "sent_at": sent_at,
         "message_id": message_id, "touch_step": touch_step},
    )


def _latch_stop(session: Session, message_id: int, reason: str) -> None:
    row = session.execute(
        text(
            "UPDATE inbound_messages "
            "SET cadence_state = 'STOPPED', cadence_stopped_at = NOW(), "
            "    cadence_stop_reason = :reason "
            "WHERE id = :id AND (cadence_state IS NULL OR cadence_state = 'ARMED') "
            "RETURNING client_id"
        ),
        {"id": message_id, "reason": reason},
    ).fetchone()
    if row is not None:
        _cancel_queued_touch_orders(session, row[0], message_id)
    session.commit()


def _cancel_queued_touch_orders(session: Session, client_id: str, message_id) -> None:
    """Mark every still-QUEUED STL touch work order for this message SKIPPED.

    On latching STOPPED we cancel outstanding touches eagerly rather than
    relying only on the lazy stl_cadence_touch_still_ready gate at sweep time —
    so a card never even surfaces after a reply/booking/opt-out. agent_work_orders
    has no CANCELLED status, so SKIPPED is the terminal no-send state (winback
    precedent). The status='QUEUED' guard keeps this idempotent."""
    session.execute(
        text(
            "UPDATE agent_work_orders "
            "SET status = 'SKIPPED', decided_by = 'stl_cadence:stop', "
            "    decided_at = NOW(), updated_at = NOW() "
            "WHERE client_id = :client_id "
            "  AND action_class = :action "
            "  AND entity_id = :entity_id "
            "  AND status = 'QUEUED'"
        ),
        {"client_id": client_id, "action": _TOUCH_ACTION, "entity_id": str(message_id)},
    )


def _stop_reason_from_event(event_type: str) -> str:
    return {
        "inbound_reply_received": "REPLY",
        "meeting_booked": "BOOKED",
        "speed_to_lead_opted_out": "OPT_OUT",
    }.get(event_type, "REPLY")


# ── Template helpers ──────────────────────────────────────────────────────────

def _default_touch_template(step: int) -> dict:
    """Default follow-up templates. Clients override stl_reply_html_template
    on the clients row; per-touch custom templates are a future enhancement."""
    templates = {
        1: {
            "subject": "Still interested in learning more?",
            "body": (
                "<p>Hi {{name}},</p>"
                "<p>I wanted to follow up on your recent inquiry. "
                "We'd love to connect and discuss how we can help you.</p>"
                "<p>{{booking_link}}</p>"
                "<p>Best regards,<br>The Team</p>"
                "{{unsubscribe}}"
            ),
        },
        2: {
            "subject": "Quick question about your inquiry",
            "body": (
                "<p>Hi {{name}},</p>"
                "<p>I wanted to check in — did you have any questions about our services? "
                "I'm happy to answer anything.</p>"
                "<p>{{booking_link}}</p>"
                "<p>Best regards,<br>The Team</p>"
                "{{unsubscribe}}"
            ),
        },
        3: {
            "subject": "Checking in",
            "body": (
                "<p>Hi {{name}},</p>"
                "<p>Just checking in. We're still here if you'd like to chat about how we can help.</p>"
                "<p>{{booking_link}}</p>"
                "<p>Best regards,<br>The Team</p>"
                "{{unsubscribe}}"
            ),
        },
        4: {
            "subject": "One more reach out",
            "body": (
                "<p>Hi {{name}},</p>"
                "<p>I don't want to be a bother, but I wanted to make sure your inquiry didn't "
                "fall through the cracks. We'd love to help if the timing is right.</p>"
                "<p>{{booking_link}}</p>"
                "<p>Best regards,<br>The Team</p>"
                "{{unsubscribe}}"
            ),
        },
        5: {
            "subject": "Last follow-up from us",
            "body": (
                "<p>Hi {{name}},</p>"
                "<p>This is our final follow-up. If you're still interested in our services, "
                "we'd love to connect whenever you're ready.</p>"
                "<p>{{booking_link}}</p>"
                "<p>Best regards,<br>The Team</p>"
                "{{unsubscribe}}"
            ),
        },
    }
    return templates.get(step, templates[1])


def _render_touch_html(
    template: str,
    name: str,
    booking_url: Optional[str],
    unsubscribe_url: Optional[str] = None,
) -> str:
    html = template.replace("{{name}}", name or "there")
    if booking_url:
        html = html.replace("{{booking_link}}", f'<a href="{booking_url}">Schedule a call</a>')
    else:
        html = html.replace("{{booking_link}}", "Feel free to reply to this email and we'll schedule a time.")
    if unsubscribe_url:
        html = html.replace(
            "{{unsubscribe}}",
            f'<p style="font-size:11px;color:#999">Don\'t want to hear from us? '
            f'<a href="{unsubscribe_url}">Unsubscribe</a></p>',
        )
    else:
        html = html.replace("{{unsubscribe}}", "")
    return html


def _html_to_text(html: str) -> str:
    import re
    t = re.sub(r"(?i)<br\s*/?>", "\n", html)
    t = re.sub(r"(?i)</p\s*>", "\n\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()
