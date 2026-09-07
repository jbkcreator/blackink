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
    with get_db_context() as session:
        row = session.execute(
            text(
                "SELECT client_id FROM clients "
                "WHERE subdomain_slug = :slug AND is_active = TRUE LIMIT 1"
            ),
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
    message_id = form.get("Message-Id", "") or form.get("message-id", "")

    # Derive email from sender field (e.g. "Name <email@domain.com>")
    email_match = re.search(r"<([^>]+)>", sender) or re.search(r"[\w.+-]+@[\w.-]+", sender)
    email = email_match.group(1 if "<" in sender else 0) if email_match else None

    dedupe_key = message_id.strip("<>") if message_id else hashlib.sha256(
        f"{client_id}:{sender}:{subject}:{body_plain[:200]}".encode()
    ).hexdigest()

    raw_payload = str(dict(form))[:4000]

    lead = InboundLead(
        client_id=client_id,
        channel="EMAIL",
        source_channel="LISTING_PORTAL",   # refined by 4.2.3 portal parsers
        dedupe_key=dedupe_key,
        prospect_name=None,
        email=email,
        phone=None,
        property_address=None,
        inquiry_text=body_plain[:2000],
        raw_payload=raw_payload,
    )

    try:
        result = run_inbound_pipeline(lead)
        logger.info("[mailgun] outcome=%s message_id=%s", result.outcome, result.message_id)
    except Exception:
        logger.exception("[mailgun] pipeline failed for client=%s", client_id)
        # Return 200 to Mailgun — non-200 triggers retry, which could storm
        # on a persistent error. Log and accept; alert via Slack.
        return {"ok": False, "detail": "pipeline error"}

    return {"ok": True, "outcome": result.outcome}
