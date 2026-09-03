"""Event logging service — writes structured rows to the `events` table."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def log_touch_dispatched(
    session: Session,
    client_id: str,
    contact_id: int,
    touch_step: int,
    dispatch_id: str,
    mailbox_id: int,
    sending_domain: str,
    template_version: str,
) -> None:
    """Log outbound_touch_dispatched event to events table.

    entity is the contact (entity_type='contact', entity_id=contact_id) and
    actor is 'cold_outbound_sequencer' per wayfinder ticket 03. occurred_at
    lives in the payload — the events table has no such column, and the
    buffered-event design loses the real occurrence time otherwise (ticket 02).
    """
    payload = {
        "touch_step": touch_step,
        "dispatch_id": dispatch_id,
        "mailbox_id": mailbox_id,
        "sending_domain": sending_domain,
        "template_version": template_version,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    session.execute(
        text(
            "INSERT INTO events (client_id, event_type, entity_type, entity_id, payload, actor) "
            "VALUES (:client_id, 'outbound_touch_dispatched', 'contact', :entity_id, :payload, "
            "'cold_outbound_sequencer')"
        ),
        {
            "client_id": client_id,
            "entity_id": str(contact_id),
            "payload": json.dumps(payload),
        },
    )
    logger.info(
        "events: outbound_touch_dispatched client=%s contact=%s touch=%d dispatch=%s",
        client_id, contact_id, touch_step, dispatch_id,
    )
