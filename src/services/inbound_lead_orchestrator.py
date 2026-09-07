"""Shared pipeline for all inbound leads (Task 4.2.1 Speed-to-Lead).

Both paths — Path A (webhook) and Path B (Mailgun email parse) — funnel
through run_inbound_pipeline(). Nothing else in the codebase writes to
inbound_messages directly; this is the single write path.

Pipeline steps (SPEC-4.2.1.md §2):
  1. Dedupe on (client_id, dedupe_key) — duplicate → no-op, returns None.
  2. Upsert contacts row, deduped on email/phone per client_id.
  3. Non-poach gate — on match → status=SUPPRESSED, event, return.
  4. Write inbound_messages + inbound_lead_received event.
  5. Post closer-alert card to #blackink-setter immediately.
  6. Compute send_at (now if in business hours, else next business-open).

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

from src.core.database import session_scope
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
    company_name: Optional[str] = None    # for non-poach gate if resolvable


@dataclass
class PipelineResult:
    message_id: Optional[str]
    outcome: str    # 'written' | 'duplicate' | 'suppressed'


def run_inbound_pipeline(lead: InboundLead) -> PipelineResult:
    """Execute the shared inbound pipeline. Caller owns no session — this
    function opens its own session_scope so the entire pipeline is atomic."""

    with session_scope(client_id=lead.client_id) as session:
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

    # ── 2. Upsert contact ─────────────────────────────────────────────────
    contact_id = _upsert_contact(session, lead)

    # ── 3. Non-poach gate ─────────────────────────────────────────────────
    #
    # Gate is advisory for inbound leads (no company_id resolution yet —
    # inbound only has email/phone, not a property-management company).
    # If a company_name is supplied we attempt a domain-based lookup; if
    # company_id can't be resolved we skip the gate (never hard-block on
    # unresolvable). The gate is strict-block only where company_id is
    # positively known.
    company_id = _resolve_company_id(session, lead)
    if company_id:
        suppressed = _check_non_poach(session, company_id)
        if suppressed:
            message_id = _write_message(session, lead, contact_id, status="SUPPRESSED", send_at=None)
            log_event(
                lead.client_id,
                "non_poach_suppressed",
                entity_type="inbound_message",
                entity_id=message_id,
                payload={"message_id": message_id, "source_channel": lead.source_channel, "company_id": company_id},
                session=session,
            )
            return PipelineResult(message_id=message_id, outcome="suppressed")

    # ── 4. Write inbound_messages + event ─────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    send_at = compute_send_at(now_utc)
    sla_due_at = _compute_sla_due(now_utc)

    message_id = _write_message(
        session, lead, contact_id,
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

def _upsert_contact(session: Session, lead: InboundLead) -> Optional[int]:
    """Upsert a contacts row keyed on (client_id's owning company OR email/phone).
    Returns contact_id (int) or None if neither email nor phone supplied."""
    if not lead.email and not lead.phone:
        return None

    # Try email match first, then phone — one lookup avoids a full upsert
    # when the contact already exists under this client.
    row = session.execute(
        text(
            "SELECT c.id FROM contacts c "
            "JOIN companies co ON co.id = c.company_id "
            "WHERE co.owning_client_id = :client_id "
            "AND (c.email = :email OR c.phone = :phone) "
            "LIMIT 1"
        ),
        {"client_id": lead.client_id, "email": lead.email or "", "phone": lead.phone or ""},
    ).first()
    if row:
        return row.id

    # New contact — we need a company shell to FK into. Use a "INBOUND" pseudo-company
    # keyed by client_id + email domain (or phone hash) so repeated inbound
    # from same prospect doesn't explode companies.
    company_id = _ensure_inbound_company(session, lead)
    if company_id is None:
        return None

    result = session.execute(
        text(
            "INSERT INTO contacts (company_id, first_name, email, phone, created_at) "
            "VALUES (:company_id, :first_name, :email, :phone, NOW()) "
            "RETURNING id"
        ),
        {
            "company_id": company_id,
            "first_name": lead.prospect_name or "",
            "email": lead.email,
            "phone": lead.phone,
        },
    ).scalar()
    return result


def _ensure_inbound_company(session: Session, lead: InboundLead) -> Optional[int]:
    """Return or create a lightweight company shell for this inbound lead.
    Uses SHA-256 of (client_id + email_domain_or_phone) as company_id,
    consistent with BaseIngestLoader.compute_company_id convention."""
    if lead.email and "@" in lead.email:
        domain = lead.email.split("@", 1)[1].lower().strip()
        raw_key = f"{lead.client_id}:inbound:{domain}"
    elif lead.phone:
        raw_key = f"{lead.client_id}:inbound:phone:{lead.phone}"
    else:
        return None

    company_id = hashlib.sha256(raw_key.encode()).hexdigest()

    exists = session.execute(
        text("SELECT id FROM companies WHERE company_id = :company_id LIMIT 1"),
        {"company_id": company_id},
    ).scalar()
    if exists:
        return exists

    result = session.execute(
        text(
            "INSERT INTO companies (company_id, owning_client_id, name, created_at) "
            "VALUES (:company_id, :client_id, :name, NOW()) "
            "RETURNING id"
        ),
        {
            "company_id": company_id,
            "client_id": lead.client_id,
            "name": lead.company_name or (f"Inbound:{domain}" if lead.email and "@" in lead.email else "Inbound"),
        },
    ).scalar()
    return result


def _resolve_company_id(session: Session, lead: InboundLead) -> Optional[str]:
    """Try to resolve a known company_id from email domain for the non-poach gate.
    Returns None if unresolvable — gate is skipped, never blocks on unknown."""
    if not lead.email or "@" not in lead.email:
        return None
    domain = lead.email.split("@", 1)[1].lower().strip()
    raw_key = f"{lead.client_id}:inbound:{domain}"
    return hashlib.sha256(raw_key.encode()).hexdigest()


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
    contact_id: Optional[int],
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
            "(message_id, client_id, contact_id, channel, source_channel, raw_payload, "
            " received_at, send_at, lead_sla_due_at, status, dedupe_key, utm) "
            "VALUES (:message_id, :client_id, :contact_id, :channel, :source_channel, "
            "        :raw_payload, NOW(), :send_at, :lead_sla_due_at, :status, :dedupe_key, :utm)"
        ),
        {
            "message_id": message_id,
            "client_id": lead.client_id,
            "contact_id": contact_id,
            "channel": lead.channel,
            "source_channel": lead.source_channel,
            "raw_payload": lead.raw_payload,
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
