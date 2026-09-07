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


# -- Subtask 3.2.2 -- Show-Rate Reminder Cascade --------------------------------
# INTERNAL_SALES_DEMO-scope fixtures: a prospective PM firm booking a
# sales-demo call with a Blackink sales rep, under the reserved
# BLACKINK_INTERNAL_SALES client_id -- see booking_ingest.py's module
# docstring and schedule_show_rate_reminders().

_BLACKINK_INTERNAL_SALES = "BLACKINK_INTERNAL_SALES"


@pytest.fixture
def sales_demo_connection():
	# Defensive pre-clean: this client_id + external_calendar_id='rep-primary'
	# namespace is shared with several other live-DB fixtures (no-show,
	# show-rate, meeting-outcome). A row left behind by a prior fixture or a
	# prior interrupted session would otherwise collide with the partial
	# unique index on (client_id, provider, external_calendar_id). Clear all
	# FK-dependent job rows before the bookings, and the bookings before the
	# connections.
	with get_owner_db_context() as session:
		for tbl in ("meeting_outcome_prompt_jobs", "no_show_prompt_jobs", "no_show_recovery_jobs", "booking_reminder_jobs"):
			session.execute(text(
				f"DELETE FROM {tbl} WHERE booking_id IN "
				"(SELECT booking_id FROM bookings WHERE client_id = :cid)"
			), {"cid": _BLACKINK_INTERNAL_SALES})
		session.execute(text(
			"UPDATE contacts SET outbound_pause_source_booking_id = NULL WHERE outbound_pause_source_booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = :cid)"
		), {"cid": _BLACKINK_INTERNAL_SALES})
	with get_system_db_context() as session:
		session.execute(text("DELETE FROM bookings WHERE client_id = :cid"), {"cid": _BLACKINK_INTERNAL_SALES})
		session.execute(text("DELETE FROM calendar_connections WHERE client_id = :cid"), {"cid": _BLACKINK_INTERNAL_SALES})
	with get_system_db_context() as session:
		row = session.execute(
			text(
				"INSERT INTO calendar_connections "
				"(client_id, provider, connection_scope, external_calendar_id, subscription_id, "
				" verification_secret, initial_sync_done) "
				"VALUES (:cid, 'GOOGLE', 'INTERNAL_SALES_DEMO', 'rep-primary', 'sub-sales-demo', 'secret', TRUE) "
				"RETURNING connection_id"
			),
			{"cid": _BLACKINK_INTERNAL_SALES},
		).one()
		connection_id = row.connection_id
	yield connection_id
	with get_owner_db_context() as session:
		# booking_reminder_jobs/no_show_prompt_jobs/no_show_recovery_jobs have
		# no DELETE grant for blackink_app/blackink_system in production (a
		# job row is never deleted, only status-transitioned) -- test cleanup
		# goes through the owner/superuser context instead. All three now
		# FK-reference bookings, so all three must clear before bookings can
		# be deleted below.
		session.execute(text("DELETE FROM booking_reminder_jobs WHERE booking_id IN "
							  "(SELECT booking_id FROM bookings WHERE calendar_connection_id = :id)"), {"id": connection_id})
		session.execute(text("DELETE FROM no_show_prompt_jobs WHERE booking_id IN "
							  "(SELECT booking_id FROM bookings WHERE calendar_connection_id = :id)"), {"id": connection_id})
		session.execute(text("DELETE FROM no_show_recovery_jobs WHERE booking_id IN "
							  "(SELECT booking_id FROM bookings WHERE calendar_connection_id = :id)"), {"id": connection_id})
		session.execute(text("DELETE FROM meeting_outcome_prompt_jobs WHERE booking_id IN "
							  "(SELECT booking_id FROM bookings WHERE calendar_connection_id = :id)"), {"id": connection_id})
		# A test may have paused a contact whose outbound_pause_source_booking_id
		# FK-references one of these bookings (directly, or via trigger_recovery())
		# -- clear it first so the bookings DELETE below doesn't hit that FK.
		session.execute(text(
			"UPDATE contacts SET outbound_pause_source_booking_id = NULL WHERE outbound_pause_source_booking_id IN "
			"(SELECT booking_id FROM bookings WHERE calendar_connection_id = :id)"
		), {"id": connection_id})
	with get_system_db_context() as session:
		session.execute(text("DELETE FROM bookings WHERE calendar_connection_id = :id"), {"id": connection_id})
		session.execute(text("DELETE FROM calendar_connections WHERE connection_id = :id"), {"id": connection_id})
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM events WHERE client_id = :cid"), {"cid": _BLACKINK_INTERNAL_SALES})


