"""Inbound email router — receives normalized forwarded owner-reply payloads
from the inbound parse service (SendGrid/Mailgun/Postmark) and enqueues them
for the Respond triage worker.

Path:   POST /api/v1/inbound/reply/{client_id}

Auth:   X-Blackink-Inbound-Secret header. The parse service is configured
        to add this header. Fail-closed: if INBOUND_PARSE_SECRET is not set,
        every request returns 503 rather than processing unauthenticated mail.

The parse service's per-provider payload format is normalized to this
canonical JSON schema by the caller (a thin adapter per provider):
  {
    "original_message_id": "<abc@mail.gmail.com>",  # Message-ID header
    "sender_email":        "owner@example.com",
    "sender_name":         "John Smith",             # optional
    "subject":             "Re: Introduction",       # optional
    "body_text":           "...",                    # optional
    "body_html":           "<p>...</p>"              # optional
  }

destination_address is derived from the path parameter, not the payload,
to avoid trusting a body field for client routing.

Idempotency: SHA-256(destination_address + original_message_id). A duplicate
delivery is silently accepted with HTTP 200 — the parse service may retry.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import text

from config.settings import get_settings
from src.agents.respond import queue as respond_queue
from src.core.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/inbound", tags=["inbound"])


class InboundEmailPayload(BaseModel):
    original_message_id: str = Field(..., max_length=998)
    sender_email: str = Field(..., max_length=320)
    sender_name: Optional[str] = Field(default=None, max_length=256)
    subject: Optional[str] = Field(default=None, max_length=998)
    body_text: Optional[str] = None
    body_html: Optional[str] = None


def _verify_secret(provided: Optional[str]) -> None:
    """Fail-closed: 503 if not configured, 403 if wrong."""
    settings = get_settings()
    if not settings.inbound_parse_secret:
        raise HTTPException(
            status_code=503,
            detail="Inbound parse secret not configured.",
        )
    expected = settings.inbound_parse_secret.get_secret_value()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid inbound secret.")


def _resolve_client(db, client_id: str) -> bool:
    """Returns True if client_id is an active client under the current RLS context."""
    row = db.execute(
        text("SELECT is_active FROM clients WHERE client_id = :cid"),
        {"cid": client_id},
    ).first()
    return row is not None and bool(row.is_active)


def _idempotency_key(destination_address: str, original_message_id: str) -> str:
    raw = f"{destination_address}:{original_message_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("/reply/{client_id}", status_code=200)
async def receive_inbound_reply(
    client_id: str = Path(..., max_length=40),
    payload: InboundEmailPayload = ...,
    x_blackink_inbound_secret: Optional[str] = Header(default=None),
) -> dict:
    _verify_secret(x_blackink_inbound_secret)

    settings = get_settings()
    domain = getattr(settings, "inbound_email_domain", "getblackink.com")
    destination_address = f"replies@{client_id}.{domain}"
    idem_key = _idempotency_key(destination_address, payload.original_message_id)

    db_obj = Database()
    with db_obj.session_scope(client_id=client_id) as db:
        if not _resolve_client(db, client_id):
            raise HTTPException(status_code=404, detail="Client not found or inactive.")

        # Idempotent insert — duplicate deliveries return 200 silently.
        result = db.execute(
            text(
                "INSERT INTO inbound_messages "
                "(client_id, idempotency_key, destination_address, original_message_id, "
                " sender_email, sender_name, subject, body_text, body_html, status) "
                "VALUES (:client_id, :idem_key, :dest, :orig_msg_id, "
                "        :sender_email, :sender_name, :subject, :body_text, :body_html, 'PENDING') "
                "ON CONFLICT (idempotency_key) DO NOTHING "
                "RETURNING id"
            ),
            {
                "client_id": client_id,
                "idem_key": idem_key,
                "dest": destination_address,
                "orig_msg_id": payload.original_message_id,
                "sender_email": payload.sender_email,
                "sender_name": payload.sender_name,
                "subject": payload.subject,
                "body_text": payload.body_text,
                "body_html": payload.body_html,
            },
        )
        row = result.first()
        db.commit()

    if row is None:
        # Duplicate delivery — already in the DB.
        logger.info(
            "inbound_router: duplicate delivery client_id=%s idem_key=%s — accepted",
            client_id, idem_key,
        )
        return {"status": "accepted", "duplicate": True}

    db_id = row[0]
    stream_id = respond_queue.publish(
        db_id=db_id,
        client_id=client_id,
        idempotency_key=idem_key,
    )

    if stream_id is None:
        # Queue publish failed — row is in DB with status=PENDING.
        # The stale-sweep in the worker picks it up; don't surface this to the parse service.
        logger.error(
            "inbound_router: Redis publish failed for db_id=%s — stale-sweep will recover",
            db_id,
        )

    logger.info(
        "inbound_router: accepted client_id=%s db_id=%s stream_id=%s sender=%s",
        client_id, db_id, stream_id, payload.sender_email,
    )
    return {"status": "accepted", "duplicate": False}
