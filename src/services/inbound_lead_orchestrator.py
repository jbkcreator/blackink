"""Shared pipeline for all inbound leads (Task 4.2.1 Speed-to-Lead).

Both paths — Path A (webhook) and Path B (Mailgun email parse) — funnel
through run_inbound_pipeline(). Nothing else in the codebase writes to
inbound_messages directly; this is the single write path.

Pipeline steps (SPEC-4.2.1.md §2):
  1. Dedupe on idempotency_key (global UNIQUE) — duplicate → no-op, returns existing.
  2. Non-poach gate (advisory) — look up an EXISTING company by sender email
     domain; if claimed by another client → status=SUPPRESSED, event, return.
  3. Write inbound_messages (prospect fields stored inline, contact_id NULL)
     + inbound_lead_received event.
  4. Post closer-alert card to #blackink-setter immediately.
  5. Compute send_at (now if in business hours, else next business-open).

Inbound prospects are renters/owners inquiring — NOT PM-firm prospects — so
nothing is written to companies/contacts (those model outbound prospect firms
and require domain + county_slug). Prospect name/email/phone live directly on
inbound_messages.

The SLA sweep (src/tasks/speed_to_lead_sweep.py) dispatches the actual
auto-response email for rows where send_at <= now() AND status=RECEIVED.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.database import get_db_context
from src.services.events import log_event
from src.services.business_hours import compute_send_at


def _run_coro(coro):
    """Run a coroutine to completion from EITHER a sync caller (Path A's
    threadpool background task) or an async caller (Path B's async webhook
    handler). asyncio.run() raises if a loop is already running, so when one
    is, run the coroutine in a fresh loop on a worker thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # no running loop — safe
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()

logger = logging.getLogger(__name__)


@dataclass
class InboundLead:
    client_id: str
    channel: str                    # 'WEBHOOK' or 'EMAIL'
    source_channel: str             # e.g. 'WEBSITE_FORM', 'LISTING_PORTAL', 'APM', …
    idempotency_key: str            # namespaced per client; global-unique on inbound_messages
    destination_address: str        # Path A: "webhook:<source>"; Path B: the leads@ recipient
    prospect_name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    property_address: Optional[str]
    inquiry_text: Optional[str]
    subject: Optional[str] = None
    body_html: Optional[str] = None
    utm: Optional[dict] = None
    requires_human_review: bool = False


@dataclass
class PipelineResult:
    message_id: Optional[str]       # str(inbound_messages.id)
    outcome: str    # 'written' | 'duplicate' | 'suppressed'


def run_inbound_pipeline(lead: InboundLead) -> PipelineResult:
    """Execute the shared inbound pipeline. Caller owns no session — this
    function opens its own get_db_context so the entire pipeline is atomic."""

    with get_db_context(client_id=lead.client_id) as session:
        return _run(session, lead)


def _run(session: Session, lead: InboundLead) -> PipelineResult:
    # ── 1. Dedupe ─────────────────────────────────────────────────────────
    # idempotency_key is GLOBALLY unique on inbound_messages (Dev 2's
    # constraint); the key is already namespaced with client_id by the caller.
    existing = session.execute(
        text("SELECT id FROM inbound_messages WHERE idempotency_key = :k LIMIT 1"),
        {"k": lead.idempotency_key},
    ).scalar()
    if existing:
        logger.info("[inbound] duplicate idempotency_key=%s client=%s — no-op", lead.idempotency_key, lead.client_id)
        return PipelineResult(message_id=str(existing), outcome="duplicate")

    # ── 2. Non-poach gate ─────────────────────────────────────────────────
    #
    # An inbound speed-to-lead prospect is a renter/owner inquiring — NOT a
    # PM-firm prospect — so nothing is written to companies/contacts here.
    # Suppress if the sender's email domain is claimed by ANOTHER client's PM
    # book. This MUST go through the domain SECURITY DEFINER predicate: a plain
    # SELECT on companies runs under this tenant's RLS and would hide exactly
    # the other-client company we need to detect, silently defeating non-poach.
    if _is_domain_claimed_by_other_client(session, lead):
        message_id = _write_message(session, lead, status="SUPPRESSED", send_at=None)
        log_event(
            lead.client_id,
            "non_poach_suppressed",
            entity_type="inbound_message",
            entity_id=message_id,
            payload={"message_id": message_id, "source_channel": lead.source_channel},
            session=session,
        )
        return PipelineResult(message_id=message_id, outcome="suppressed")

    # ── 3. Write inbound_messages + event ─────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    send_at = compute_send_at(now_utc)
    sla_due_at = _compute_sla_due(now_utc)

    message_id = _write_message(
        session, lead,
        status="RECEIVED", send_at=send_at, lead_sla_due_at=sla_due_at,
    )
    log_event(
        lead.client_id,
        "inbound_lead_received",
        entity_type="inbound_message",
        entity_id=message_id,
        payload={"message_id": message_id, "source_channel": lead.source_channel, "channel": lead.channel},
        session=session,
    )

    # ── 5. Closer-alert Slack card (immediate, best-effort) ───────────────
    try:
        _post_closer_alert(session, lead, message_id, now_utc)
    except Exception:
        # Never let Slack failure block the lead write — the DB row is the
        # durable record; the card is best-effort.
        logger.exception("[inbound] closer alert failed for message_id=%s", message_id)

    # ── 6. Arm cadence: enqueue +24h check for the 5-touch follow-up ──────
    # Task 4.2.2 — best-effort, never blocks the pipeline. A failure here
    # is logged; the arm-check work order can always be re-enqueued manually.
    try:
        from src.services.stl_cadence import arm_cadence
        arm_cadence(session, lead.client_id, message_id, now_utc)
    except Exception:
        logger.exception("[inbound] cadence arm failed for message_id=%s", message_id)

    return PipelineResult(message_id=message_id, outcome="written")


