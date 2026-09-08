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
from src.services.booking_link import resolve_booking_link
from src.services.email_sender import build_email_sender
from src.services.events import log_event
from src.services.mailbox_dispatcher import get_active_mailbox_for_client, NoMailboxAvailable, AllMailboxesCapped

logger = logging.getLogger(__name__)

_BATCH = 50


def run_sweep(limit: int = _BATCH) -> int:
    dispatched = 0
    with get_system_db_context() as system_session:
        rows = _claim_due(system_session, limit)
    for row in rows:
        try:
            _send_response(row)
            dispatched += 1
        except Exception:
            logger.exception("[stl-sweep] failed for message_id=%s", row.message_id)
    logger.info("[stl-sweep] dispatched %d response(s)", dispatched)
    return dispatched


def _claim_due(session: Session, limit: int):
    """Atomic claim: flip status to DEFERRED (in-progress sentinel) while
    fetching; prevents concurrent sweep ticks from double-sending."""
    rows = session.execute(
        text(
            "UPDATE inbound_messages SET status = 'DEFERRED' "
            "WHERE message_id IN ("
            "  SELECT message_id FROM inbound_messages "
            "  WHERE status = 'RECEIVED' AND send_at <= NOW() "
            "  ORDER BY send_at ASC LIMIT :limit "
            "  FOR UPDATE SKIP LOCKED"
            ") "
            "RETURNING message_id, client_id, source_channel, "
            "          prospect_name, prospect_email, received_at, dedupe_key"
        ),
        {"limit": limit},
    ).fetchall()
    session.commit()
    return rows


def _send_response(row) -> None:
    client_id = row.client_id
    message_id = str(row.message_id)

    prospect_email = row.prospect_email
    prospect_name = row.prospect_name

    with get_db_context(client_id=client_id) as session:
        if not prospect_email:
            logger.warning("[stl-sweep] no prospect email for message_id=%s — marking RESPONDED", message_id)
            _mark_responded(session, message_id, ack_latency=None)
            return

        # Resolve booking link (best-effort; None → omit from email)
        booking = resolve_booking_link(session, name=prospect_name, email=prospect_email)
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

        try:
            mailbox = get_active_mailbox_for_client(session, client_id)
        except (NoMailboxAvailable, AllMailboxesCapped) as exc:
            logger.error("[stl-sweep] no mailbox for client=%s message=%s: %s", client_id, message_id, exc)
            # Re-flip to RECEIVED so the next sweep tick retries
            session.execute(
                text("UPDATE inbound_messages SET status = 'RECEIVED' WHERE message_id = :id"),
                {"id": message_id},
            )
            session.commit()
            return

        sender = build_email_sender()
        sender.send(
            from_address=mailbox.mailbox_address,
            to_address=prospect_email,
            subject=subject,
            body=html_body,
            sending_domain=mailbox.sending_domain,
        )

        now = datetime.now(timezone.utc)
        ack_latency = (now - row.received_at.replace(tzinfo=timezone.utc)).total_seconds()
        _mark_responded(session, message_id, ack_latency)

        log_event(
            client_id,
            "speed_to_lead_response_sent",
            entity_type="inbound_message",
            entity_id=message_id,
            payload={"message_id": message_id, "ack_latency_seconds": ack_latency},
            session=session,
        )


def _mark_responded(session: Session, message_id: str, ack_latency: Optional[float]) -> None:
    session.execute(
        text(
            "UPDATE inbound_messages SET status = 'RESPONDED', "
            "ack_latency_seconds = :ack WHERE message_id = :id"
        ),
        {"ack": ack_latency, "id": message_id},
    )
    session.commit()


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