@pytest.fixture
def sales_demo_target_contact():
	"""A real companies/contacts row -- the OVS scores companies (PM firms),
	so a sales-demo booking's target is matched against contacts.email,
	never owner_contacts (see booking_ingest.py's _find_sales_demo_target)."""
	with get_owner_db_context() as session:
		company = session.execute(
			text(
				"INSERT INTO companies (company_id, company_name, domain, county_slug, status) "
				"VALUES ('test-sales-demo-co', 'Test Sales Demo PM Co', 'salesdemo-test.example.com', "
				"(SELECT county_slug FROM counties LIMIT 1), 'PROSPECTING') "
				"ON CONFLICT (company_id) DO NOTHING RETURNING company_id"
			)
		).first()
		company_id = company.company_id if company else "test-sales-demo-co"
		session.execute(text("DELETE FROM contacts WHERE email = 'sam@salesdemo-test.example.com'"))
		contact = session.execute(
			text(
				"INSERT INTO contacts (company_id, contact_role_type, first_name, last_name, email, phone) "
				"VALUES (:cid, 'OWNER_BROKER_MD', 'Sam', 'Prospect', 'sam@salesdemo-test.example.com', '+14075551234') "
				"RETURNING contact_id"
			),
			{"cid": company_id},
		).one()
	yield {"company_id": company_id, "contact_id": contact.contact_id, "email": "sam@salesdemo-test.example.com"}
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM contacts WHERE contact_id = :id"), {"id": contact.contact_id})
		session.execute(text("DELETE FROM companies WHERE company_id = :id"), {"id": company_id})


def test_sales_demo_booking_matches_via_contacts_not_owner_contacts(sales_demo_target_contact, sales_demo_connection):
	target = sales_demo_target_contact
	client = _FakeProviderClient(incremental_events=[_tagged_event("evt-sales-demo-match", owner_email=target["email"])])
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		sync_connection_locked(session, sales_demo_connection, client)
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-sales-demo-match'"),
			{"id": sales_demo_connection},
		).one()
		assert booking.status == "MATCHED"
		assert booking.target_company_id == target["company_id"]
		assert booking.target_contact_id == target["contact_id"]
		assert booking.owner_contact_id is None


def test_new_sales_demo_booking_schedules_both_reminder_jobs(sales_demo_target_contact, sales_demo_connection):
	target = sales_demo_target_contact
	scheduled_at = datetime.now(timezone.utc) + timedelta(days=2)
	client = _FakeProviderClient(incremental_events=[
		_tagged_event("evt-schedule", owner_email=target["email"], scheduled_at=scheduled_at)
	])
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		sync_connection_locked(session, sales_demo_connection, client)
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-schedule'"),
			{"id": sales_demo_connection},
		).one()
		jobs = {
			r.reminder_step: r for r in session.execute(
				text("SELECT * FROM booking_reminder_jobs WHERE booking_id = :id"), {"id": booking.booking_id}
			).fetchall()
		}
		assert set(jobs) == {"24h_email", "30min_email"}
		assert jobs["24h_email"].status == "PENDING"
		assert jobs["24h_email"].scheduled_for == scheduled_at - timedelta(hours=24)
		assert jobs["30min_email"].status == "PENDING"
		assert jobs["30min_email"].scheduled_for == scheduled_at - timedelta(minutes=30)


