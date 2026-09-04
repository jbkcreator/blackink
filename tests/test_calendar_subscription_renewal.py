"""Live-DB tests for src/tasks/calendar_subscription_renewal.py —
regression coverage for PR #17 review findings 2 and 3:

2. A GoHighLevel connection (expires_at always NULL — no push
   subscription to renew) must never be selected as a renewal candidate;
   previously it fell into the Microsoft branch, failed, and was marked
   NEEDS_RECONNECT, killing the webhook within one sweep tick.
3. A Google connection's expires_at must be persisted from the renewed
   watch's real expiration, not left NULL — otherwise it's "due" again
   on every subsequent tick.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.calendar_providers import GoogleCalendarClient, MicrosoftGraphClient
from src.tasks import calendar_subscription_renewal
from tests.fixtures.synthetic_tenants import CANARY_A, canary_tenants  # noqa: F401


def _insert_connection(session, *, provider: str, expires_at=None) -> int:
    row = session.execute(
        text(
            "INSERT INTO calendar_connections "
            "(client_id, provider, external_calendar_id, subscription_id, verification_secret, "
            " access_token_encrypted, refresh_token_encrypted, token_expires_at, expires_at, status) "
            "VALUES (:cid, :provider, 'cal-1', :sub, 'secret', 'enc-access', 'enc-refresh', "
            " :token_expires_at, :expires_at, 'ACTIVE') RETURNING connection_id"
        ),
        {
            "cid": CANARY_A, "provider": provider, "sub": f"sub-{provider}",
            "token_expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            "expires_at": expires_at,
        },
    ).one()
    return row.connection_id


def test_renewal_sweep_never_selects_gohighlevel_connections(monkeypatch, canary_tenants):
    monkeypatch.setattr(calendar_subscription_renewal.get_settings(), "skip_calendar_watch_registration", False)

    def _fail(*a, **k):
        raise AssertionError("GHL connection must never reach a provider client")

    monkeypatch.setattr(MicrosoftGraphClient, "register_subscription", _fail)
    monkeypatch.setattr(GoogleCalendarClient, "register_watch", _fail)

    with get_system_db_context() as session:
        ghl_id = _insert_connection(session, provider="GOHIGHLEVEL", expires_at=None)

    try:
        calendar_subscription_renewal.run_renewal_sweep()

        with get_system_db_context() as session:
            row = session.execute(
                text("SELECT status, subscription_id FROM calendar_connections WHERE connection_id = :id"),
                {"id": ghl_id},
            ).one()
        assert row.status == "ACTIVE"
        assert row.subscription_id == "sub-GOHIGHLEVEL"
    finally:
        with get_system_db_context() as session:
            session.execute(text("DELETE FROM calendar_connections WHERE connection_id = :id"), {"id": ghl_id})


def test_renewal_sweep_persists_google_watch_expiration(monkeypatch, canary_tenants):
    monkeypatch.setattr(calendar_subscription_renewal.get_settings(), "skip_calendar_watch_registration", False)

    new_expires_ms = int((datetime.now(timezone.utc) + timedelta(days=3)).timestamp() * 1000)
    monkeypatch.setattr(
        GoogleCalendarClient, "register_watch",
        lambda self, connection, channel_id, webhook_url, verification_secret: {
            "resource_id": "google-resource-id", "expires_at_ms": str(new_expires_ms),
        },
    )

    with get_system_db_context() as session:
        google_id = _insert_connection(session, provider="GOOGLE", expires_at=None)

    try:
        renewed = calendar_subscription_renewal.run_renewal_sweep()
        assert renewed >= 1

        with get_system_db_context() as session:
            row = session.execute(
                text("SELECT status, expires_at FROM calendar_connections WHERE connection_id = :id"),
                {"id": google_id},
            ).one()
        assert row.status == "ACTIVE"
        assert row.expires_at is not None
        assert row.expires_at > datetime.now(timezone.utc) + timedelta(days=2)

        # A second sweep immediately after must NOT treat this connection as
        # due again — the persisted expires_at is well outside _RENEW_WITHIN.
        def _fail_if_called(*a, **k):
            raise AssertionError("connection should not be re-renewed immediately after a successful renewal")

        monkeypatch.setattr(GoogleCalendarClient, "register_watch", _fail_if_called)
        renewed_again = calendar_subscription_renewal.run_renewal_sweep()
        assert renewed_again == 0
    finally:
        with get_system_db_context() as session:
            session.execute(text("DELETE FROM calendar_connections WHERE connection_id = :id"), {"id": google_id})
