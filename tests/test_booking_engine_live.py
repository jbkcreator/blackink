"""Live-DB tests for Subtask 3.2.1 — Inbound Booking Engine.

Requires a live Postgres with every migration applied (same workflow as
tests/test_tenant_isolation.py — see CLAUDE.md's "Local development
database" section). Exercises the real Postgres CTE/ON CONFLICT upsert
in src/services/booking_ingest.py, which a FakeSession cannot faithfully
emulate — a plain in-memory fake can't reproduce `xmax = 0`/ON CONFLICT
semantics, so these tests run against real Postgres via a fake
CalendarProviderClient (booking_ingest.sync_connection_locked only
depends on that Protocol, never on any real Google/Microsoft HTTP call),
not a fake DB session.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_db_context, get_owner_db_context, get_system_db_context
from src.services.booking_ingest import (
	FetchResult,
	NormalizedEvent,
	link_booking_to_owner,
	sync_connection_locked,
)
from src.services.calendar_confirmation import claim_confirmations, send_confirmation_for_booking
from tests.fixtures.synthetic_tenants import CANARY_A, CANARY_B, canary_tenants  # noqa: F401


class _FakeProviderClient:
	"""A CalendarProviderClient that returns canned events instead of
	calling Google/Microsoft — booking_ingest.py only depends on this
	Protocol, so the real upsert/classification SQL still runs against
	live Postgres."""

	def __init__(self, baseline_events=None, incremental_events=None, sync_token="token-1"):
		self._baseline_events = baseline_events or []
		self._incremental_events = incremental_events or []
		self._sync_token = sync_token

	def fetch_baseline(self, connection):
		return FetchResult(events=self._baseline_events, sync_token=self._sync_token)

	def fetch_incremental(self, connection):
		return FetchResult(events=self._incremental_events, sync_token=self._sync_token + "-next")


def _tagged_event(event_id, *, owner_email="owner@example.com", status="CONFIRMED", created_at=None, scheduled_at=None):
	now = datetime.now(timezone.utc)
	return NormalizedEvent(
		external_event_id=event_id,
		tagged=True,
		event_status=status,
		scheduled_at=scheduled_at or (now + timedelta(days=1)),
		created_at=created_at or (now - timedelta(days=1)),
		client_rep_name="Rep Name",
		client_rep_email="rep@clientfirm.com",
		owner_name="Jane Owner",
		owner_email=owner_email,
		owner_phone=None,
		raw_payload={"id": event_id, "status": status},
	)


def _untagged_event(event_id):
	e = _tagged_event(event_id)
	e.tagged = False
	return e


@pytest.fixture
def calendar_connection(canary_tenants):
	created = {}
	with get_system_db_context() as session:
		for cid in (CANARY_A, CANARY_B):
			row = session.execute(
				text(
					"INSERT INTO calendar_connections "
					"(client_id, provider, external_calendar_id, subscription_id, verification_secret, "
					" initial_sync_done) "
					"VALUES (:cid, 'GOOGLE', 'primary', :sub, 'secret', TRUE) RETURNING connection_id"
				),
				{"cid": cid, "sub": f"sub-{cid}"},
			).one()
			created[cid] = row.connection_id
	yield created
	with get_system_db_context() as session:
		for cid in (CANARY_A, CANARY_B):
			session.execute(text("DELETE FROM bookings WHERE calendar_connection_id = :id"), {"id": created[cid]})
			session.execute(text("DELETE FROM owner_contacts WHERE client_id = :cid"), {"cid": cid})
			session.execute(text("DELETE FROM calendar_connections WHERE connection_id = :id"), {"id": created[cid]})
	# events is append-only — blackink_app/blackink_system have no DELETE
	# grant on it by design (audit-trail immutability). Same cleanup
	# pattern as test_tenant_isolation.py's _cleanup_compliance_events:
	# go through the owner context instead. sync_connection_locked()
	# writes meeting_booked/booking_cancelled/booking_rescheduled events
	# for this client during the test — the outer canary_tenants fixture's
	# own teardown does DELETE FROM clients, which fails on events' FK if
	# these aren't purged first.
	with get_owner_db_context() as session:
		for cid in (CANARY_A, CANARY_B):
			session.execute(text("DELETE FROM events WHERE client_id = :cid"), {"cid": cid})


def test_untagged_event_never_becomes_a_booking(calendar_connection):
	connection_id = calendar_connection[CANARY_A]
	client = _FakeProviderClient(incremental_events=[_untagged_event("evt-untagged")])
	with get_db_context(client_id=CANARY_A) as session:
		session.execute(
			text("UPDATE calendar_connections SET initial_sync_done = TRUE WHERE connection_id = :id"),
			{"id": connection_id},
		)
		sync_connection_locked(session, connection_id, client)
		row = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-untagged'"),
			{"id": connection_id},
		).first()
		assert row is None


def test_tagged_unmatched_booking_is_queued_for_reconciliation(calendar_connection):
	connection_id = calendar_connection[CANARY_A]
	client = _FakeProviderClient(incremental_events=[_tagged_event("evt-unmatched", owner_email="nomatch@example.com")])
	with get_db_context(client_id=CANARY_A) as session:
		outcome = sync_connection_locked(session, connection_id, client)
		assert outcome.new_bookings == 1
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-unmatched'"),
			{"id": connection_id},
		).one()
		assert booking.status == "PENDING_RECONCILIATION"
		assert booking.owner_contact_id is None
		assert booking.confirmation_status == "PENDING"
		event = session.execute(
			text("SELECT * FROM events WHERE event_type = 'meeting_booked' AND entity_id = :bid"),
			{"bid": str(booking.booking_id)},
		).one()
		assert event.payload["match_status"] == "PENDING_RECONCILIATION"


def test_tagged_matched_booking_links_owner_contact(calendar_connection):
	connection_id = calendar_connection[CANARY_A]
	with get_db_context(client_id=CANARY_A) as session:
		owner_row = session.execute(
			text(
				"INSERT INTO owner_contacts (client_id, full_name, email) "
				"VALUES (:cid, 'Jane Owner', 'match@example.com') RETURNING owner_contact_id"
			),
			{"cid": CANARY_A},
		).one()

		client = _FakeProviderClient(incremental_events=[_tagged_event("evt-matched", owner_email="match@example.com")])
		sync_connection_locked(session, connection_id, client)
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-matched'"),
			{"id": connection_id},
		).one()
		assert booking.status == "MATCHED"
		assert booking.owner_contact_id == owner_row.owner_contact_id


def test_duplicate_notification_is_idempotent(calendar_connection):
	connection_id = calendar_connection[CANARY_A]
	client = _FakeProviderClient(incremental_events=[_tagged_event("evt-dup")])
	with get_db_context(client_id=CANARY_A) as session:
		outcome_1 = sync_connection_locked(session, connection_id, client)
		outcome_2 = sync_connection_locked(session, connection_id, client)
		assert outcome_1.new_bookings == 1
		assert outcome_2.new_bookings == 0
		assert outcome_2.unchanged == 1
		count = session.execute(
			text("SELECT COUNT(*) AS c FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-dup'"),
			{"id": connection_id},
		).one()
		assert count.c == 1


def test_cancellation_by_tenant_scoped_identity(calendar_connection):
	connection_id = calendar_connection[CANARY_A]
	with get_db_context(client_id=CANARY_A) as session:
		sync_connection_locked(session, connection_id, _FakeProviderClient(incremental_events=[_tagged_event("evt-cancel")]))
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-cancel'"),
			{"id": connection_id},
		).one()
		assert booking.confirmation_status == "PENDING"

		cancel_event = _tagged_event("evt-cancel", status="CANCELLED")
		outcome = sync_connection_locked(session, connection_id, _FakeProviderClient(incremental_events=[cancel_event]))
		assert outcome.cancelled == 1
		updated = session.execute(
			text("SELECT * FROM bookings WHERE booking_id = :id"), {"id": booking.booking_id}
		).one()
		assert updated.event_status == "CANCELLED"
		assert updated.confirmation_status == "CANCELLED"


def test_cancellation_of_untracked_event_is_a_no_op(calendar_connection):
	connection_id = calendar_connection[CANARY_A]
	with get_db_context(client_id=CANARY_A) as session:
		outcome = sync_connection_locked(
			session, connection_id, _FakeProviderClient(incremental_events=[_tagged_event("evt-never-seen", status="CANCELLED")])
		)
		assert outcome.cancelled == 0


def test_baseline_sync_suppresses_confirmation_for_preexisting_event(calendar_connection):
	connection_id = calendar_connection[CANARY_A]
	with get_db_context(client_id=CANARY_A) as session:
		session.execute(
			text("UPDATE calendar_connections SET initial_sync_done = FALSE WHERE connection_id = :id"),
			{"id": connection_id},
		)
		boundary = datetime.now(timezone.utc)
		old_event = _tagged_event("evt-baseline-old", created_at=boundary - timedelta(days=2))
		outcome = sync_connection_locked(
			session, connection_id, _FakeProviderClient(baseline_events=[old_event]), connect_boundary=boundary
		)
		assert outcome.baseline is True
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-baseline-old'"),
			{"id": connection_id},
		).one()
		assert booking.confirmation_status == "NOT_REQUIRED"


def test_baseline_event_created_after_boundary_is_not_suppressed(calendar_connection):
	"""The initialization-boundary fix: an event created during the connect
	race window (on/after connect_initiated_at) must not be silently
	swallowed as baseline, even though it arrives via the same fetch."""
	connection_id = calendar_connection[CANARY_A]
	with get_db_context(client_id=CANARY_A) as session:
		session.execute(
			text("UPDATE calendar_connections SET initial_sync_done = FALSE WHERE connection_id = :id"),
			{"id": connection_id},
		)
		boundary = datetime.now(timezone.utc)
		race_window_event = _tagged_event("evt-baseline-race", created_at=boundary + timedelta(seconds=1))
		sync_connection_locked(
			session, connection_id, _FakeProviderClient(baseline_events=[race_window_event]), connect_boundary=boundary
		)
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-baseline-race'"),
			{"id": connection_id},
		).one()
		assert booking.confirmation_status == "PENDING"


def test_link_booking_to_owner_rejects_cross_tenant_link(calendar_connection):
	with get_system_db_context() as session:
		owner_b = session.execute(
			text(
				"INSERT INTO owner_contacts (client_id, full_name, email) "
				"VALUES (:cid, 'Cross Tenant Owner', 'cross@example.com') RETURNING owner_contact_id"
			),
			{"cid": CANARY_B},
		).one()
		booking_a = session.execute(
			text(
				"INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id, "
				"event_status, raw_payload) VALUES (:cid, 'GOOGLE', :conn, 'evt-cross', 'CONFIRMED', '{}') "
				"RETURNING booking_id"
			),
			{"cid": CANARY_A, "conn": calendar_connection[CANARY_A]},
		).one()

		with pytest.raises(ValueError):
			link_booking_to_owner(session, booking_a.booking_id, owner_b.owner_contact_id)


def test_link_booking_to_owner_resets_missing_email_failure_to_pending(calendar_connection):
	"""PR #17 review finding 4: a booking whose confirmation attempt failed
	because it had no owner_contact yet (FAILED_PERMANENT, set the instant
	owner_email is NULL — see send_confirmation_for_booking) must become
	claimable again once reconciliation supplies a real owner, or the
	confirmation email is lost forever (claim_confirmations never reclaims
	FAILED_PERMANENT)."""
	connection_id = calendar_connection[CANARY_A]
	with get_db_context(client_id=CANARY_A) as session:
		booking = session.execute(
			text(
				"INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id, "
				"event_status, confirmation_status, confirmation_last_error, confirmation_attempts, raw_payload) "
				"VALUES (:cid, 'GOOGLE', :conn, 'evt-reconcile-missing-email', 'CONFIRMED', 'FAILED_PERMANENT', "
				"'no owner_contact email on file', 1, '{}') RETURNING booking_id"
			),
			{"cid": CANARY_A, "conn": connection_id},
		).one()
		owner = session.execute(
			text(
				"INSERT INTO owner_contacts (client_id, full_name, email) "
				"VALUES (:cid, 'Reconciled Owner', 'reconciled@example.com') RETURNING owner_contact_id"
			),
			{"cid": CANARY_A},
		).one()

		link_booking_to_owner(session, booking.booking_id, owner.owner_contact_id)

		final = session.execute(text("SELECT * FROM bookings WHERE booking_id = :id"), {"id": booking.booking_id}).one()
		assert final.confirmation_status == "PENDING"
		assert final.confirmation_attempts == 0
		assert final.confirmation_last_error is None
		assert final.owner_contact_id == owner.owner_contact_id


def test_link_booking_to_owner_does_not_resurrect_other_failure_reasons(calendar_connection):
	"""A FAILED_PERMANENT booking from a genuine provider error (max
	attempts exhausted) must NOT be silently reset just because an owner
	got matched later — only the specific missing-owner-email reason this
	fix targets is resurrected."""
	connection_id = calendar_connection[CANARY_A]
	with get_db_context(client_id=CANARY_A) as session:
		booking = session.execute(
			text(
				"INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id, "
				"event_status, confirmation_status, confirmation_last_error, confirmation_attempts, raw_payload) "
				"VALUES (:cid, 'GOOGLE', :conn, 'evt-reconcile-real-failure', 'CONFIRMED', 'FAILED_PERMANENT', "
				"'SMTP 550 mailbox unavailable', 5, '{}') RETURNING booking_id"
			),
			{"cid": CANARY_A, "conn": connection_id},
		).one()
		owner = session.execute(
			text(
				"INSERT INTO owner_contacts (client_id, full_name, email) "
				"VALUES (:cid, 'Reconciled Owner Two', 'reconciled2@example.com') RETURNING owner_contact_id"
			),
			{"cid": CANARY_A},
		).one()

		link_booking_to_owner(session, booking.booking_id, owner.owner_contact_id)

		final = session.execute(text("SELECT * FROM bookings WHERE booking_id = :id"), {"id": booking.booking_id}).one()
		assert final.confirmation_status == "FAILED_PERMANENT"
		assert final.confirmation_attempts == 5
		assert final.owner_contact_id == owner.owner_contact_id


def test_link_booking_to_owner_does_not_touch_not_required(calendar_connection):
	"""A baseline-suppressed booking (NOT_REQUIRED, permanent by design —
	pre-dates the connection) must stay NOT_REQUIRED after reconciliation,
	never resurrected into the confirmation queue."""
	connection_id = calendar_connection[CANARY_A]
	with get_db_context(client_id=CANARY_A) as session:
		booking = session.execute(
			text(
				"INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id, "
				"event_status, confirmation_status, raw_payload) "
				"VALUES (:cid, 'GOOGLE', :conn, 'evt-reconcile-not-required', 'CONFIRMED', 'NOT_REQUIRED', '{}') "
				"RETURNING booking_id"
			),
			{"cid": CANARY_A, "conn": connection_id},
		).one()
		owner = session.execute(
			text(
				"INSERT INTO owner_contacts (client_id, full_name, email) "
				"VALUES (:cid, 'Reconciled Owner Three', 'reconciled3@example.com') RETURNING owner_contact_id"
			),
			{"cid": CANARY_A},
		).one()

		link_booking_to_owner(session, booking.booking_id, owner.owner_contact_id)

		final = session.execute(text("SELECT * FROM bookings WHERE booking_id = :id"), {"id": booking.booking_id}).one()
		assert final.confirmation_status == "NOT_REQUIRED"


def test_calendar_connections_and_bookings_are_tenant_isolated(calendar_connection):
	with get_db_context(client_id=CANARY_A) as session:
		sync_connection_locked(session, calendar_connection[CANARY_A], _FakeProviderClient(incremental_events=[_tagged_event("evt-isolated")]))

	with get_db_context(client_id=CANARY_B) as session:
		rows = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id"), {"id": calendar_connection[CANARY_A]}
		).fetchall()
		assert rows == []
		conn_rows = session.execute(
			text("SELECT * FROM calendar_connections WHERE connection_id = :id"), {"id": calendar_connection[CANARY_A]}
		).fetchall()
		assert conn_rows == []


class _FakeEmailProvider:
	def __init__(self):
		self.sent = []

	def send(self, to, reply_to, bcc, subject, html_body, ics_attachment):
		self.sent.append(to)
		return "fake-message-id"


def test_confirmation_claim_and_send_marks_sent(calendar_connection, monkeypatch):
	connection_id = calendar_connection[CANARY_A]
	with get_db_context(client_id=CANARY_A) as session:
		session.execute(
			text(
				"INSERT INTO owner_contacts (client_id, full_name, email) "
				"VALUES (:cid, 'Jane Owner', 'confirm@example.com')"
			),
			{"cid": CANARY_A},
		)
		sync_connection_locked(
			session, connection_id, _FakeProviderClient(incremental_events=[_tagged_event("evt-confirm", owner_email="confirm@example.com")])
		)
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-confirm'"),
			{"id": connection_id},
		).one()
		assert booking.confirmation_status == "PENDING"

	with get_system_db_context() as session:
		from config.settings import get_settings
		from src.services import email_dispatch

		monkeypatch.setattr(get_settings(), "email_sending_enabled", True)
		fake_provider = _FakeEmailProvider()
		monkeypatch.setattr(
			email_dispatch, "_resolve_mailbox",
			lambda s, cid: type("M", (), {
				"spf_validated": True, "dkim_validated": True, "dmarc_validated": True,
				"smtp_host": "x", "smtp_port": 587, "smtp_username": "x",
				"smtp_password_encrypted": "unused-because-decrypt_token-is-mocked-below",
				"mailbox_address": "noreply@getblackink.com",
			})(),
		)
		# decrypt_token() is called on the fake mailbox's password before
		# SmtpEmailProvider is even constructed — mock it too, since the
		# fake password above isn't a real Fernet token.
		monkeypatch.setattr(email_dispatch, "decrypt_token", lambda ciphertext: "unused")
		monkeypatch.setattr(
			email_dispatch, "SmtpEmailProvider", lambda **kwargs: fake_provider
		)

		claimed = claim_confirmations(session, booking_ids=[booking.booking_id])
		assert claimed == [booking.booking_id]
		send_confirmation_for_booking(session, booking.booking_id)

		final = session.execute(text("SELECT * FROM bookings WHERE booking_id = :id"), {"id": booking.booking_id}).one()
		assert final.confirmation_status == "SENT"
		assert fake_provider.sent == ["confirm@example.com"]
