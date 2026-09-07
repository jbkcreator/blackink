"""Live-DB tests for the OAuth callback's calendar_connections row —
regression coverage for PR #17 review findings 1 and 3:

1. Microsoft's persisted subscription_id must be Graph's own server-issued
   id (sub_result["subscription_id"]), not the locally generated
   channel_id — otherwise resolve_calendar_connection() can never match a
   real Graph webhook notification.
3. Google's expires_at must be persisted from register_watch()'s returned
   expiration, not hardcoded NULL — otherwise every connection looks
   permanently "due" to the renewal sweep.

Exercises oauth_callback() directly (not over HTTP) with its provider
HTTP calls monkeypatched out — booking_ingest.sync_connection_locked is
also stubbed since baseline sync isn't what these tests are about.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.api import calendar_oauth_router
from src.core.database import get_system_db_context
from src.services import calendar_oauth
from src.services.calendar_oauth import ConnectLinkClaims, TokenExchangeResult
from src.services.calendar_providers import GoogleCalendarClient, MicrosoftGraphClient
from tests.fixtures.synthetic_tenants import CANARY_A, canary_tenants  # noqa: F401


def _patch_common(monkeypatch, *, provider: str):
    monkeypatch.setattr(
        calendar_oauth_router.calendar_oauth,
        "verify_state",
        lambda state: ConnectLinkClaims(client_id=CANARY_A, provider=provider, nonce="n"),
    )
    monkeypatch.setattr(
        calendar_oauth_router.calendar_oauth,
        "exchange_code_for_tokens",
        lambda provider, code, redirect_uri: TokenExchangeResult(
            access_token="fake-access-token", refresh_token="fake-refresh-token",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    monkeypatch.setattr(calendar_oauth_router, "sync_connection_locked", lambda *a, **k: None)


def _cleanup(client_id: str):
    with get_system_db_context() as session:
        session.execute(text("DELETE FROM bookings WHERE client_id = :cid"), {"cid": client_id})
        session.execute(text("DELETE FROM calendar_connections WHERE client_id = :cid"), {"cid": client_id})


def test_microsoft_callback_persists_graph_subscription_id_not_channel_id(monkeypatch, canary_tenants):
    _patch_common(monkeypatch, provider="MICROSOFT")

    class _FakeMeCalendarResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "graph-calendar-id"}

    monkeypatch.setattr(calendar_oauth_router.requests, "get", lambda *a, **k: _FakeMeCalendarResponse())
    monkeypatch.setattr(
        MicrosoftGraphClient, "register_subscription",
        lambda self, connection, webhook_url, verification_secret: {
            "subscription_id": "graph-issued-subscription-id",
            "expires_at": datetime.now(timezone.utc) + timedelta(days=3),
        },
    )

    try:
        result = calendar_oauth_router.oauth_callback("microsoft", code="fake-code", state="fake-state")
        assert result["status"] == "connected"

        with get_system_db_context() as session:
            row = session.execute(
                text("SELECT subscription_id FROM calendar_connections WHERE connection_id = :id"),
                {"id": result["connection_id"]},
            ).one()
        assert row.subscription_id == "graph-issued-subscription-id"
    finally:
        _cleanup(CANARY_A)


def test_google_callback_persists_watch_expiration(monkeypatch, canary_tenants):
    _patch_common(monkeypatch, provider="GOOGLE")

    expires_ms = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp() * 1000)
    monkeypatch.setattr(
        GoogleCalendarClient, "register_watch",
        lambda self, connection, channel_id, webhook_url, verification_secret: {
            "resource_id": "google-resource-id",
            "expires_at_ms": str(expires_ms),
        },
    )

    try:
        result = calendar_oauth_router.oauth_callback("google", code="fake-code", state="fake-state")
        assert result["status"] == "connected"

        with get_system_db_context() as session:
            row = session.execute(
                text("SELECT expires_at FROM calendar_connections WHERE connection_id = :id"),
                {"id": result["connection_id"]},
            ).one()
        assert row.expires_at is not None
        assert row.expires_at > datetime.now(timezone.utc) + timedelta(days=2)
    finally:
        _cleanup(CANARY_A)
