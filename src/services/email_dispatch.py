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

import smtplib
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


class SmtpEmailProvider(EmailProvider):
	def __init__(self, host: str, port: int, username: str, password: str, from_address: str):
		self._host = host
		self._port = port
		self._username = username
		self._password = password
		self._from_address = from_address

	def send(self, to: str, reply_to: str, bcc: str, subject: str, html_body: str, ics_attachment: bytes) -> str:
		import uuid

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
