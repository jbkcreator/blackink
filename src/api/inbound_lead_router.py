"""Path A — webhook ingest endpoint for Speed-to-Lead (Task 4.2.1).

POST /api/v1/webhooks/inbound-lead

Auth: per-client shared secret supplied in the `X-Client-Secret` header.
The secret is resolved to a client_id; unknown or missing → 401.

Payload (JSON): {
  client_secret, prospect_name, email, phone, property_address,
  inquiry_text, source (WEBSITE_FORM|LISTING_PORTAL), utm?
}
422 if neither email nor phone present.
The lead is persisted synchronously BEFORE the 202 is returned — a caller
that gets a 202 is guaranteed the lead is durably stored (or a duplicate of
one already stored). Persistence failure returns a retryable 503 so the
source can retry, rather than acking a lead that was silently lost.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy import text

from src.core.database import get_db_context
from src.services.inbound_lead_orchestrator import InboundLead, run_inbound_pipeline

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/webhooks", tags=["inbound"])


class InboundLeadPayload(BaseModel):
    client_secret: str
    prospect_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    property_address: Optional[str] = None
    inquiry_text: Optional[str] = None
    source: str = "WEBSITE_FORM"
    external_id: Optional[str] = None  # caller-supplied dedupe key
    utm: Optional[dict] = None

    @model_validator(mode="after")
    def require_email_or_phone(self) -> "InboundLeadPayload":
        if not self.email and not self.phone:
            raise ValueError("at least one of email or phone is required")
        return self


def _resolve_client_id(client_secret: str) -> Optional[str]:
    """Resolve client_id from a hashed shared secret. The clients table is
    RLS-scoped, so a bare (unscoped) session sees zero rows — resolution goes
    through the resolve_client_by_webhook_secret SECURITY DEFINER function
    (apply_clients_stl_fields.py), the same pre-tenant lookup pattern the
    booking webhooks use with resolve_calendar_connection."""
    secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()
    with get_db_context() as session:
        row = session.execute(
            text("SELECT client_id FROM resolve_client_by_webhook_secret(:h)"),
            {"h": secret_hash},
        ).first()
    return row.client_id if row else None


def _fallback_dedupe_key(client_id: str, payload: "InboundLeadPayload") -> str:
    """Deterministic dedupe key when the caller omits external_id. A random
    UUID would let a source retry (after a lost 202) create a duplicate lead
    and a duplicate response, since each retry would carry a new key. Deriving
    the key from the stable payload content makes an identical retry collapse
    onto the existing row via the UNIQUE idempotency_key constraint. client_id
    is folded into the hash so the globally-unique key can't collide across
    tenants."""
    canonical = "|".join([
        client_id,
        (payload.source or "").strip().lower(),
        (payload.email or "").strip().lower(),
        (payload.phone or "").strip(),
        (payload.inquiry_text or "").strip(),
        (payload.property_address or "").strip().lower(),
    ])
    return "auto-" + hashlib.sha256(canonical.encode()).hexdigest()


@router.post("/inbound-lead", status_code=202)
def receive_inbound_lead(payload: InboundLeadPayload) -> dict:
    client_id = _resolve_client_id(payload.client_secret)
    if not client_id:
        raise HTTPException(status_code=401, detail="invalid client_secret")

    # idempotency_key is globally UNIQUE on inbound_messages — namespace it
    # with client_id so a caller-supplied external_id can't collide across
    # tenants. The fallback hash is already client-scoped.
    if payload.external_id:
        idempotency_key = f"{client_id}:{payload.external_id}"
    else:
        idempotency_key = _fallback_dedupe_key(client_id, payload)

    lead = InboundLead(
        client_id=client_id,
        channel="WEBHOOK",
        source_channel=payload.source,
        idempotency_key=idempotency_key,
        destination_address=f"webhook:{payload.source}",
        prospect_name=payload.prospect_name,
        email=payload.email,
        phone=payload.phone,
        property_address=payload.property_address,
        inquiry_text=payload.inquiry_text,
        subject=None,
        body_html=None,
        utm=payload.utm,
    )
    # Persist synchronously BEFORE acking — a 202 means the lead is durably
    # stored. A failure returns a retryable 503 rather than losing the lead.
    try:
        result = run_inbound_pipeline(lead)
    except Exception:
        logger.exception("[inbound-webhook] persistence failed for client=%s", client_id)
        raise HTTPException(status_code=503, detail="temporarily unable to accept lead; retry")

    logger.info("[inbound-webhook] outcome=%s message_id=%s", result.outcome, result.message_id)
    return {"accepted": True, "dedupe_key": idempotency_key, "outcome": result.outcome}
