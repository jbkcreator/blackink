"""Email sender seam — direct SMTP per warmed mailbox (wayfinder ticket 05).

No SMTP vendor/credentials are modelled yet, so only the stub sender exists —
same shape as the compliance-gate stub providers. The stub mints a real,
RFC-5322 Message-ID (so threading and the at-most-once mark_sent path are
exercised end to end) but transmits nothing. Swap StubEmailSender for a real
smtplib-backed sender once mailbox SMTP credentials are provisioned; callers
depend only on the SendResult contract.
"""

import logging
from dataclasses import dataclass
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
