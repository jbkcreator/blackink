"""Email sender seam — direct SMTP per warmed mailbox (wayfinder ticket 05).

Two implementations behind one Protocol:
  - StubEmailSender  — mints a real RFC-5322 Message-ID, transmits nothing.
    Used until SMTP credentials are provisioned; exercises the full
    claim -> send -> mark_sent lifecycle without delivering mail.
  - SmtpEmailSender  — real delivery via smtplib over STARTTLS, per warmed
    mailbox (Google Workspace / Outlook). Sets Message-ID, In-Reply-To
    (threading), Reply-To and Bcc (blueprint §768).

build_email_sender() picks the real sender when SMTP is configured
(settings.smtp_host + smtp_password), else the stub — so wiring real client
creds is a config change, not a code change. Callers depend only on
send(...) -> SendResult.
"""

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendResult:
    message_id: str


class EmailSender(Protocol):
    def send(
        self,
        *,
        from_address: str,
        to_address: str,
        subject: str,
        body: str,
        sending_domain: str,
        in_reply_to: Optional[str] = None,
    ) -> SendResult: ...


class StubEmailSender:
    """Mints a Message-ID and logs; transmits nothing. No vendor contracted."""

    def send(
        self,
        *,
        from_address: str,
        to_address: str,
        subject: str,
        body: str,
        sending_domain: str,
        in_reply_to: Optional[str] = None,
    ) -> SendResult:
        message_id = make_msgid(domain=sending_domain)
        logger.info(
            "STUB email send from=%s to=%s domain=%s in_reply_to=%s message_id=%s "
            "(no vendor contracted — nothing transmitted)",
            from_address, to_address, sending_domain, in_reply_to, message_id,
        )
        return SendResult(message_id=message_id)


class SmtpEmailSender:
    """Real delivery via SMTP over STARTTLS, one login per warmed mailbox.

    login/password authenticate the SMTP session; from_address is the mailbox
    the mail is sent as (defaults to the SMTP login when not separately set).
    reply_to / bcc apply the client-inbox Reply-To and per-send BCC (§768).
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        password: str,
        username: Optional[str] = None,
        use_tls: bool = True,
        reply_to: Optional[str] = None,
        bcc: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._username = username
        self._use_tls = use_tls
        self._reply_to = reply_to
        self._bcc = bcc
        self._timeout = timeout

    def send(
        self,
        *,
        from_address: str,
        to_address: str,
        subject: str,
        body: str,
        sending_domain: str,
        in_reply_to: Optional[str] = None,
    ) -> SendResult:
        message_id = make_msgid(domain=sending_domain)
        login = self._username or from_address

        msg = EmailMessage()
        msg["Message-ID"] = message_id
        msg["From"] = from_address
        msg["To"] = to_address
        msg["Subject"] = subject
        if self._reply_to:
            msg["Reply-To"] = self._reply_to
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        rcpts = [to_address]
        if self._bcc:
            rcpts.append(self._bcc)
        msg.set_content(body)

        with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
            if self._use_tls:
                smtp.starttls()
            smtp.login(login, self._password)
            smtp.send_message(msg, from_addr=from_address, to_addrs=rcpts)

        logger.info(
            "SMTP email sent from=%s to=%s domain=%s in_reply_to=%s message_id=%s",
            from_address, to_address, sending_domain, in_reply_to, message_id,
        )
        return SendResult(message_id=message_id)


def build_email_sender() -> EmailSender:
    """Return a real SmtpEmailSender when SMTP is configured, else the stub.

    Wiring real client creds is a config change (set SMTP_HOST + SMTP_PASSWORD
    + friends), not a code change — dispatch_touch calls this by default."""
    from config.settings import get_settings

    s = get_settings()
    if s.smtp_host and s.smtp_password:
        return SmtpEmailSender(
            host=s.smtp_host,
            port=s.smtp_port,
            password=s.smtp_password.get_secret_value(),
            username=s.smtp_username,
            use_tls=s.smtp_use_tls,
            reply_to=s.email_reply_to,
            bcc=s.email_bcc,
        )
    logger.info("build_email_sender: SMTP not configured — using StubEmailSender")
    return StubEmailSender()