def test_booking_less_than_24h_out_skips_only_the_24h_reminder(sales_demo_target_contact, sales_demo_connection):
	target = sales_demo_target_contact
	scheduled_at = datetime.now(timezone.utc) + timedelta(hours=2)
	client = _FakeProviderClient(incremental_events=[
		_tagged_event("evt-near-term", owner_email=target["email"], scheduled_at=scheduled_at)
	])
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		sync_connection_locked(session, sales_demo_connection, client)
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-near-term'"),
			{"id": sales_demo_connection},
		).one()
		jobs = {
			r.reminder_step: r for r in session.execute(
				text("SELECT * FROM booking_reminder_jobs WHERE booking_id = :id"), {"id": booking.booking_id}
			).fetchall()
		}
		assert jobs["24h_email"].status == "SKIPPED"
		assert jobs["30min_email"].status == "PENDING"


def test_reschedule_updates_pending_reminder_jobs(sales_demo_target_contact, sales_demo_connection):
	target = sales_demo_target_contact
	original_time = datetime.now(timezone.utc) + timedelta(days=3)
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		sync_connection_locked(session, sales_demo_connection, _FakeProviderClient(incremental_events=[
			_tagged_event("evt-resched", owner_email=target["email"], scheduled_at=original_time)
		]))
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-resched'"),
			{"id": sales_demo_connection},
		).one()

		new_time = original_time + timedelta(days=1)
		sync_connection_locked(session, sales_demo_connection, _FakeProviderClient(incremental_events=[
			_tagged_event("evt-resched", owner_email=target["email"], scheduled_at=new_time)
		]))
		jobs = {
			r.reminder_step: r for r in session.execute(
				text("SELECT * FROM booking_reminder_jobs WHERE booking_id = :id"), {"id": booking.booking_id}
			).fetchall()
		}
		assert jobs["24h_email"].scheduled_for == new_time - timedelta(hours=24)
		assert jobs["30min_email"].scheduled_for == new_time - timedelta(minutes=30)
		assert jobs["24h_email"].status == "PENDING"


def test_cancellation_leaves_zero_claimable_reminder_jobs(sales_demo_target_contact, sales_demo_connection):
	target = sales_demo_target_contact
	scheduled_at = datetime.now(timezone.utc) + timedelta(days=2)
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		sync_connection_locked(session, sales_demo_connection, _FakeProviderClient(incremental_events=[
			_tagged_event("evt-cancel-reminders", owner_email=target["email"], scheduled_at=scheduled_at)
		]))
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-cancel-reminders'"),
			{"id": sales_demo_connection},
		).one()

		sync_connection_locked(session, sales_demo_connection, _FakeProviderClient(incremental_events=[
			_tagged_event("evt-cancel-reminders", owner_email=target["email"], status="CANCELLED")
		]))

		claimable = session.execute(
			text(
				"SELECT * FROM booking_reminder_jobs WHERE booking_id = :id "
				"AND status IN ('PENDING', 'FAILED', 'SENDING', 'BLOCKED')"
			),
			{"id": booking.booking_id},
		).fetchall()
		assert claimable == []

		all_jobs = session.execute(
			text("SELECT * FROM booking_reminder_jobs WHERE booking_id = :id"), {"id": booking.booking_id}
		).fetchall()
		assert len(all_jobs) == 2
		assert all(j.status == "CANCELLED" for j in all_jobs)


# ── Subtask 3.2.3 — No-Show Handler ───────────────────────────────────────

def test_no_show_prompt_job_scheduled_for_new_sales_demo_booking(sales_demo_target_contact, sales_demo_connection):
	target = sales_demo_target_contact
	scheduled_at = datetime.now(timezone.utc) + timedelta(days=2)
	client = _FakeProviderClient(incremental_events=[
		_tagged_event("evt-no-show-schedule", owner_email=target["email"], scheduled_at=scheduled_at)
	])
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		sync_connection_locked(session, sales_demo_connection, client)
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-no-show-schedule'"),
			{"id": sales_demo_connection},
		).one()
		job = session.execute(
			text("SELECT * FROM no_show_prompt_jobs WHERE booking_id = :id"), {"id": booking.booking_id}
		).one()
		assert job.status == "PENDING"
		assert job.scheduled_for == scheduled_at


