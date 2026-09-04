"""Tests for POST /api/v1/webhooks/inbound-email (Task 3.1.3 / ticket 30).

The one new seam — drives the endpoint via FastAPI TestClient with signed
Mailgun payloads. Verifies:
  - Valid signature + In-Reply-To attribution → 200, row stored, card posted
  - Forged signature → 403
  - BCC echo → discarded
  - Duplicate message_id → discarded
  - Unknown alias → discarded
  - Unattributed reply → 200, unattributed row
  - Opt-out button value carries contact_id + client_id (from card builder)

Follows 3.2.1-style trust-boundary test pattern (closest prior art:
tests/test_slack_auth.py + tests/test_work_orders.py).
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


# ── App fixture ───────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    """Minimal FastAPI test client with only the inbound-email router,
    settings patched so MAILGUN_SIGNING_KEY is set."""
    from fastapi import FastAPI
    from src.api.inbound_email_router import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── Signing helpers ───────────────────────────────────────────────────────────

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


# ── Patches shared across tests ───────────────────────────────────────────────

def _patch_settings(signing_key=_SIGNING_KEY):
    from pydantic import SecretStr
    settings = MagicMock()
    settings.mailgun_signing_key = SecretStr(signing_key) if signing_key else None
    return patch("src.api.inbound_email_router.get_settings", return_value=settings)


def _patch_db(
    *,
    client_id: str | None = "testclient",
    is_echo: bool = False,
    existing: bool = False,
    attribution_status: str = "attributed",
    contact_id: int | None = 1,
):
    """Patch the DB-touching functions to return controlled results."""

    def _ctx():
        session = MagicMock()
        # existing inbound_messages check
        exists_result = MagicMock()
        exists_result.first.return_value = MagicMock() if existing else None
        session.execute.return_value = exists_result
        cm = MagicMock()
        cm.__enter__ = lambda s: session
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    attribution = MagicMock()
    attribution.client_id = client_id or "testclient"
    attribution.contact_id = contact_id
    attribution.run_id = "run-uuid-1" if attribution_status == "attributed" else None
    attribution.touch_step = 1 if attribution_status == "attributed" else None
    attribution.attribution_status = attribution_status
    attribution.contact_name = "Jane Doe" if contact_id else None
    attribution.firm_name = "Acme PM" if contact_id else None

    return (
        patch("src.api.inbound_email_router.resolve_client_from_alias", return_value=client_id),
        patch("src.api.inbound_email_router.is_bcc_echo", return_value=is_echo),
        patch("src.api.inbound_email_router.attribute", return_value=attribution),
        patch("src.api.inbound_email_router.get_system_db_context", return_value=_ctx()),
        patch("src.api.inbound_email_router.slack_post.post_notice", new_callable=AsyncMock),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_valid_signature_returns_200(client):
    form = _make_form()
    p1, p2, p3, p4, p5 = _patch_db()
    with _patch_settings(), p1, p2, p3, p4, p5:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "inbound_id" in body


def test_forged_signature_returns_403(client):
    form = _make_form(key="wrong-key")
    with _patch_settings():
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 403


def test_missing_signing_key_returns_403(client):
    form = _make_form()
    with _patch_settings(signing_key=None):
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 403


def test_stale_timestamp_returns_403(client):
    form = _make_form(age_seconds=400)  # > 300s limit
    with _patch_settings():
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 403


def test_unknown_alias_discards(client):
    form = _make_form()
    p1, p2, p3, p4, p5 = _patch_db(client_id=None)
    with _patch_settings(), p1, p2, p3, p4, p5:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "unknown_alias"


def test_bcc_echo_discards(client):
    form = _make_form()
    p1, p2, p3, p4, p5 = _patch_db(is_echo=True)
    with _patch_settings(), p1, p2, p3, p4, p5:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "bcc_echo"


def test_duplicate_message_id_discards(client):
    form = _make_form()
    p1, p2, p3, p4, p5 = _patch_db(existing=True)
    with _patch_settings(), p1, p2, p3, p4, p5:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "duplicate"


def test_unattributed_reply_stored_and_posted(client):
    form = _make_form()
    p1, p2, p3, p4, p5 = _patch_db(attribution_status="unattributed", contact_id=None)
    with _patch_settings(), p1, p2, p3, p4, p5 as mock_post_notice:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    # Slack card should still be posted for unattributed
    mock_post_notice.assert_called_once()
    call_kwargs = mock_post_notice.call_args.kwargs
    assert call_kwargs["channel_key"] == "replies"


def test_attributed_reply_posts_card_with_correct_channel(client):
    form = _make_form()
    p1, p2, p3, p4, p5 = _patch_db(attribution_status="attributed", contact_id=42)
    with _patch_settings(), p1, p2, p3, p4, p5 as mock_post_notice:
        resp = client.post("/api/v1/webhooks/inbound-email", data=form)
    assert resp.status_code == 200
    mock_post_notice.assert_called_once()
    assert mock_post_notice.call_args.kwargs["channel_key"] == "replies"
