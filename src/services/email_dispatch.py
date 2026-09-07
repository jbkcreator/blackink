"""Real, gated booking-confirmation email (Subtask 3.2.1). Unlike DNC/
SMS/RentCast (genuinely no vendor exists), a real SmtpEmailProvider is
implemented here per explicit instruction — but gated behind
settings.email_sending_enabled (default False) so a missing/misconfigured
sending domain is a visible launch blocker (a logged
booking_confirmation_blocked event), never a silent no-op or a stub that
quietly satisfies a test.

Sends through the client's own delegated subdomain — the existing
Respond design already documented in CLAUDE.md (delegated subdomain,
Reply-To the client's own address, BCC the client on every send).
Mailbox SMTP credentials are encrypted at rest via
src/core/token_crypto.py (migrations/apply_mailbox_smtp_credentials.py).
"""

from __future__ import annotations

import re
import smtplib
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from icalendar import Calendar, Event as IcsEvent
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.core.token_crypto import decrypt_token


class EmailProvider(ABC):
	@abstractmethod
	def send(self, to: str, reply_to: str, bcc: str, subject: str, html_body: str, ics_attachment: bytes) -> str:
		"""Send the email and return a provider message id (SMTP has no
		native id concept — SmtpEmailProvider synthesizes one)."""

	@abstractmethod
	def send_plain(self, to: str, reply_to: str, bcc: str, subject: str, html_body: str) -> str:
		"""Show-Rate Reminder Cascade (Subtask 3.2.2) — the 24h reminder has
		no attachment at all, unlike the ICS-bearing confirmation email."""

	@abstractmethod
	def send_with_attachment(
		self, to: str, reply_to: str, bcc: str, subject: str, html_body: str,
		attachment_bytes: bytes, attachment_filename: str, attachment_subtype: str,
	) -> str:
		"""Show-Rate Reminder Cascade (Subtask 3.2.2) — the pre-demo email's
		single arbitrary attachment (the OVS PDF), distinct from the fixed
		ICS the booking-confirmation path always sends."""


class SmtpEmailProvider(EmailProvider):
	def __init__(self, host: str, port: int, username: str, password: str, from_address: str):
		self._host = host
		self._port = port
		self._username = username
		self._password = password
		self._from_address = from_address

	def send(self, to: str, reply_to: str, bcc: str, subject: str, html_body: str, ics_attachment: bytes) -> str:
		msg = MIMEMultipart()
		msg["From"] = self._from_address
		msg["To"] = to
		msg["Reply-To"] = reply_to
		msg["Subject"] = subject
		msg.attach(MIMEText(html_body, "html"))
		ics_part = MIMEApplication(ics_attachment, _subtype="ics")
		ics_part.add_header("Content-Disposition", "attachment", filename="confirmation.ics")
		msg.attach(ics_part)

		recipients = [to, bcc] if bcc else [to]
		self._send_mime(msg, recipients)
		return f"smtp-{uuid.uuid4().hex}"

	def _send_mime(self, msg: MIMEMultipart, recipients: list) -> None:
		with smtplib.SMTP(self._host, self._port, timeout=15) as smtp:
			smtp.starttls()
			smtp.login(self._username, self._password)
			try:
				smtp.sendmail(self._from_address, recipients, msg.as_string())
			except (smtplib.SMTPServerDisconnected, ConnectionResetError, TimeoutError) as exc:
				# The DATA transaction had already started when the
				# connection dropped — the message may or may not have
				# reached the server. Distinct from a clean rejection
				# (SMTPRecipientsRefused, SMTPResponseException with a
				# definite error code), which propagates normally as a
				# retryable failure.
				from src.services.calendar_confirmation import UncertainDeliveryError

				raise UncertainDeliveryError(f"SMTP connection dropped mid-transaction: {exc}") from exc

	def send_plain(self, to: str, reply_to: str, bcc: str, subject: str, html_body: str) -> str:
		msg = MIMEMultipart()
		msg["From"] = self._from_address
		msg["To"] = to
		msg["Reply-To"] = reply_to
		msg["Subject"] = subject
		msg.attach(MIMEText(html_body, "html"))
		recipients = [to, bcc] if bcc else [to]
		self._send_mime(msg, recipients)
		return f"smtp-{uuid.uuid4().hex}"

	def send_with_attachment(
		self, to: str, reply_to: str, bcc: str, subject: str, html_body: str,
		attachment_bytes: bytes, attachment_filename: str, attachment_subtype: str,
	) -> str:
		msg = MIMEMultipart()
		msg["From"] = self._from_address
		msg["To"] = to
		msg["Reply-To"] = reply_to
		msg["Subject"] = subject
		msg.attach(MIMEText(html_body, "html"))
		part = MIMEApplication(attachment_bytes, _subtype=attachment_subtype)
		part.add_header("Content-Disposition", "attachment", filename=attachment_filename)
		msg.attach(part)
		recipients = [to, bcc] if bcc else [to]
		self._send_mime(msg, recipients)
		return f"smtp-{uuid.uuid4().hex}"


