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
		return {
			"resource_id": "res-1",
			"expires_at_ms": "1",
			"expires_at": datetime.now(timezone.utc) + timedelta(days=7),
		}


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
