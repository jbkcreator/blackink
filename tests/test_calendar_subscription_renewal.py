"""Live-DB regression tests for calendar_subscription_renewal.

Covers two production-outage bugs:
  * GoHighLevel connections (no OAuth, expires_at permanently NULL) must NOT
    be selected by the renewal sweep — otherwise they route to the Microsoft
    Graph branch, fail for lack of credentials, and get flipped to
    NEEDS_RECONNECT, after which the webhook resolver rejects real GHL
    bookings.
  * Google connections must have their returned watch expiry persisted, so a
    fresh connection is not re-selected (a new watch channel created) on
    every hourly sweep.

Provider clients are faked so no real Google/Microsoft HTTP call is made.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context
from src.tasks import calendar_subscription_renewal as renewal

_CLIENT = "BLACKINK_INTERNAL_SALES"


class _FakeGoogle:
	def __init__(self, session):
		self.session = session

	def register_watch(self, connection, channel_id, webhook_url, verification_secret):
		# Real epoch-ms string, as Google's API returns — the caller converts
		# it via calendar_providers.expires_at_ms_to_datetime(), so a dummy
		# value here would persist a 1970 expiry and look permanently due.
		expires_ms = int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp() * 1000)
		return {"resource_id": "res-1", "expires_at_ms": str(expires_ms)}


class _FakeMicrosoft:
	constructed = 0

	def __init__(self, session):
		_FakeMicrosoft.constructed += 1

	def register_subscription(self, connection, webhook_url, verification_secret):
		return {"subscription_id": "sub-ms", "expires_at": datetime.now(timezone.utc) + timedelta(days=2)}


@pytest.fixture(autouse=True)
def _fake_clients(monkeypatch):
	monkeypatch.setattr(renewal, "GoogleCalendarClient", _FakeGoogle)
	monkeypatch.setattr(renewal, "MicrosoftGraphClient", _FakeMicrosoft)
	# The sweep no-ops entirely when the skip flag is set; force it off so the
	# selection/branching logic under test actually runs.
	monkeypatch.setattr(renewal.get_settings(), "skip_calendar_watch_registration", False)
	_FakeMicrosoft.constructed = 0


def _mk_connection(provider, *, expires_at, ext):
	with get_system_db_context() as s:
		return s.execute(text(
			"INSERT INTO calendar_connections "
			"(client_id, provider, external_calendar_id, subscription_id, verification_secret, "
			" initial_sync_done, status, expires_at) "
			"VALUES (:c, :p, :ext, 'sub0', 'secret', TRUE, 'ACTIVE', :exp) RETURNING connection_id"
		), {"c": _CLIENT, "p": provider, "ext": ext, "exp": expires_at}).scalar()


def _cleanup(ext_prefix):
	with get_system_db_context() as s:
		s.execute(text("DELETE FROM calendar_connections WHERE client_id = :c AND external_calendar_id LIKE :e"),
				  {"c": _CLIENT, "e": ext_prefix + "%"})


def test_ghl_connection_is_not_selected_or_disabled():
	cid = _mk_connection("GOHIGHLEVEL", expires_at=None, ext="renewal-ghl")
	try:
		renewal.run_renewal_sweep()
		with get_owner_db_context() as s:
			row = s.execute(text("SELECT status, expires_at FROM calendar_connections WHERE connection_id = :id"),
							{"id": cid}).one()
		assert row.status == "ACTIVE", "GHL connection must never be flipped to NEEDS_RECONNECT"
		assert row.expires_at is None, "GHL connection must be left untouched by the renewal sweep"
	finally:
		_cleanup("renewal-ghl")


def test_google_connection_persists_returned_expiry_and_is_not_reselected():
	# expires_at NULL => due on the first sweep.
	cid = _mk_connection("GOOGLE", expires_at=None, ext="renewal-goog")
	try:
		renewal.run_renewal_sweep()
		with get_owner_db_context() as s:
			expires_at = s.execute(text("SELECT expires_at FROM calendar_connections WHERE connection_id = :id"),
								   {"id": cid}).scalar()
		assert expires_at is not None, "Google watch expiry must be persisted after renewal"
		assert expires_at > datetime.now(timezone.utc) + timedelta(days=1)

		# Now that a real future expiry is stored, a second sweep must NOT
		# select it again (no new watch channel every tick).
		due_ids = _due_connection_ids()
		assert cid not in due_ids
	finally:
		_cleanup("renewal-goog")


def _due_connection_ids():
	from datetime import datetime as _dt
	with get_system_db_context() as s:
		rows = s.execute(text(
			"SELECT connection_id FROM calendar_connections "
			"WHERE status = 'ACTIVE' AND provider IN ('GOOGLE', 'MICROSOFT') "
			"AND (expires_at IS NULL OR expires_at <= :cutoff)"
		), {"cutoff": _dt.now(timezone.utc) + renewal._RENEW_WITHIN}).fetchall()
	return {r.connection_id for r in rows}


# ── PR #17's own regression coverage for the same two bugs, kept alongside
# the above (which arrived via PR #18) — same behaviors, but exercised
# through the repo's established canary_tenants fixture rather than the
# reserved BLACKINK_INTERNAL_SALES client. Both were written independently
# on the two branches; neither is dropped in the merge.
#
# Live-DB tests for src/tasks/calendar_subscription_renewal.py —
# regression coverage for PR #17 review findings 2 and 3:
#
# 2. A GoHighLevel connection (expires_at always NULL — no push
#    subscription to renew) must never be selected as a renewal candidate;
#    previously it fell into the Microsoft branch, failed, and was marked
#    NEEDS_RECONNECT, killing the webhook within one sweep tick.
# 3. A Google connection's expires_at must be persisted from the renewed
#    watch's real expiration, not left NULL — otherwise it's "due" again
#    on every subsequent tick.

from src.services.calendar_providers import (  # noqa: E402
    GoogleCalendarClient,
    MicrosoftGraphClient,
)
from src.tasks import calendar_subscription_renewal  # noqa: E402
from tests.fixtures.synthetic_tenants import CANARY_A, canary_tenants  # noqa: F401,E402


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
