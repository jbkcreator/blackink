"""Inbound reply ingestion (Task 3.1.3 / ticket 30).

Owns the cross-client work for a forwarded prospect reply: client resolution,
BCC-loop break, dedup, two-tier attribution, persistence, and the
#sales-replies Slack card. This is the service layer that holds the
`blackink_system` BYPASSRLS session — the webhook router (src/api/) must never
open that session itself (CLAUDE.md invariant), so it hands the parsed payload
here and gets back a small result dict for its HTTP response.

All DB access uses sqlalchemy.text() with named binds.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.inbound_attribution import (
    attribute,
    is_bcc_echo,
    resolve_client_from_alias,
    strip_display_name,
)
from src.services.ovs_lookup import fetch_latest_ovs, ovs_card_lines
from src.services.slack import post as slack_post
from src.services.slack.listeners import sales_reply_content_blocks

logger = logging.getLogger(__name__)


@dataclass
class InboundParsed:
    """The verified, parsed fields of one Mailgun inbound POST."""

    to_alias: str
    from_raw: str
    subject: Optional[str]
    raw_body: Optional[str]
    inbound_message_id: str
    in_reply_to: Optional[str]


async def ingest_inbound_reply(parsed: InboundParsed) -> dict:
    """Resolve, persist, and surface one inbound reply.

    Returns a small dict describing the outcome (the router uses it verbatim as
    the 200 body): a discard reason for messages we drop on purpose, or
    ``{"status": "ok", "inbound_id": ...}`` once stored and posted.
    """
    from_address = strip_display_name(parsed.from_raw) or parsed.from_raw
    received_at = datetime.now(timezone.utc)

    logger.info(
        "[inbound_ingest] received from=%s alias=%s in_reply_to=%s",
        from_address,
        parsed.to_alias,
        parsed.in_reply_to and parsed.in_reply_to[:30],
    )

    with get_system_db_context() as session:
        client_id = resolve_client_from_alias(session, parsed.to_alias)
        if client_id is None:
            logger.warning("[inbound_ingest] unknown alias=%s — discarding", parsed.to_alias)
            return {"status": "discarded", "reason": "unknown_alias"}

        if is_bcc_echo(session, parsed.inbound_message_id):
            logger.info(
                "[inbound_ingest] BCC echo message_id=%s — dropping",
                parsed.inbound_message_id[:40],
            )
            return {"status": "discarded", "reason": "bcc_echo"}

        existing = session.execute(
            text("SELECT id FROM inbound_messages WHERE message_id = :mid LIMIT 1"),
            {"mid": parsed.inbound_message_id},
        ).first()
        if existing is not None:
            logger.info("[inbound_ingest] duplicate message_id — already stored, no-op")
            return {"status": "discarded", "reason": "duplicate"}

        result = attribute(
            session,
            client_id=client_id,
            in_reply_to=parsed.in_reply_to,
            from_address=from_address,
        )

        inbound_id = str(uuid.uuid4())
        try:
            session.execute(
                text(
                    """
                    INSERT INTO inbound_messages
                        (id, message_id, client_id, contact_id, run_id,
                         from_address, to_alias, in_reply_to, subject, raw_body,
                         attribution_status, received_at)
                    VALUES
                        (:id, :message_id, :client_id, :contact_id, :run_id,
                         :from_address, :to_alias, :in_reply_to, :subject, :raw_body,
                         :attribution_status, :received_at)
                    """
                ),
                {
                    "id": inbound_id,
                    "message_id": parsed.inbound_message_id,
                    "client_id": result.client_id,
                    "contact_id": result.contact_id,
                    "run_id": result.run_id,
                    "from_address": from_address,
                    "to_alias": parsed.to_alias,
                    "in_reply_to": parsed.in_reply_to,
                    "subject": parsed.subject,
                    "raw_body": parsed.raw_body,
                    "attribution_status": result.attribution_status,
                    "received_at": received_at,
                },
            )
        except Exception:
            # UNIQUE(message_id) race between the dedup read above and this insert.
            logger.warning("[inbound_ingest] INSERT conflict on message_id — concurrent duplicate")
            return {"status": "discarded", "reason": "duplicate_race"}

        session.execute(
            text(
                "INSERT INTO events (client_id, event_type, entity_type, entity_id, actor, payload) "
                "VALUES (:client_id, 'inbound_reply_received', 'inbound_message', :entity_id, 'mailgun_inbound', :payload)"
            ),
            {
                "client_id": result.client_id,
                "entity_id": inbound_id,
                "payload": json.dumps(
                    {
                        "inbound_id": inbound_id,
                        "message_id": parsed.inbound_message_id,
                        "from_address": from_address,
                        "attribution_status": result.attribution_status,
                        "contact_id": result.contact_id,
                        "run_id": result.run_id,
                        "channel": "email",
                        "raw_body": parsed.raw_body,
                    }
                ),
            },
        )

        thread_lines = _recent_thread(session, contact_id=result.contact_id, run_id=result.run_id, exclude_id=inbound_id)
        ovs_lines = ovs_card_lines(fetch_latest_ovs(session, result.firm_company_id))

        session.commit()

    card_text = (
        f"💬 Reply from {from_address}"
        + (f" · {result.contact_name}" if result.contact_name else "")
        + (f" ({result.firm_name})" if result.firm_name else "")
        + f" [{result.attribution_status}]"
    )
    blocks = sales_reply_content_blocks(
        from_address=from_address,
        contact_name=result.contact_name,
        firm_name=result.firm_name,
        run_id=result.run_id,
        touch_step=result.touch_step,
        attribution_status=result.attribution_status,
        subject=parsed.subject,
        raw_body=parsed.raw_body,
        contact_id=result.contact_id,
        client_id=result.client_id,
        inbound_id=inbound_id,
        firm_domain=result.firm_domain,
        thread_lines=None,  # history now posted as real threaded replies (below), not crammed in-card
        door_count=result.door_count,
        ovs_lines=ovs_lines,
    )
    # Colored side bar: green attributed / amber unattributed — quick triage cue.
    bar = "#2eb67d" if result.attribution_status == "attributed" else "#e8912d"
    card_ts = await slack_post.post_notice(
        channel_key="replies",
        text=card_text,
        attachments=[{"color": bar, "blocks": blocks}],
    )

    # Threaded history: post the last-3 prior messages as real replies under the
    # card (v2 §3.1.3 "message thread history"), oldest → newest so the thread
    # reads chronologically. Rep's own Reply-in-Thread lands in the same thread.
    if card_ts and thread_lines:
        for line in reversed(thread_lines):
            await slack_post.post_notice(channel_key="replies", text=line, thread_ts=card_ts)

    logger.info(
        "[inbound_ingest] stored inbound_id=%s attribution=%s",
        inbound_id[:8],
        result.attribution_status,
    )
    return {"status": "ok", "inbound_id": inbound_id}


def _recent_thread(session, *, contact_id, run_id, exclude_id) -> list[str]:
    """Last 3 messages for this contact's thread (v2 §3.1.3): prior inbound
    replies plus our own outbound touches, newest-first, rendered as card lines.
    Empty when the reply is unattributed (no contact to gather a thread for)."""
    if not contact_id:
        return []

    events: list[tuple] = []  # (sort_key_datetime, rendered_line)

    inbound = session.execute(
        text(
            "SELECT received_at, subject, raw_body FROM inbound_messages "
            "WHERE contact_id = :cid AND id <> :exclude "
            "ORDER BY received_at DESC LIMIT 3"
        ),
        {"cid": contact_id, "exclude": exclude_id},
    ).mappings().all()
    for r in inbound:
        snippet = (r["raw_body"] or r["subject"] or "").strip().replace("\n", " ")[:80]
        events.append((r["received_at"], f"⬅️ _{r['received_at']:%b %d}_ — {snippet}"))

    if run_id:
        outbound = session.execute(
            text(
                "SELECT sent_at, touch_step FROM sequence_touch_dispatches "
                "WHERE run_id = :run AND status = 'SENT' AND sent_at IS NOT NULL "
                "ORDER BY sent_at DESC LIMIT 3"
            ),
            {"run": run_id},
        ).mappings().all()
        for r in outbound:
            events.append((r["sent_at"], f"➡️ _{r['sent_at']:%b %d}_ — Touch {r['touch_step']} sent"))

    events.sort(key=lambda e: e[0], reverse=True)
    return [line for _, line in events[:3]]
