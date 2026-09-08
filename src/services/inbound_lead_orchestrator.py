"""Shared pipeline for all inbound leads (Task 4.2.1 Speed-to-Lead).

Both paths — Path A (webhook) and Path B (Mailgun email parse) — funnel
through run_inbound_pipeline(). Nothing else in the codebase writes to
inbound_messages directly; this is the single write path.

Pipeline steps (SPEC-4.2.1.md §2):
  1. Dedupe on (client_id, dedupe_key) — duplicate → no-op, returns existing.
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

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.database import get_db_context
from src.services.events import log_event
from src.services.business_hours import compute_send_at

logger = logging.getLogger(__name__)


@dataclass
class InboundLead:
    client_id: str
    channel: str                    # 'WEBHOOK' or 'EMAIL'
    source_channel: str             # e.g. 'WEBSITE_FORM', 'LISTING_PORTAL', 'APM', …
    dedupe_key: str                 # Path A: external_id; Path B: Mailgun Message-Id
    prospect_name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    property_address: Optional[str]
    inquiry_text: Optional[str]
    raw_payload: Optional[str]
    utm: Optional[dict] = None


@dataclass
class PipelineResult:
    message_id: Optional[str]
    outcome: str    # 'written' | 'duplicate' | 'suppressed'


def run_inbound_pipeline(lead: InboundLead) -> PipelineResult:
    """Execute the shared inbound pipeline. Caller owns no session — this
    function opens its own get_db_context so the entire pipeline is atomic."""

    with get_db_context(client_id=lead.client_id) as session:
        return _run(session, lead)


def _run(session: Session, lead: InboundLead) -> PipelineResult:
    # ── 1. Dedupe ─────────────────────────────────────────────────────────
    existing = session.execute(
        text(
            "SELECT message_id FROM inbound_messages "
            "WHERE client_id = :client_id AND dedupe_key = :dedupe_key LIMIT 1"
        ),
        {"client_id": lead.client_id, "dedupe_key": lead.dedupe_key},
    ).scalar()
    if existing:
        logger.info("[inbound] duplicate dedupe_key=%s client=%s — no-op", lead.dedupe_key, lead.client_id)
        return PipelineResult(message_id=str(existing), outcome="duplicate")

    # ── 2. Non-poach gate (advisory) ──────────────────────────────────────
    #
    # An inbound speed-to-lead prospect is a renter/owner inquiring — NOT a
    # PM-firm prospect — so nothing is written to companies/contacts here
    # (those model outbound prospect firms and require domain + county_slug).
    # The gate is advisory: it only looks up an EXISTING company by the
    # sender's email domain and suppresses if that company is claimed by
    # another client. No match → proceed. contact_id stays NULL.
    company_id = _resolve_existing_company_id(session, lead)
    if company_id and _check_non_poach(session, company_id):
        message_id = _write_message(session, lead, status="SUPPRESSED", send_at=None)
        log_event(
            lead.client_id,
            "non_poach_suppressed",
            entity_type="inbound_message",
            entity_id=message_id,
            payload={"message_id": message_id, "source_channel": lead.source_channel, "company_id": company_id},
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

    return PipelineResult(message_id=message_id, outcome="written")


# ── Helpers ───────────────────────────────────────────────────────────────

def _resolve_existing_company_id(session: Session, lead: InboundLead) -> Optional[str]:
    """Advisory non-poach lookup: does an EXISTING companies row match the
    sender's email domain? Returns its company_id, else None. Never creates a
    company — inbound leads are not PM-firm prospects. `companies.domain` is
    UNIQUE, so at most one row matches."""
    if not lead.email or "@" not in lead.email:
        return None
    domain = lead.email.split("@", 1)[1].lower().strip()
    row = session.execute(
        text("SELECT company_id FROM companies WHERE domain = :domain LIMIT 1"),
        {"domain": domain},
    ).scalar()
    return str(row) if row else None


def _check_non_poach(session: Session, company_id: str) -> bool:
    """Returns True if another client has claimed this company. Reads the
    session's own app.current_client_id — same approach as compliance_gate.py."""
    try:
        claimed = session.execute(
            text("SELECT is_claimed_by_other_client(:company_id) AS claimed"),
            {"company_id": company_id},
        ).scalar()
        return bool(claimed)
    except Exception:
        logger.exception("[inbound] non-poach check failed for company_id=%s — skipping gate", company_id)
        return False


def _write_message(
    session: Session,
    lead: InboundLead,
    *,
    status: str,
    send_at: Optional[datetime],
    lead_sla_due_at: Optional[datetime] = None,
) -> str:
    import json as _json
    message_id = str(uuid.uuid4())
    session.execute(
        text(
            "INSERT INTO inbound_messages "
            "(message_id, client_id, channel, source_channel, raw_payload, cleaned_body, "
            " prospect_name, prospect_email, prospect_phone, property_address, "
            " received_at, send_at, lead_sla_due_at, status, dedupe_key, utm) "
            "VALUES (:message_id, :client_id, :channel, :source_channel, :raw_payload, :cleaned_body, "
            "        :prospect_name, :prospect_email, :prospect_phone, :property_address, "
            "        NOW(), :send_at, :lead_sla_due_at, :status, :dedupe_key, :utm)"
        ),
        {
            "message_id": message_id,
            "client_id": lead.client_id,
            "channel": lead.channel,
            "source_channel": lead.source_channel,
            "raw_payload": lead.raw_payload,
            "cleaned_body": lead.inquiry_text,
            "prospect_name": lead.prospect_name,
            "prospect_email": lead.email,
            "prospect_phone": lead.phone,
            "property_address": lead.property_address,
            "send_at": send_at,
            "lead_sla_due_at": lead_sla_due_at,
            "status": status,
            "dedupe_key": lead.dedupe_key,
            "utm": _json.dumps(lead.utm) if lead.utm else None,
        },
    )
    return message_id


def _compute_sla_due(received_at: datetime) -> datetime:
    from datetime import timedelta
    return received_at + timedelta(minutes=30)


def _post_closer_alert(session: Session, lead: InboundLead, message_id: str, received_at: datetime) -> None:
    """Post a Slack card to #blackink-setter with lead context."""
    from src.services.slack.bolt_app import app as slack_app
    from config.settings import get_settings

    settings = get_settings()
    channel = getattr(settings, "slack_closer_alert_channel", "#blackink-setter")

    local_time = received_at.strftime("%Y-%m-%d %H:%M UTC")
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*New Inbound Lead* — `{lead.source_channel}`\n"
                    f"*Name:* {lead.prospect_name or 'Unknown'}\n"
                    f"*Email:* {lead.email or '—'}  |  *Phone:* {lead.phone or '—'}\n"
                    f"*Property:* {lead.property_address or '—'}\n"
                    f"*Inquiry:* {(lead.inquiry_text or '')[:300]}\n"
                    f"*Client:* `{lead.client_id}`  |  *Received:* {local_time}"
                ),
            },
        }
    ]
    slack_app.client.chat_postMessage(channel=channel, blocks=blocks, text="New inbound lead")

    log_event(
        lead.client_id,
        "closer_alert_posted",
        entity_type="inbound_message",
        entity_id=message_id,
        payload={"message_id": message_id, "slack_channel": channel},
        session=session,
    )