def build_ics(*, uid: str, summary: str, start: datetime, duration_minutes: int, organizer_email: str, attendee_email: str) -> bytes:
	cal = Calendar()
	cal.add("prodid", "-//Blackink//Booking Confirmation//EN")
	cal.add("version", "2.0")
	event = IcsEvent()
	event.add("uid", uid)
	event.add("summary", summary)
	event.add("dtstart", start)
	event.add("dtend", start + timedelta(minutes=duration_minutes))
	event.add("organizer", f"mailto:{organizer_email}")
	event.add("attendee", f"mailto:{attendee_email}")
	cal.add_component(event)
	return cal.to_ical()


def _record_blocked(session: Session, client_id: str, booking_id: int, reason: str) -> None:
	session.execute(
		text(
			"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
			"VALUES (:client_id, 'booking_confirmation_blocked', 'booking', :entity_id, "
			"jsonb_build_object('layer', 'CONFIG', 'reason', :reason))"
		),
		{"client_id": client_id, "entity_id": str(booking_id), "reason": reason},
	)


def _resolve_mailbox(session: Session, client_id: str) -> Optional[object]:
	return session.execute(
		text(
			"SELECT m.mailbox_address, m.smtp_host, m.smtp_port, m.smtp_username, m.smtp_password_encrypted, "
			"d.spf_validated, d.dkim_validated, d.dmarc_validated "
			"FROM mailboxes m JOIN sending_domains d ON m.domain_id = d.id "
			"WHERE m.client_id = :client_id AND m.quarantine_state = 'active' LIMIT 1"
		),
		{"client_id": client_id},
	).first()


def send_booking_confirmation_email(
	session: Session,
	*,
	client_id: str,
	booking_id: int,
	owner_email: str,
	owner_name: str,
	client_reply_to: str,
	scheduled_at: datetime,
	provider: Optional[EmailProvider] = None,
) -> str:
	"""Returns a provider message id on success. Raises on a genuine send
	failure — callers (src/tasks/booking_confirmation_sender.py) are
	responsible for classifying the exception as FAILED vs UNCERTAIN.
	A disabled/misconfigured provider does not raise — it logs
	booking_confirmation_blocked and returns None, since that's a launch-
	readiness gap, not a per-message failure."""
	settings = get_settings()
	if not settings.email_sending_enabled:
		_record_blocked(session, client_id, booking_id, "email_sending_enabled is False")
		return None

	mailbox = _resolve_mailbox(session, client_id)
	if mailbox is None:
		_record_blocked(session, client_id, booking_id, "no active mailbox configured for client")
		return None
	if not (mailbox.spf_validated and mailbox.dkim_validated and mailbox.dmarc_validated):
		_record_blocked(session, client_id, booking_id, "sending domain SPF/DKIM/DMARC not fully validated")
		return None

	if provider is None:
		provider = SmtpEmailProvider(
			host=mailbox.smtp_host,
			port=mailbox.smtp_port or 587,
			username=mailbox.smtp_username,
			password=decrypt_token(mailbox.smtp_password_encrypted),
			from_address=mailbox.mailbox_address,
		)

	ics = build_ics(
		uid=f"booking-{booking_id}@getblackink.com",
		summary="Property Management Consultation",
		start=scheduled_at,
		duration_minutes=30,
		organizer_email=client_reply_to,
		attendee_email=owner_email,
	)
	html_body = (
		f"<p>Hi {owner_name or 'there'},</p>"
		f"<p>Your meeting is confirmed for {scheduled_at.isoformat()}.</p>"
	)
	return provider.send(
		to=owner_email,
		reply_to=client_reply_to,
		bcc=client_reply_to,
		subject="Your meeting is confirmed",
		html_body=html_body,
		ics_attachment=ics,
	)