# ── Helpers ───────────────────────────────────────────────────────────────

def _is_domain_claimed_by_other_client(session: Session, lead: InboundLead) -> bool:
    """True if the sender's email domain is claimed by ANOTHER client's PM book.

    Goes through the is_domain_claimed_by_other_client SECURITY DEFINER function
    (apply_inbound_messages_lead_fields.py): it bypasses RLS to see other
    tenants' claims — a plain companies SELECT under this tenant's RLS would hide
    exactly the row that must trigger suppression — while deriving the requesting
    client from session context and returning only a boolean (no identity leak).
    Fails closed (True/suppress) on error, since a non-poach breach is worse than
    a missed auto-response."""
    if not lead.email or "@" not in lead.email:
        return False
    domain = lead.email.split("@", 1)[1].lower().strip()
    try:
        claimed = session.execute(
            text("SELECT is_domain_claimed_by_other_client(:domain) AS claimed"),
            {"domain": domain},
        ).scalar()
        return bool(claimed)
    except Exception:
        logger.exception("[inbound] non-poach domain check failed for %s — suppressing (fail closed)", domain)
        return True


def _write_message(
    session: Session,
    lead: InboundLead,
    *,
    status: str,
    send_at: Optional[datetime],
    lead_sla_due_at: Optional[datetime] = None,
) -> str:
    """Insert one inbound_messages row using Dev 2's column names, and return
    str(id). sender_email is NOT NULL on the shared table — a phone-only lead
    stores '' there (the SLA sweep then skips the email send) and keeps the
    number in sender_phone."""
    import json as _json
    new_id = session.execute(
        text(
            "INSERT INTO inbound_messages "
            "(client_id, idempotency_key, destination_address, original_message_id, "
            " sender_email, sender_name, sender_phone, subject, body_text, body_html, "
            " channel, source_channel, property_address, "
            " received_at, send_at, lead_sla_due_at, status) "
            "VALUES (:client_id, :idempotency_key, :destination_address, :original_message_id, "
            "        :sender_email, :sender_name, :sender_phone, :subject, :body_text, :body_html, "
            "        :channel, :source_channel, :property_address, "
            "        NOW(), :send_at, :lead_sla_due_at, :status) "
            "RETURNING id"
        ),
        {
            "client_id": lead.client_id,
            "idempotency_key": lead.idempotency_key,
            "destination_address": lead.destination_address,
            "original_message_id": lead.idempotency_key,
            "sender_email": lead.email or "",
            "sender_name": lead.prospect_name,
            "sender_phone": lead.phone,
            "subject": lead.subject,
            "body_text": lead.inquiry_text,
            "body_html": lead.body_html,
            "channel": lead.channel,
            "source_channel": lead.source_channel,
            "property_address": lead.property_address,
            "send_at": send_at,
            "lead_sla_due_at": lead_sla_due_at,
            "status": status,
        },
    ).scalar()
    # utm is a Dev 4 extension column; set separately so a fresh DB missing it
    # (shouldn't happen — this migration adds it) never fails the core insert.
    if lead.utm:
        session.execute(
            text("UPDATE inbound_messages SET utm = CAST(:utm AS JSONB) WHERE id = :id"),
            {"utm": _json.dumps(lead.utm), "id": new_id},
        )
    if lead.requires_human_review:
        session.execute(
            text("UPDATE inbound_messages SET requires_human_review = TRUE WHERE id = :id"),
            {"id": new_id},
        )
    return str(new_id)


def _compute_sla_due(received_at: datetime) -> datetime:
    from datetime import timedelta
    return received_at + timedelta(minutes=30)


def _post_closer_alert(session: Session, lead: InboundLead, message_id: str, received_at: datetime) -> None:
    """Post a Slack card to the setter channel (#blackink-setter) with lead
    context. Uses the shared async post_action_card helper; runs it to
    completion from this sync context. No-ops (returns None) when Slack isn't
    configured. Only logs closer_alert_posted when the post actually lands."""
    from src.services.slack.post import post_action_card

    channel_key = "setter"
    local_time = received_at.strftime("%Y-%m-%d %H:%M UTC")
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*New Inbound Lead* — `{lead.source_channel}`\n"
                    f"*Name:* {lead.prospect_name or 'Unknown'}\n"
                    f"*Email:* {lead.email or '-'}  |  *Phone:* {lead.phone or '-'}\n"
                    f"*Property:* {lead.property_address or '-'}\n"
                    f"*Inquiry:* {(lead.inquiry_text or '')[:300]}\n"
                    f"*Client:* `{lead.client_id}`  |  *Received:* {local_time}"
                ),
            },
        }
    ]
    result = _run_coro(
        post_action_card(channel_key=channel_key, text="New inbound lead", blocks=blocks)
    )
    if result is None:
        return  # Slack unconfigured or post failed — nothing to record.

    log_event(
        lead.client_id,
        "closer_alert_posted",
        entity_type="inbound_message",
        entity_id=message_id,
        payload={"message_id": message_id, "slack_channel": result["channel_id"]},
        session=session,
    )