def test_no_show_prompt_job_blocked_when_target_unresolved(sales_demo_connection):
	"""A booking whose work-email doesn't match any real contact never
	gets an actionable 'Mark No-Show' button — see
	schedule_no_show_prompt()'s docstring."""
	scheduled_at = datetime.now(timezone.utc) + timedelta(days=2)
	client = _FakeProviderClient(incremental_events=[
		_tagged_event("evt-unresolved", owner_email="unknown@nowhere.example.com", scheduled_at=scheduled_at)
	])
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		sync_connection_locked(session, sales_demo_connection, client)
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-unresolved'"),
			{"id": sales_demo_connection},
		).one()
		assert booking.target_contact_id is None
		job = session.execute(
			text("SELECT * FROM no_show_prompt_jobs WHERE booking_id = :id"), {"id": booking.booking_id}
		).one()
		assert job.status == "BLOCKED"


def test_resume_clears_pause_only_for_a_distinct_new_booking(sales_demo_target_contact, sales_demo_connection):
	"""Correction: a routine re-sync/re-delivery of the SAME booking that
	caused the pause must never clear it -- only a genuinely new,
	different booking_id (the contact actually rebooking) does.

	contacts is RLS-scoped via companies.owning_client_id, which is NULL
	for this unallocated prospect -- a session scoped to
	client_id='BLACKINK_INTERNAL_SALES' can never read or write that row
	directly (same reason booking_ingest.py goes through
	pause_contact_after_no_show()/resume_contact_if_rebooked() rather than
	a bare UPDATE). This test's own direct read/write of contacts must
	therefore go through get_owner_db_context() (BYPASSRLS), exactly as
	the real pause/resume SECURITY DEFINER functions do internally --
	only the sync_connection_locked() calls themselves run under the
	tenant-scoped session, matching production."""
	target = sales_demo_target_contact
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		sync_connection_locked(session, sales_demo_connection, _FakeProviderClient(incremental_events=[
			_tagged_event("evt-no-show-source", owner_email=target["email"])
		]))
		booking = session.execute(
			text("SELECT * FROM bookings WHERE calendar_connection_id = :id AND external_event_id = 'evt-no-show-source'"),
			{"id": sales_demo_connection},
		).one()

	# Simulate the pause trigger_recovery() would have set (via
	# pause_contact_after_no_show() itself, not a bare UPDATE, so this
	# matches production's write path exactly).
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		session.execute(
			text("SELECT pause_contact_after_no_show(:cid, :bid)"),
			{"cid": target["contact_id"], "bid": booking.booking_id},
		)

	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		# A re-sync of the SAME booking (e.g. an incremental sync
		# re-delivering an unchanged event) must NOT clear the pause.
		sync_connection_locked(session, sales_demo_connection, _FakeProviderClient(incremental_events=[
			_tagged_event("evt-no-show-source", owner_email=target["email"])
		]))
	with get_owner_db_context() as session:
		still_paused = session.execute(
			text("SELECT outbound_paused_at FROM contacts WHERE contact_id = :cid"), {"cid": target["contact_id"]}
		).one()
		assert still_paused.outbound_paused_at is not None

	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		# A genuinely NEW booking (the contact rebooking) DOES clear it.
		sync_connection_locked(session, sales_demo_connection, _FakeProviderClient(incremental_events=[
			_tagged_event("evt-no-show-rebooked", owner_email=target["email"])
		]))
	with get_owner_db_context() as session:
		resumed = session.execute(
			text("SELECT outbound_paused_at, outbound_pause_source_booking_id FROM contacts WHERE contact_id = :cid"),
			{"cid": target["contact_id"]},
		).one()
		assert resumed.outbound_paused_at is None
		assert resumed.outbound_pause_source_booking_id is None