# ── Show-Rate Reminder Cascade (Subtask 3.2.2) ──────────────────────────────
# Static content guard, applied to the REAL rendered body of both
# templates below, right before dispatch — the DoD requires the 30-minute
# pre-demo email to contain zero Rent Analysis Bot / phone / SMS-instruction
# content; applied to the 24h template too since nothing calls for phone/SMS
# content there either.
_PHONE_PATTERN = re.compile(r"(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
_FORBIDDEN_PHRASES = ("rent analysis bot", "text any property address", "reply stop", "msg & data rates")


class ForbiddenContentError(Exception):
	"""Raised if a rendered reminder body contains banned content (Rent
	Analysis Bot / phone / SMS-instruction language) — a hard stop, not a
	warning, since sending this content is exactly what the DoD forbids."""


def _assert_clean_content(html_body: str) -> None:
	lowered = html_body.lower()
	for phrase in _FORBIDDEN_PHRASES:
		if phrase in lowered:
			raise ForbiddenContentError(f"Forbidden phrase '{phrase}' found in reminder body")
	if _PHONE_PATTERN.search(html_body):
		raise ForbiddenContentError("Phone-number-shaped content found in reminder body")
# Both functions below share send_booking_confirmation_email's gating
# posture: settings.email_sending_enabled is checked by the caller
# (src/services/show_rate_reminders.py), which also owns the BLOCKED/
# MISSING_OVS_SCORE/MISSING_OVS_PDF preconditions — these two functions
# assume the caller has already confirmed it's safe to send and focus only
# on composing and dispatching the message.


def send_show_rate_24h_reminder(
	session: Session,
	*,
	client_id: str,
	target_email: str,
	target_name: Optional[str],
	scheduled_at: datetime,
	county_name: str,
	county_rank: Optional[int],
	county_percentile: Optional[int],
	local_time_label: Optional[str],
	view_event_link: Optional[str],
	provider: Optional[EmailProvider] = None,
) -> Optional[str]:
	"""24-hour-prior reminder — agenda, county-specific visibility
	benchmark content (real numbers from Dev 2's owner_visibility_scores
	table — county_rank/county_percentile, never fabricated growth
	statistics), and a "View calendar event" link when the provider
	payload has one. No self-serve reschedule mechanism exists anywhere
	in this repo or its provider contracts — this function does not
	fabricate one; see show_rate_reminders.py for the citation."""
	if not get_settings().email_sending_enabled:
		return None
	mailbox = _resolve_mailbox(session, client_id)
	if mailbox is None:
		return None
	if provider is None:
		provider = SmtpEmailProvider(
			host=mailbox.smtp_host, port=mailbox.smtp_port or 587, username=mailbox.smtp_username,
			password=decrypt_token(mailbox.smtp_password_encrypted), from_address=mailbox.mailbox_address,
		)

	when_label = f"{scheduled_at.isoformat()}" + (f" ({local_time_label})" if local_time_label else "")
	if county_rank is not None and county_percentile is not None:
		benchmark_line = (
			f"<p>In {county_name}, your firm currently ranks #{county_rank} "
			f"({county_percentile}th percentile) for public owner-visibility.</p>"
		)
	else:
		benchmark_line = ""
	view_link_line = f'<p><a href="{view_event_link}">View calendar event</a></p>' if view_event_link else ""

	html_body = (
		f"<p>Hi {target_name or 'there'},</p>"
		f"<p>Reminder: your Blackink demo is scheduled for {when_label}.</p>"
		f"{benchmark_line}"
		f"<p>We'll walk through your firm's owner-visibility standing and how to improve it.</p>"
		f"{view_link_line}"
	)
	_assert_clean_content(html_body)
	return provider.send_plain(
		to=target_email, reply_to=mailbox.mailbox_address, bcc=mailbox.mailbox_address,
		subject="Reminder: your Blackink demo is tomorrow", html_body=html_body,
	)


def send_no_show_recovery_email(
	session: Session,
	*,
	client_id: str,
	target_email: str,
	target_name: Optional[str],
	booking_url: Optional[str],
	provider: Optional[EmailProvider] = None,
) -> Optional[str]:
	"""Subtask 3.2.3 — No-Show Handler recovery email. One immediate
	send, no cadence — the DoD tests exactly one recovery email within 5
	minutes of the no-show trigger; no further follow-up timing is
	specified anywhere in the source of truth for this flow, so none is
	invented here (src/tasks/no_show_recovery_sender.py calls this
	exactly once per booking, enforced by no_show_recovery_jobs'
	UNIQUE(booking_id)). Email only — no SMS import anywhere in this
	function or its call path, matching the DoD's own "no SMS" line."""
	if not get_settings().email_sending_enabled:
		return None
	mailbox = _resolve_mailbox(session, client_id)
	if mailbox is None:
		return None
	if provider is None:
		provider = SmtpEmailProvider(
			host=mailbox.smtp_host, port=mailbox.smtp_port or 587, username=mailbox.smtp_username,
			password=decrypt_token(mailbox.smtp_password_encrypted), from_address=mailbox.mailbox_address,
		)

	booking_line = (
		f'<p><a href="{booking_url}">Pick a new time</a></p>' if booking_url
		else "<p>Reply to this email and we'll find a new time.</p>"
	)
	html_body = (
		f"<p>Hi {target_name or 'there'},</p>"
		f"<p>We missed you for your Blackink demo — no worries, let's find a time that works.</p>"
		f"{booking_line}"
	)
	_assert_clean_content(html_body)
	return provider.send_plain(
		to=target_email, reply_to=mailbox.mailbox_address, bcc=mailbox.mailbox_address,
		subject="Let's reschedule your Blackink demo", html_body=html_body,
	)


def send_show_rate_pre_demo_email(
	session: Session,
	*,
	client_id: str,
	target_email: str,
	target_name: Optional[str],
	scheduled_at: datetime,
	local_time_label: Optional[str],
	ovs_pdf_bytes: bytes,
	provider: Optional[EmailProvider] = None,
) -> Optional[str]:
	"""30-minute pre-demo lead-in — agenda/prep copy plus the OVS PDF
	fetched from Dev 2's stored contacts.ovs_pdf_url (never regenerated
	here — see show_rate_reminders.py). Per the DoD: no Rent Analysis Bot
	reference, no phone number, no SMS instruction anywhere in this
	template — enforced by a static content-assertion test, not just
	review."""
	if not get_settings().email_sending_enabled:
		return None
	mailbox = _resolve_mailbox(session, client_id)
	if mailbox is None:
		return None
	if provider is None:
		provider = SmtpEmailProvider(
			host=mailbox.smtp_host, port=mailbox.smtp_port or 587, username=mailbox.smtp_username,
			password=decrypt_token(mailbox.smtp_password_encrypted), from_address=mailbox.mailbox_address,
		)

	when_label = f"{scheduled_at.isoformat()}" + (f" ({local_time_label})" if local_time_label else "")
	html_body = (
		f"<p>Hi {target_name or 'there'},</p>"
		f"<p>Your Blackink demo starts in 30 minutes ({when_label}).</p>"
		f"<p>Attached is your firm's Owner Visibility Score report — we'll use it as the "
		f"starting point for today's walkthrough.</p>"
	)
	_assert_clean_content(html_body)
	return provider.send_with_attachment(
		to=target_email, reply_to=mailbox.mailbox_address, bcc=mailbox.mailbox_address,
		subject="Starting in 30 minutes — your Blackink demo", html_body=html_body,
		attachment_bytes=ovs_pdf_bytes, attachment_filename="owner-visibility-score.pdf", attachment_subtype="pdf",
	)
