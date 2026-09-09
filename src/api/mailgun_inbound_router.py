"""Path B — Mailgun inbound email webhook for Speed-to-Lead (Task 4.2.1).

POST /api/v1/webhooks/mailgun-inbound

Mailgun Routes delivers emails to leads@{client-subdomain}.getblackink.com
here via HTTP POST (multipart form). HMAC-signed per Mailgun's webhook
signing spec — signature verified BEFORE any DB write; reject unsigned
deliveries with 406 (not 200, so Mailgun won't retry).

client_id is resolved from the subdomain slug in the recipient address
(e.g. leads@acme.getblackink.com → slug "acme" → clients lookup).

Portal-specific parsers (APM, Manage My Property, Thumbtack) are 4.2.3
and land separately; this handler treats the raw body as inquiry_text
and marks source_channel=LISTING_PORTAL until a parser identifies the
portal type.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_db_context
from src.services.inbound_lead_orchestrator import InboundLead, run_inbound_pipeline
from src.services.portal_parsers import EmailParts, classify_and_parse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/webhooks", tags=["inbound"])

_SUBDOMAIN_RE = re.compile(r"leads@([a-z0-9\-]+)\.getblackink\.com", re.IGNORECASE)


def _verify_mailgun_signature(timestamp: str, token: str, signature: str) -> bool:
    signing_key = get_settings().mailgun_webhook_signing_key
    if not signing_key:
        logger.error("[mailgun] MAILGUN_WEBHOOK_SIGNING_KEY not configured — rejecting all")
        return False
    expected = hmac.new(
        signing_key.encode(),
        f"{timestamp}{token}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_subdomain(recipient: str) -> Optional[str]:
    m = _SUBDOMAIN_RE.search(recipient)
    return m.group(1).lower() if m else None


def _resolve_client_id_from_slug(slug: str) -> Optional[str]:
    """Resolve client_id from the subdomain slug. The clients table is
    RLS-scoped, so a bare session sees zero rows — resolution goes through the
    resolve_client_by_subdomain SECURITY DEFINER function
    (apply_clients_stl_fields.py), same pre-tenant pattern as the booking
    webhooks' resolve_calendar_connection."""
    with get_db_context() as session:
        row = session.execute(
            text("SELECT client_id FROM resolve_client_by_subdomain(:slug)"),
            {"slug": slug},
        ).first()
    return row.client_id if row else None


@router.post("/mailgun-inbound", status_code=200)
async def mailgun_inbound(request: Request) -> dict:
    form = await request.form()

    timestamp = form.get("timestamp", "")
    token = form.get("token", "")
    signature = form.get("signature", "")

    if not _verify_mailgun_signature(timestamp, token, signature):
        logger.warning("[mailgun] signature verification failed")
        raise HTTPException(status_code=406, detail="invalid signature")

    recipient = form.get("recipient", "")
    slug = _extract_subdomain(recipient)
    if not slug:
        logger.warning("[mailgun] unrecognised recipient format: %s", recipient)
        raise HTTPException(status_code=406, detail="unrecognised recipient")

    client_id = _resolve_client_id_from_slug(slug)
    if not client_id:
        logger.warning("[mailgun] no client for slug=%s", slug)
        raise HTTPException(status_code=406, detail="unknown client subdomain")

    sender = form.get("sender", "")
    subject = form.get("subject", "")
    body_plain = form.get("body-plain", "") or form.get("stripped-text", "")
    body_html = form.get("body-html", "") or None
    mg_message_id = form.get("Message-Id", "") or form.get("message-id", "")

    # idempotency_key is globally UNIQUE — namespace with client_id. Prefer the
    # Mailgun Message-Id; fall back to a content hash if absent.
    stable = mg_message_id.strip("<>") if mg_message_id else hashlib.sha256(
        f"{sender}:{subject}:{body_plain[:200]}".encode()
    ).hexdigest()
    idempotency_key = f"{client_id}:{stable}"

    # 4.2.3 — classify the portal and extract structured fields.
    parsed = classify_and_parse(EmailParts(
        sender=sender,
        subject=subject,
        body_plain=body_plain,
        body_html=body_html,
    ))

    lead = InboundLead(
        client_id=client_id,
        channel="EMAIL",
        source_channel=parsed.source_channel,
        idempotency_key=idempotency_key,
        destination_address=recipient,
        prospect_name=parsed.prospect_name,
        # Never fall back to the From-header email for a portal notification:
        # the sender IS the portal's own address, so backfilling it would make
        # the STL sweep auto-reply to the portal instead of the owner. Only a
        # body-extracted email (parsed.email) is a real owner address; if none
        # was found, leave it unset (a name+phone lead still posts a closer
        # card, and the sweep skips the email send rather than misdirecting it).
        email=parsed.email,
        phone=parsed.phone,
        property_address=parsed.property_address,
        inquiry_text=(parsed.inquiry_text or body_plain)[:2000],
        subject=subject or None,
        body_html=body_html,
        requires_human_review=parsed.requires_human_review,
    )

    try:
        result = run_inbound_pipeline(lead)
        logger.info("[mailgun] outcome=%s message_id=%s", result.outcome, result.message_id)
    except Exception:
        logger.exception("[mailgun] persistence failed for client=%s", client_id)
        # Return a retryable 500 so Mailgun re-delivers rather than dropping
        # the lead. Mailgun backs off over hours then stops, so a persistent
        # error self-limits instead of storming. Signature/slug rejections
        # above stay 406 (not retryable — those will never succeed).
        raise HTTPException(status_code=500, detail="temporarily unable to persist lead; retry")

    return {"ok": True, "outcome": result.outcome}
