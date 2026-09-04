"""Tests for POST /api/v1/webhooks/inbound-email (Task 3.1.3 / ticket 30).

The router is now a thin HTTP trust boundary: it verifies the Mailgun HMAC
signature, parses the form, and delegates to
src.services.inbound_ingest.ingest_inbound_reply. These tests cover exactly
that boundary — signature acceptance/rejection and delegation. The ingestion
logic (attribution, dedup, discard reasons) is covered in
tests/test_inbound_ingest.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from src.api.inbound_email_router import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_SIGNING_KEY = "test-mailgun-signing-key-for-tests"


def _sign(timestamp: str, token: str, key: str = _SIGNING_KEY) -> str:
    return hmac.new(
        key=key.encode("utf-8"),
        msg=(timestamp + token).encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _make_form(
    *,
    from_addr: str = "prospect@example.com",
    recipient: str = "testclient@inbound.getblackink.com",
    subject: str = "Re: Your Visibility Report",
    body: str = "Thanks for reaching out!",
    in_reply_to: str | None = "<msg-id-123@mail.example.com>",
    message_id: str | None = None,
    age_seconds: int = 10,
    key: str = _SIGNING_KEY,
) -> dict:
    ts = str(int(time.time()) - age_seconds)
    token = str(uuid.uuid4()).replace("-", "")
    sig = _sign(ts, token, key)
    msg_id = message_id or f"<inbound-{uuid.uuid4()}@mail.example.com>"
    headers = [["Message-Id", msg_id]]
    if in_reply_to:
        headers.append(["In-Reply-To", in_reply_to])
    return {
        "timestamp": ts,
        "token": token,
        "signature": sig,
        "from": from_addr,
        "recipient": recipient,
        "subject": subject,
        "body-plain": body,
        "message-headers": json.dumps(headers),
    }


def _patch_settings(signing_key=_SIGNING_KEY):
    from pydantic import SecretStr
    settings = MagicMock()
    settings.mailgun_signing_key = SecretStr(signing_key) if signing_key else None
    return patch("src.api.inbound_email_router.get_settings", return_value=settings)


def _patch_ingest(return_value=None):
    rv = return_value or {"status": "ok", "inbound_id": "inbound-uuid-1"}
    return patch(
        "src.api.inbound_email_router.ingest_inbound_reply",
        new_callable=AsyncMock,
        return_value=rv,
    )


def test_valid_signature_delegates_and_returns_200(client):
    form = _make_form()
    with _patch_settings(), _patch_ingest() as mock_ingest:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "inbound_id": "inbound-uuid-1"}
    mock_ingest.assert_awaited_once()
    # The parsed payload carries the fields the router extracted.
    parsed = mock_ingest.await_args.args[0]
    assert parsed.to_alias == "testclient@inbound.getblackink.com"
    assert parsed.from_raw == "prospect@example.com"
    assert parsed.in_reply_to == "<msg-id-123@mail.example.com>"


def test_forged_signature_returns_403_and_does_not_delegate(client):
    form = _make_form(key="wrong-key")
    with _patch_settings(), _patch_ingest() as mock_ingest:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 403
    mock_ingest.assert_not_awaited()


def test_missing_signing_key_returns_403(client):
    form = _make_form()
    with _patch_settings(signing_key=None), _patch_ingest() as mock_ingest:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 403
    mock_ingest.assert_not_awaited()


def test_old_timestamp_still_accepted(client):
    """The 300s freshness gate was removed — a legit Mailgun retry with an old
    timestamp but valid signature must still be delegated (not 403)."""
    form = _make_form(age_seconds=6000)  # well past the old 300s window
    with _patch_settings(), _patch_ingest() as mock_ingest:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 200
    mock_ingest.assert_awaited_once()


def test_router_returns_service_discard_verbatim(client):
    form = _make_form()
    with _patch_settings(), _patch_ingest(return_value={"status": "discarded", "reason": "bcc_echo"}):
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "bcc_echo"


def test_missing_message_id_header_generates_one(client):
    form = _make_form()
    form["message-headers"] = json.dumps([["Subject", "hi"]])  # no Message-Id
    with _patch_settings(), _patch_ingest() as mock_ingest:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 200
    parsed = mock_ingest.await_args.args[0]
    assert parsed.inbound_message_id.startswith("<generated-")
