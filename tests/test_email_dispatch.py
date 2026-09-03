"""Pure unit tests for src/services/email_dispatch.py — no DB required
for the parts that don't touch mailboxes/sending_domains (ICS structure,
the email_sending_enabled gate)."""

from datetime import datetime, timezone

from icalendar import Calendar

from config.settings import get_settings
from src.services import email_dispatch


def test_build_ics_round_trips_and_has_required_fields():
	ics_bytes = email_dispatch.build_ics(
		uid="booking-123@getblackink.com",
		summary="Property Management Consultation",
		start=datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc),
		duration_minutes=30,
		organizer_email="rep@clientfirm.com",
		attendee_email="owner@example.com",
	)
	cal = Calendar.from_ical(ics_bytes)
	events = [c for c in cal.walk() if c.name == "VEVENT"]
	assert len(events) == 1
	event = events[0]
	assert str(event.get("uid")) == "booking-123@getblackink.com"
	assert "rep@clientfirm.com" in str(event.get("organizer"))
	assert "owner@example.com" in str(event.get("attendee"))


class _FakeSession:
	def __init__(self):
		self.executed = []

	def execute(self, stmt, params=None):
		self.executed.append((str(stmt), params))
		return _FakeResult(None)


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def first(self):
		return self._row

	def one(self):
		return self._row


def test_disabled_email_provider_blocks_and_logs_instead_of_sending(monkeypatch):
	monkeypatch.setattr(get_settings(), "email_sending_enabled", False)

	session = _FakeSession()
	result = email_dispatch.send_booking_confirmation_email(
		session,
		client_id="acme_pm",
		booking_id=42,
		owner_email="owner@example.com",
		owner_name="Jane Owner",
		client_reply_to="rep@clientfirm.com",
		scheduled_at=datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc),
	)
	assert result is None
	assert any("booking_confirmation_blocked" in sql for sql, _ in session.executed)


def test_missing_mailbox_blocks_and_logs_even_when_sending_enabled(monkeypatch):
	monkeypatch.setattr(get_settings(), "email_sending_enabled", True)
	monkeypatch.setattr(email_dispatch, "_resolve_mailbox", lambda session, client_id: None)

	session = _FakeSession()
	result = email_dispatch.send_booking_confirmation_email(
		session,
		client_id="acme_pm",
		booking_id=42,
		owner_email="owner@example.com",
		owner_name="Jane Owner",
		client_reply_to="rep@clientfirm.com",
		scheduled_at=datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc),
	)
	assert result is None
	assert any("booking_confirmation_blocked" in sql for sql, _ in session.executed)
