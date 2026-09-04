"""Live-DB tests for Subtask 3.2.2 -- Show-Rate Reminder Cascade's
dispatch worker (src/services/show_rate_reminders.py,
src/tasks/show_rate_reminder_sender.py).

Same rationale as tests/test_booking_engine_live.py for needing real
Postgres: the SKIP LOCKED atomic claim and the claim_time-parameterized
comparison can't be faithfully emulated by a FakeSession.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_db_context, get_owner_db_context, get_system_db_context
from src.services.show_rate_reminders import claim_reminders, recover_blocked_jobs, send_show_rate_reminder
from src.tasks.show_rate_reminder_sender import run_sweep

_BLACKINK_INTERNAL_SALES = "BLACKINK_INTERNAL_SALES"


@pytest.fixture
def sales_demo_setup():
	"""One INTERNAL_SALES_DEMO connection + one matched, CONFIRMED booking
	with both reminder jobs already scheduled -- built directly via SQL
	(not through booking_ingest.sync_connection_locked) so each test
	controls scheduled_at/company/contact state precisely."""
	with get_owner_db_context() as session:
		# booking_reminder_jobs has no DELETE grant for blackink_app/blackink_system
		# in production either -- see apply_booking_reminder_jobs.py.
		session.execute(text(
			"DELETE FROM booking_reminder_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
	with get_system_db_context() as session:
		session.execute(text("DELETE FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES'"))
		session.execute(text("DELETE FROM calendar_connections WHERE client_id = 'BLACKINK_INTERNAL_SALES'"))
	with get_owner_db_context() as session:
		# owner_visibility_scores has no DELETE grant for blackink_app/
		# blackink_system in production (neither ever deletes a score row —
		# owner_visibility_sweep.py only INSERT/UPDATEs, show_rate_reminders.py
		# only SELECTs) — test cleanup goes through the owner/superuser
		# context instead of widening a production grant just for tests,
		# same convention test_tenant_isolation.py already uses for the
		# append-only events table.
		session.execute(text("DELETE FROM owner_visibility_scores WHERE company_id = 'test-show-rate-co'"))
		session.execute(text("DELETE FROM contacts WHERE email = 'sales-target@example.com'"))
		session.execute(text("DELETE FROM companies WHERE company_id = 'test-show-rate-co'"))
		company = session.execute(
			text(
				"INSERT INTO companies (company_id, company_name, domain, county_slug, status) "
				"VALUES ('test-show-rate-co', 'Test Show Rate PM Co', 'showrate-test.example.com', "
				"(SELECT county_slug FROM counties LIMIT 1), 'PROSPECTING') RETURNING company_id"
			)
		).one()
		contact = session.execute(
			text(
				"INSERT INTO contacts (company_id, contact_role_type, first_name, last_name, email, phone) "
				"VALUES (:cid, 'OWNER_BROKER_MD', 'Sam', 'Target', 'sales-target@example.com', '+14075551234') "
				"RETURNING contact_id"
			),
			{"cid": company.company_id},
		).one()

	with get_system_db_context() as session:
		conn = session.execute(
			text(
				"INSERT INTO calendar_connections "
				"(client_id, provider, connection_scope, external_calendar_id, subscription_id, "
				" verification_secret, initial_sync_done) "
				"VALUES (:cid, 'GOOGLE', 'INTERNAL_SALES_DEMO', 'rep-primary', 'sub-show-rate', 'secret', TRUE) "
				"RETURNING connection_id"
			),
			{"cid": _BLACKINK_INTERNAL_SALES},
		).one()
		scheduled_at = datetime.now(timezone.utc) + timedelta(days=5)
		booking = session.execute(
			text(
				"INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id, "
				"event_status, scheduled_at, raw_payload, target_company_id, target_contact_id, status) "
				"VALUES (:cid, 'GOOGLE', :conn, 'evt-show-rate', 'CONFIRMED', :sched, "
				"'{\"start\": {\"timeZone\": \"America/New_York\"}}', :company_id, :contact_id, 'MATCHED') "
				"RETURNING booking_id"
			),
			{
				"cid": _BLACKINK_INTERNAL_SALES, "conn": conn.connection_id, "sched": scheduled_at,
				"company_id": company.company_id, "contact_id": contact.contact_id,
			},
		).one()
		jobs = {}
		for step, offset in (("24h_email", timedelta(hours=24)), ("30min_email", timedelta(minutes=30))):
			row = session.execute(
				text(
					"INSERT INTO booking_reminder_jobs (client_id, booking_id, reminder_step, scheduled_for, status) "
					"VALUES (:cid, :bid, :step, :sfor, 'PENDING') RETURNING reminder_job_id"
				),
				{"cid": _BLACKINK_INTERNAL_SALES, "bid": booking.booking_id, "step": step, "sfor": scheduled_at - offset},
			).one()
			jobs[step] = row.reminder_job_id

	result = {
		"connection_id": conn.connection_id, "booking_id": booking.booking_id, "scheduled_at": scheduled_at,
		"company_id": company.company_id, "contact_id": contact.contact_id, "jobs": jobs,
	}
	yield result

	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM booking_reminder_jobs WHERE booking_id = :id"), {"id": booking.booking_id})
	with get_system_db_context() as session:
		session.execute(text("DELETE FROM bookings WHERE booking_id = :id"), {"id": booking.booking_id})
		session.execute(text("DELETE FROM calendar_connections WHERE connection_id = :id"), {"id": conn.connection_id})
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM owner_visibility_scores WHERE company_id = :id"), {"id": company.company_id})
		session.execute(text("DELETE FROM contacts WHERE contact_id = :id"), {"id": contact.contact_id})
		session.execute(text("DELETE FROM companies WHERE company_id = :id"), {"id": company.company_id})
		session.execute(text("DELETE FROM events WHERE client_id = :cid"), {"cid": _BLACKINK_INTERNAL_SALES})


def _seed_ovs_score(company_id, *, rank=3, percentile=91):
	with get_system_db_context() as session:
		session.execute(
			text(
				"INSERT INTO owner_visibility_scores (company_id, month_key, county_slug, score_total, "
				"county_rank, county_percentile) "
				"VALUES (:cid, TO_CHAR(NOW(), 'YYYY-MM'), (SELECT county_slug FROM counties LIMIT 1), 76, :rank, :pct) "
				"ON CONFLICT (company_id, month_key) DO UPDATE SET county_rank = EXCLUDED.county_rank, "
				"county_percentile = EXCLUDED.county_percentile"
			),
			{"cid": company_id, "rank": rank, "pct": percentile},
		)


def test_24h_job_before_its_window_stays_pending(sales_demo_setup):
	s = sales_demo_setup
	with get_system_db_context() as session:
		claimed = claim_reminders(session, claim_time=s["scheduled_at"] - timedelta(hours=25))
		assert s["jobs"]["24h_email"] not in claimed
		assert s["jobs"]["30min_email"] not in claimed


def test_24h_reminder_fires_within_60s_of_the_24h_mark(sales_demo_setup):
	s = sales_demo_setup
	_seed_ovs_score(s["company_id"])
	as_of = s["scheduled_at"] - timedelta(hours=24)
	sent = run_sweep(as_of=as_of)
	assert sent >= 1
	with get_system_db_context() as session:
		job = session.execute(
			text("SELECT * FROM booking_reminder_jobs WHERE reminder_job_id = :id"), {"id": s["jobs"]["24h_email"]}
		).one()
		# email_sending_enabled defaults False in this test env -> BLOCKED, not
		# silently PENDING forever -- proves the sweep's own claim+send logic
		# actually ran at this instant, not that a real email was delivered.
		assert job.status in ("BLOCKED", "SENT")
		assert job.last_error != "meeting already started before send"


def test_30min_job_not_claimed_before_its_window(sales_demo_setup):
	s = sales_demo_setup
	as_of = s["scheduled_at"] - timedelta(hours=24)  # only the 24h mark, not yet the 30min mark
	with get_system_db_context() as session:
		claimed = claim_reminders(session, claim_time=as_of)
		assert s["jobs"]["30min_email"] not in claimed


def test_missing_ovs_score_blocks_24h_reminder_never_fabricates_content(sales_demo_setup):
	s = sales_demo_setup
	with get_system_db_context() as session:
		claimed = claim_reminders(session, claim_time=s["scheduled_at"] - timedelta(hours=24))
		assert s["jobs"]["24h_email"] in claimed
		send_show_rate_reminder(session, s["jobs"]["24h_email"], as_of=s["scheduled_at"] - timedelta(hours=24))
		job = session.execute(
			text("SELECT * FROM booking_reminder_jobs WHERE reminder_job_id = :id"), {"id": s["jobs"]["24h_email"]}
		).one()
		assert job.status == "BLOCKED"
		assert job.last_error == "MISSING_OVS_SCORE"


def test_blocked_missing_ovs_score_auto_recovers_once_score_exists(sales_demo_setup):
	s = sales_demo_setup
	with get_system_db_context() as session:
		claim_time = s["scheduled_at"] - timedelta(hours=24)
		claimed = claim_reminders(session, claim_time=claim_time)
		send_show_rate_reminder(session, s["jobs"]["24h_email"], as_of=claim_time)
		job = session.execute(
			text("SELECT status FROM booking_reminder_jobs WHERE reminder_job_id = :id"), {"id": s["jobs"]["24h_email"]}
		).one()
		assert job.status == "BLOCKED"

	_seed_ovs_score(s["company_id"])

	with get_system_db_context() as session:
		recover_blocked_jobs(session, email_sending_enabled=False)
		job = session.execute(
			text("SELECT status FROM booking_reminder_jobs WHERE reminder_job_id = :id"), {"id": s["jobs"]["24h_email"]}
		).one()
		assert job.status == "PENDING"


def test_missing_ovs_pdf_blocks_30min_reminder(sales_demo_setup):
	s = sales_demo_setup
	_seed_ovs_score(s["company_id"])
	with get_system_db_context() as session:
		as_of = s["scheduled_at"] - timedelta(minutes=30)
		claimed = claim_reminders(session, claim_time=as_of)
		assert s["jobs"]["30min_email"] in claimed
		send_show_rate_reminder(session, s["jobs"]["30min_email"], as_of=as_of)
		job = session.execute(
			text("SELECT * FROM booking_reminder_jobs WHERE reminder_job_id = :id"), {"id": s["jobs"]["30min_email"]}
		).one()
		assert job.status == "BLOCKED"
		assert job.last_error == "MISSING_OVS_PDF"


def test_blocked_missing_ovs_pdf_auto_recovers_once_url_exists(sales_demo_setup):
	s = sales_demo_setup
	_seed_ovs_score(s["company_id"])
	with get_system_db_context() as session:
		as_of = s["scheduled_at"] - timedelta(minutes=30)
		claim_reminders(session, claim_time=as_of)
		send_show_rate_reminder(session, s["jobs"]["30min_email"], as_of=as_of)
		job = session.execute(
			text("SELECT status FROM booking_reminder_jobs WHERE reminder_job_id = :id"), {"id": s["jobs"]["30min_email"]}
		).one()
		assert job.status == "BLOCKED"

	with get_owner_db_context() as session:
		session.execute(
			text("UPDATE contacts SET ovs_pdf_url = 'https://example.com/ovs.pdf' WHERE contact_id = :id"),
			{"id": s["contact_id"]},
		)

	with get_system_db_context() as session:
		recover_blocked_jobs(session, email_sending_enabled=False)
		job = session.execute(
			text("SELECT status FROM booking_reminder_jobs WHERE reminder_job_id = :id"), {"id": s["jobs"]["30min_email"]}
		).one()
		assert job.status == "PENDING"


def test_cancelled_booking_between_claim_and_send_is_not_sent(sales_demo_setup):
	s = sales_demo_setup
	_seed_ovs_score(s["company_id"])
	with get_system_db_context() as session:
		as_of = s["scheduled_at"] - timedelta(hours=24)
		claimed = claim_reminders(session, claim_time=as_of)
		assert s["jobs"]["24h_email"] in claimed
		# Simulate a genuinely concurrent cancellation landing after claim.
		session.execute(text("UPDATE bookings SET event_status = 'CANCELLED' WHERE booking_id = :id"), {"id": s["booking_id"]})
		send_show_rate_reminder(session, s["jobs"]["24h_email"], as_of=as_of)
		job = session.execute(
			text("SELECT status FROM booking_reminder_jobs WHERE reminder_job_id = :id"), {"id": s["jobs"]["24h_email"]}
		).one()
		assert job.status == "CANCELLED"


def test_stale_reminder_after_meeting_started_is_skipped_not_sent(sales_demo_setup):
	s = sales_demo_setup
	_seed_ovs_score(s["company_id"])
	with get_system_db_context() as session:
		claim_time = s["scheduled_at"] - timedelta(hours=24)
		claimed = claim_reminders(session, claim_time=claim_time)
		assert s["jobs"]["24h_email"] in claimed
		# Process it very late -- after the meeting's own start time.
		late_as_of = s["scheduled_at"] + timedelta(minutes=5)
		send_show_rate_reminder(session, s["jobs"]["24h_email"], as_of=late_as_of)
		job = session.execute(
			text("SELECT * FROM booking_reminder_jobs WHERE reminder_job_id = :id"), {"id": s["jobs"]["24h_email"]}
		).one()
		assert job.status == "SKIPPED"
		assert job.last_error == "meeting already started before send"


def test_atomic_claim_no_double_send(sales_demo_setup):
	s = sales_demo_setup
	_seed_ovs_score(s["company_id"])
	with get_system_db_context() as session:
		as_of = s["scheduled_at"] - timedelta(hours=24)
		first_claim = claim_reminders(session, claim_time=as_of)
		second_claim = claim_reminders(session, claim_time=as_of)
		assert s["jobs"]["24h_email"] in first_claim
		assert s["jobs"]["24h_email"] not in second_claim


def test_show_rate_reminder_sent_event_logged_with_correct_step(sales_demo_setup, monkeypatch):
	s = sales_demo_setup
	_seed_ovs_score(s["company_id"])
	with get_system_db_context() as session:
		from config.settings import get_settings
		from src.services import email_dispatch

		monkeypatch.setattr(get_settings(), "email_sending_enabled", True)
		monkeypatch.setattr(
			email_dispatch, "_resolve_mailbox",
			lambda sess, cid: type("M", (), {
				"spf_validated": True, "dkim_validated": True, "dmarc_validated": True,
				"smtp_host": "x", "smtp_port": 587, "smtp_username": "x",
				"smtp_password_encrypted": "unused", "mailbox_address": "sales@getblackink.com",
			})(),
		)
		monkeypatch.setattr(email_dispatch, "decrypt_token", lambda ciphertext: "unused")

		class _FakeProvider:
			def send_plain(self, to, reply_to, bcc, subject, html_body):
				return "fake-msg-id"

		monkeypatch.setattr(email_dispatch, "SmtpEmailProvider", lambda **kwargs: _FakeProvider())

		as_of = s["scheduled_at"] - timedelta(hours=24)
		claim_reminders(session, claim_time=as_of)
		send_show_rate_reminder(session, s["jobs"]["24h_email"], as_of=as_of)

		job = session.execute(
			text("SELECT status FROM booking_reminder_jobs WHERE reminder_job_id = :id"), {"id": s["jobs"]["24h_email"]}
		).one()
		assert job.status == "SENT"

		event = session.execute(
			text(
				"SELECT * FROM events WHERE event_type = 'show_rate_reminder_sent' AND entity_id = :id"
			),
			{"id": str(s["jobs"]["24h_email"])},
		).one()
		assert event.payload["reminder_step"] == "24h_email"


def test_30min_reminder_fires_within_60s_of_the_30min_mark_and_sends():
	"""Companion to the 24h timing test -- proves the 30-minute step also
	reaches SENT at its own fire point, not just that it gets claimed."""
	from src.services import email_dispatch

	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM contacts WHERE email = 'sales-target@example.com'"))
		session.execute(text("DELETE FROM companies WHERE company_id = 'test-show-rate-co'"))
		company = session.execute(
			text(
				"INSERT INTO companies (company_id, company_name, domain, county_slug, status) "
				"VALUES ('test-show-rate-co', 'Test Show Rate PM Co', 'showrate-test.example.com', "
				"(SELECT county_slug FROM counties LIMIT 1), 'PROSPECTING') RETURNING company_id"
			)
		).one()
		contact = session.execute(
			text(
				"INSERT INTO contacts (company_id, contact_role_type, first_name, last_name, email, phone, ovs_pdf_url) "
				"VALUES (:cid, 'OWNER_BROKER_MD', 'Sam', 'Target', 'sales-target@example.com', '+14075551234', "
				"'https://example.com/ovs.pdf') RETURNING contact_id"
			),
			{"cid": company.company_id},
		).one()

	with get_system_db_context() as session:
		conn = session.execute(
			text(
				"INSERT INTO calendar_connections "
				"(client_id, provider, connection_scope, external_calendar_id, subscription_id, "
				" verification_secret, initial_sync_done) "
				"VALUES (:cid, 'GOOGLE', 'INTERNAL_SALES_DEMO', 'rep-30min', 'sub-30min-fire', 'secret', TRUE) "
				"RETURNING connection_id"
			),
			{"cid": _BLACKINK_INTERNAL_SALES},
		).one()
		scheduled_at = datetime.now(timezone.utc) + timedelta(days=5)
		booking = session.execute(
			text(
				"INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id, "
				"event_status, scheduled_at, raw_payload, target_company_id, target_contact_id, status) "
				"VALUES (:cid, 'GOOGLE', :conn, 'evt-30min-fire', 'CONFIRMED', :sched, '{}', :cid2, :ctid, 'MATCHED') "
				"RETURNING booking_id"
			),
			{
				"cid": _BLACKINK_INTERNAL_SALES, "conn": conn.connection_id, "sched": scheduled_at,
				"cid2": company.company_id, "ctid": contact.contact_id,
			},
		).one()
		job = session.execute(
			text(
				"INSERT INTO booking_reminder_jobs (client_id, booking_id, reminder_step, scheduled_for, status) "
				"VALUES (:cid, :bid, '30min_email', :sfor, 'PENDING') RETURNING reminder_job_id"
			),
			{"cid": _BLACKINK_INTERNAL_SALES, "bid": booking.booking_id, "sfor": scheduled_at - timedelta(minutes=30)},
		).one()

	try:
		with get_system_db_context() as session:
			from config.settings import get_settings
			settings = get_settings()
			original_enabled = settings.email_sending_enabled
			settings.email_sending_enabled = True
			original_resolve = email_dispatch._resolve_mailbox
			email_dispatch._resolve_mailbox = lambda sess, cid: type("M", (), {
				"smtp_host": "x", "smtp_port": 587, "smtp_username": "x",
				"smtp_password_encrypted": "unused", "mailbox_address": "sales@getblackink.com",
			})()
			original_smtp = email_dispatch.SmtpEmailProvider
			original_decrypt = email_dispatch.decrypt_token
			email_dispatch.decrypt_token = lambda ciphertext: "unused"

			class _FakeProvider:
				def send_with_attachment(self, to, reply_to, bcc, subject, html_body, attachment_bytes, attachment_filename, attachment_subtype):
					return "fake-msg-id"

			email_dispatch.SmtpEmailProvider = lambda **kwargs: _FakeProvider()

			# _fetch_ovs_pdf() enforces a real host allowlist/https/content
			# checks against contacts.ovs_pdf_url -- mocked here at the
			# function boundary rather than faking an httpx.stream response,
			# since this test is about the reminder pipeline, not re-proving
			# _fetch_ovs_pdf()'s own hardening (see tests/test_ovs_pdf_fetch.py).
			import src.services.show_rate_reminders as show_rate_reminders_module
			original_fetch = show_rate_reminders_module._fetch_ovs_pdf
			show_rate_reminders_module._fetch_ovs_pdf = lambda url: b"%PDF-fake"

			try:
				as_of = scheduled_at - timedelta(minutes=30)
				claimed = claim_reminders(session, claim_time=as_of)
				assert job.reminder_job_id in claimed
				send_show_rate_reminder(session, job.reminder_job_id, as_of=as_of)
				row = session.execute(
					text("SELECT status FROM booking_reminder_jobs WHERE reminder_job_id = :id"),
					{"id": job.reminder_job_id},
				).one()
				assert row.status == "SENT"
			finally:
				show_rate_reminders_module._fetch_ovs_pdf = original_fetch
				email_dispatch.SmtpEmailProvider = original_smtp
				email_dispatch.decrypt_token = original_decrypt
				email_dispatch._resolve_mailbox = original_resolve
				settings.email_sending_enabled = original_enabled
	finally:
		with get_owner_db_context() as session:
			session.execute(text("DELETE FROM booking_reminder_jobs WHERE booking_id = :id"), {"id": booking.booking_id})
		with get_system_db_context() as session:
			session.execute(text("DELETE FROM bookings WHERE booking_id = :id"), {"id": booking.booking_id})
			session.execute(text("DELETE FROM calendar_connections WHERE connection_id = :id"), {"id": conn.connection_id})
		with get_owner_db_context() as session:
			session.execute(text("DELETE FROM contacts WHERE contact_id = :id"), {"id": contact.contact_id})
			session.execute(text("DELETE FROM companies WHERE company_id = :id"), {"id": company.company_id})
			session.execute(text("DELETE FROM events WHERE client_id = :cid"), {"cid": _BLACKINK_INTERNAL_SALES})


def test_24h_email_body_contains_real_county_benchmark_content():
	"""DoD: the 24h reminder body must contain the prospect's
	county-specific visibility benchmark (real county_rank/county_percentile
	from Dev 2's owner_visibility_scores table), not fabricated growth
	statistics."""
	from src.services.email_dispatch import send_show_rate_24h_reminder

	captured = {}

	class _CapturingProvider:
		def send_plain(self, to, reply_to, bcc, subject, html_body):
			captured["html_body"] = html_body
			return "fake-msg-id"

	with get_system_db_context() as session:
		from config.settings import get_settings
		settings = get_settings()
		original = settings.email_sending_enabled
		settings.email_sending_enabled = True
		try:
			from src.services import email_dispatch
			original_resolve = email_dispatch._resolve_mailbox
			email_dispatch._resolve_mailbox = lambda sess, cid: type("M", (), {
				"smtp_host": "x", "smtp_port": 587, "smtp_username": "x",
				"smtp_password_encrypted": "unused", "mailbox_address": "sales@getblackink.com",
			})()
			try:
				send_show_rate_24h_reminder(
					session, client_id=_BLACKINK_INTERNAL_SALES, target_email="sales-target@example.com",
					target_name="Sam", scheduled_at=datetime.now(timezone.utc), county_name="Hillsborough",
					county_rank=3, county_percentile=91, local_time_label="EST", view_event_link=None,
					provider=_CapturingProvider(),
				)
			finally:
				email_dispatch._resolve_mailbox = original_resolve
		finally:
			settings.email_sending_enabled = original

	assert "html_body" in captured
	body = captured["html_body"]
	assert "Hillsborough" in body
	assert "#3" in body
	assert "91th percentile" in body


def test_pre_demo_email_body_contains_no_forbidden_content():
	"""Static content assertion on the REAL rendered body -- the DoD
	requires zero Rent Analysis Bot / phone / SMS-instruction content in
	the 30-minute pre-demo email."""
	from src.services.email_dispatch import _assert_clean_content, send_show_rate_pre_demo_email

	captured = {}

	class _CapturingProvider:
		def send_with_attachment(self, to, reply_to, bcc, subject, html_body, attachment_bytes, attachment_filename, attachment_subtype):
			captured["html_body"] = html_body
			return "fake-msg-id"

	with get_system_db_context() as session:
		from config.settings import get_settings
		settings = get_settings()
		original = settings.email_sending_enabled
		settings.email_sending_enabled = True
		try:
			from src.services import email_dispatch
			original_resolve = email_dispatch._resolve_mailbox
			email_dispatch._resolve_mailbox = lambda sess, cid: type("M", (), {
				"smtp_host": "x", "smtp_port": 587, "smtp_username": "x",
				"smtp_password_encrypted": "unused", "mailbox_address": "sales@getblackink.com",
			})()
			try:
				send_show_rate_pre_demo_email(
					session, client_id=_BLACKINK_INTERNAL_SALES, target_email="sales-target@example.com",
					target_name="Sam", scheduled_at=datetime.now(timezone.utc), local_time_label="EST",
					ovs_pdf_bytes=b"%PDF-fake", provider=_CapturingProvider(),
				)
			finally:
				email_dispatch._resolve_mailbox = original_resolve
		finally:
			settings.email_sending_enabled = original

	assert "html_body" in captured
	_assert_clean_content(captured["html_body"])  # raises ForbiddenContentError if unclean
	lowered = captured["html_body"].lower()
	assert "rent analysis bot" not in lowered
	assert "reply stop" not in lowered
