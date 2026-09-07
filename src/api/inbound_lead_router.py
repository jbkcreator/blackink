"""Path A — webhook ingest endpoint for Speed-to-Lead (Task 4.2.1).

POST /api/v1/webhooks/inbound-lead

Auth: per-client shared secret supplied in the `X-Client-Secret` header.
The secret is resolved to a client_id; unknown or missing → 401.

Payload (JSON): {
  client_secret, prospect_name, email, phone, property_address,
  inquiry_text, source (WEBSITE_FORM|LISTING_PORTAL), utm?
}
422 if neither email nor phone present.
Processing budget: 2s (FastAPI background task pattern used so the HTTP
response returns immediately; the pipeline runs async-safe).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
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
    """Look up client_id from a hashed shared secret stored in the clients table."""
    secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()
    with get_db_context() as session:
        row = session.execute(
            text(
                "SELECT client_id FROM clients "
                "WHERE inbound_webhook_secret_hash = :h AND is_active = TRUE LIMIT 1"
            ),
            {"h": secret_hash},
        ).first()
    return row.client_id if row else None


@router.post("/inbound-lead", status_code=202)
def receive_inbound_lead(
    payload: InboundLeadPayload,
    background_tasks: BackgroundTasks,
) -> dict:
    client_id = _resolve_client_id(payload.client_secret)
    if not client_id:
        raise HTTPException(status_code=401, detail="invalid client_secret")

    import uuid as _uuid
    dedupe_key = payload.external_id or _uuid.uuid4().hex

    lead = InboundLead(
        client_id=client_id,
        channel="WEBHOOK",
        source_channel=payload.source,
        dedupe_key=dedupe_key,
        prospect_name=payload.prospect_name,
        email=payload.email,
        phone=payload.phone,
        property_address=payload.property_address,
        inquiry_text=payload.inquiry_text,
        raw_payload=payload.model_dump_json(),
        utm=payload.utm,
    )
    background_tasks.add_task(_dispatch, lead)
    return {"accepted": True, "dedupe_key": dedupe_key}


def _dispatch(lead: InboundLead) -> None:
    try:
        result = run_inbound_pipeline(lead)
        logger.info("[inbound-webhook] outcome=%s message_id=%s", result.outcome, result.message_id)
    except Exception:
        logger.exception("[inbound-webhook] pipeline failed for client=%s", lead.client_id)
