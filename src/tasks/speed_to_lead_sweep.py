"""SLA sweep for Speed-to-Lead auto-responses (Task 4.2.1).

Dispatches auto-response emails for inbound_messages rows where
send_at <= NOW() AND status = 'RECEIVED'.

Runs under the BYPASSRLS system session (same posture as
booking_confirmation_sender.py) so the sweep spans all clients in one
pass. Per-client template resolution drops into get_db_context(client_id)
for the actual send so the send is still RLS-scoped.

    python -m src.tasks.speed_to_lead_sweep
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.database import get_system_db_context, get_db_context
from src.services.booking_link import resolve_owner_booking_link
from src.services.email_sender import build_email_sender
from src.services.events import log_event
from src.services.mailbox_dispatcher import get_active_mailbox_for_client, NoMailboxAvailable, AllMailboxesCapped

logger = logging.getLogger(__name__)

_BATCH = 50


class PostSendError(Exception):
    """A failure that occurred AFTER sender.send() already delivered the email.

    The SMTP send is not reversible, so the row must NOT be reset to RECEIVED
    (that would resend the same auto-response on the next tick). It goes to the
    terminal SENT_UNCONFIRMED state for manual reconciliation instead."""


def run_sweep(limit: int = _BATCH) -> int:
    dispatched = 0
    with get_system_db_context() as system_session:
        rows = _claim_due(system_session, limit)
    for row in rows:
        try:
            _send_response(row)
            dispatched += 1
        except PostSendError:
            # Email already went out — a pre-send retry would duplicate it.
            # Flag terminal for manual reconciliation; never re-claim.
            logger.exception(
                "[stl-sweep] post-send write failed for id=%s — email already sent, "
                "marking SENT_UNCONFIRMED (no resend)", row.id,
            )
            _mark_sent_unconfirmed(row.id)
        except Exception:
            # Failure BEFORE the SMTP send (mailbox pick, template, booking
            # link): safe to reset the claim so a later tick retries instead of
            # stranding the row in SENDING forever.
            logger.exception("[stl-sweep] failed before send for id=%s — resetting to RECEIVED", row.id)
            _reset_to_received(row.id)
    logger.info("[stl-sweep] dispatched %d response(s)", dispatched)
    return dispatched


def _claim_due(session: Session, limit: int):
    """Atomic claim: flip status RECEIVED→SENDING while fetching, so concurrent
    ticks never double-send (FOR UPDATE SKIP LOCKED). SENDING is the sweep's
    own in-progress sentinel, distinct from Dev 2's DEFERRED.

    Durable recovery: also reclaims rows stuck in SENDING whose send_at is more
    than 15 minutes past — a process crash between claim and mark would
    otherwise strand them. 15 min is well beyond a normal send, so a legitimate
    in-flight row is never stolen."""
    rows = session.execute(
        text(
            "UPDATE inbound_messages SET status = 'SENDING' "
            "WHERE id IN ("
            "  SELECT id FROM inbound_messages "
            # requires_human_review rows are NEVER auto-responded — an
            # unparsed/ambiguous notification (e.g. an HTML-only portal mail we
            # couldn't extract) must not trigger an automatic reply, which
            # could land on the portal's own address. A human dispatches these.
            "  WHERE requires_human_review = FALSE "
            "    AND ((status = 'RECEIVED' AND send_at <= NOW()) "
            "         OR (status = 'SENDING' AND send_at <= NOW() - INTERVAL '15 minutes')) "
            "  ORDER BY send_at ASC LIMIT :limit "
            "  FOR UPDATE SKIP LOCKED"
            ") "
            "RETURNING id, client_id, source_channel, "
            "          sender_name, sender_email, received_at, idempotency_key"
        ),
        {"limit": limit},
    ).fetchall()
    session.commit()
    return rows


def _reset_to_received(message_id) -> None:
    """Reset a failed/stranded claim back to RECEIVED for retry."""
    try:
        with get_system_db_context() as session:
            session.execute(
                text("UPDATE inbound_messages SET status = 'RECEIVED' "
                     "WHERE id = :id AND status = 'SENDING'"),
                {"id": message_id},
            )
            session.commit()
    except Exception:
        logger.exception("[stl-sweep] failed to reset id=%s to RECEIVED", message_id)


def _mark_sent_unconfirmed(message_id) -> None:
    """Terminal flag for a row whose email was sent but whose post-send write
    failed. Never re-claimed by _claim_due (not RECEIVED/SENDING); a human
    reconciles mailbox_id/responded_at. Prevents duplicate resends."""
    try:
        with get_system_db_context() as session:
            session.execute(
                text("UPDATE inbound_messages SET status = 'SENT_UNCONFIRMED' "
                     "WHERE id = :id AND status = 'SENDING'"),
                {"id": message_id},
            )
            session.commit()
    except Exception:
        logger.exception("[stl-sweep] failed to mark id=%s SENT_UNCONFIRMED", message_id)


def _send_response(row) -> None:
    client_id = row.client_id
    message_id = str(row.id)

    prospect_email = row.sender_email
    prospect_name = row.sender_name

    with get_db_context(client_id=client_id) as session:
        if not prospect_email:
            logger.warning("[stl-sweep] no prospect email for id=%s — marking RESPONDED (no send)", message_id)
            _mark_responded(session, message_id, ack_latency=None, mailbox_id=None)
            return

        # Booking link points at the RECEIVING CLIENT's own owner-booking
        # calendar (CLIENT_OWNER_BOOKING), not an internal sales-demo calendar.
        booking = resolve_owner_booking_link(
            session, client_id, name=prospect_name, email=prospect_email
        )
        booking_url = booking.url if booking else None

        # Resolve per-client template
        template_row = session.execute(
            text(
                "SELECT stl_reply_subject, stl_reply_html_template "
                "FROM clients WHERE client_id = :client_id"
            ),
            {"client_id": client_id},
        ).first()

        subject = (template_row.stl_reply_subject if template_row else None) or "We received your inquiry"
        html_template = (template_row.stl_reply_html_template if template_row else None) or _default_template()

        html_body = html_template.replace("{{name}}", prospect_name or "there")
        if booking_url:
            html_body = html_body.replace("{{booking_link}}", f'<a href="{booking_url}">Schedule a call</a>')
        else:
            html_body = html_body.replace("{{booking_link}}", "We'll be in touch shortly to schedule a call.")
        text_body = _html_to_text(html_body)

        try:
            mailbox = get_active_mailbox_for_client(session, client_id)
        except (NoMailboxAvailable, AllMailboxesCapped) as exc:
            logger.error("[stl-sweep] no mailbox for client=%s id=%s: %s", client_id, message_id, exc)
            # Re-flip to RECEIVED so the next sweep tick retries
            session.execute(
                text("UPDATE inbound_messages SET status = 'RECEIVED' WHERE id = :id"),
                {"id": message_id},
            )
            session.commit()
            return

        sender = build_email_sender()
        sender.send(
            from_address=mailbox.mailbox_address,
            to_address=prospect_email,
            subject=subject,
            body=text_body,          # text/plain part
            html_body=html_body,     # text/html alternative
            sending_domain=mailbox.sending_domain,
        )

        # --- SMTP send has succeeded; everything past this point is a
        # post-send write. A failure here is NOT retryable-from-scratch (the
        # email is already out), so any exception is re-raised as PostSendError
        # for the caller to flag terminal, never reset to RECEIVED. ---
        try:
            now = datetime.now(timezone.utc)
            ack_latency = (now - row.received_at.replace(tzinfo=timezone.utc)).total_seconds()

            # Emit the event BEFORE the status-update commit. _mark_responded()
            # commits, which ends the transaction and clears SET LOCAL
            # app.current_client_id — an events INSERT after that commit has no
            # tenant context and RLS rejects it. Same transaction = one commit,
            # tenant scope still active for both writes.
            log_event(
                client_id,
                "speed_to_lead_response_sent",
                entity_type="inbound_message",
                entity_id=message_id,
                payload={"message_id": message_id, "ack_latency_seconds": ack_latency},
                session=session,
            )
            # Record the sending mailbox + timestamp so the mailbox picker counts
            # this send against that mailbox's rolling-24h cap.
            _mark_responded(session, message_id, ack_latency, mailbox_id=mailbox.mailbox_id)
        except Exception as exc:
            raise PostSendError(str(exc)) from exc


def _mark_responded(
    session: Session, message_id: str, ack_latency: Optional[float], mailbox_id: Optional[int]
) -> None:
    session.execute(
        text(
            "UPDATE inbound_messages SET status = 'RESPONDED', "
            "mailbox_id = :mb, responded_at = NOW() "
            "WHERE id = :id"
        ),
        {"mb": mailbox_id, "id": message_id},
    )
    session.commit()


def _html_to_text(html: str) -> str:
    """Minimal HTML→text for the multipart/alternative plain part. Not a full
    renderer — turns <br>/<p> into newlines and strips remaining tags."""
    import re

    t = re.sub(r"(?i)<br\s*/?>", "\n", html)
    t = re.sub(r"(?i)</p\s*>", "\n\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _default_template() -> str:
    return (
        "<p>Hi {{name}},</p>"
        "<p>Thank you for reaching out. We received your inquiry and will be in touch shortly.</p>"
        "<p>{{booking_link}}</p>"
        "<p>Best regards,<br>The Team</p>"
    )


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    run_sweep()
