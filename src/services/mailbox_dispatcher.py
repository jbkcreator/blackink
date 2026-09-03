"""Per-tenant mailbox dispatcher — strict client_id isolation, no cross-tenant bleed.

Architectural invariant: a mailbox assigned to Client A (client_id='A') MUST
NEVER be used to send for Client B. This is enforced here at the query level
(WHERE client_id = :client_id) and at the DB level (RLS). Both must hold.

Round-robin selection: ORDER BY last_used_at ASC NULLS FIRST picks the
least-recently-used warmed+active mailbox. last_used_at is updated
atomically inside the same transaction as the pick (SELECT ... FOR UPDATE
SKIP LOCKED prevents two concurrent dispatch workers from picking the same
mailbox).

Raises NoMailboxAvailable — never falls back to another client's mailbox or
to Blackink's self-marketing pool (client_id IS NULL). Caller routes to DLQ.
Deliverability sentinel (src/tasks/deliverability_sentinel.py) owns reserve
promotion — this picker is read-only except for last_used_at.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class NoMailboxAvailable(RuntimeError):
    """No warmed, active mailbox is assigned to this client_id."""


class NoWarmedMailbox(NoMailboxAvailable):
    """No mailbox has completed warmup for this client yet."""


class AllMailboxesQuarantined(NoMailboxAvailable):
    """All warmed mailboxes are quarantined at the domain level.
    deliverability_sentinel should promote a reserve domain shortly."""


@dataclass(frozen=True)
class MailboxAssignment:
    mailbox_id: int
    mailbox_address: str
    instantly_account_email: Optional[str]
    client_id: str
    sending_domain: str  # domain of the associated sending_domains row


def get_active_mailbox_for_client(
    session: Session,
    client_id: str,
) -> MailboxAssignment:
    """Pick the least-recently-used warmed+active mailbox for this client.

    Joins sending_domains to enforce domain-level quarantine — the sentinel
    quarantines sending_domains rows, not mailboxes rows directly, so checking
    only mailboxes.quarantine_state misses a quarantined domain entirely.

    Uses SELECT FOR UPDATE SKIP LOCKED so concurrent dispatch workers never
    double-pick the same mailbox. Updates last_used_at in the same transaction.

    Raises:
        AllMailboxesQuarantined: warmed mailboxes exist but all domains are quarantined.
        NoWarmedMailbox: no mailbox has completed warmup yet.
        NoMailboxAvailable: no mailboxes provisioned at all.
    """
    # Check whether any warmed mailbox exists at all (ignoring domain state),
    # so we can raise a specific cause when domain quarantine is the blocker.
    warmed_count = session.execute(
        text(
            "SELECT COUNT(*) FROM mailboxes "
            "WHERE client_id = :client_id AND warmup_status = 'warmed'"
        ),
        {"client_id": client_id},
    ).scalar() or 0

    row = session.execute(
        text(
            "SELECT m.id, m.mailbox_address, m.instantly_account_email, m.client_id, sd.domain "
            "FROM mailboxes m "
            "JOIN sending_domains sd ON sd.id = m.domain_id "
            "WHERE m.client_id = :client_id "
            "  AND m.warmup_status = 'warmed' "
            "  AND sd.quarantine_state = 'active' "
            "ORDER BY m.last_used_at ASC NULLS FIRST "
            "LIMIT 1 "
            "FOR UPDATE OF m SKIP LOCKED"
        ),
        {"client_id": client_id},
    ).fetchone()

    if row is None:
        if warmed_count > 0:
            logger.error(
                "mailbox_dispatcher: %d warmed mailbox(es) for client_id=%s but all domains quarantined",
                warmed_count, client_id,
            )
            raise AllMailboxesQuarantined(
                f"{warmed_count} warmed mailbox(es) for client_id={client_id} "
                "but all associated domains are quarantined. "
                "deliverability_sentinel reserve promotion should resolve this."
            )
        any_count = session.execute(
            text("SELECT COUNT(*) FROM mailboxes WHERE client_id = :client_id"),
            {"client_id": client_id},
        ).scalar() or 0
        if any_count > 0:
            raise NoWarmedMailbox(
                f"Mailboxes provisioned for client_id={client_id} but none warmed yet."
            )
        raise NoMailboxAvailable(
            f"No mailboxes provisioned for client_id={client_id}."
        )

    mailbox_id, mailbox_address, instantly_account_email, mb_client_id, sending_domain = row

    # Hard assertion — belt-and-suspenders on top of the WHERE clause
    if mb_client_id != client_id:
        raise RuntimeError(
            f"CROSS-TENANT BREACH: picked mailbox client_id={mb_client_id} "
            f"for request client_id={client_id}. Blocking send."
        )

    session.execute(
        text("UPDATE mailboxes SET last_used_at = :now WHERE id = :mailbox_id"),
        {"now": datetime.now(timezone.utc), "mailbox_id": mailbox_id},
    )

    logger.info(
        "mailbox_dispatcher: assigned mailbox_id=%s (%s / %s) to client_id=%s",
        mailbox_id, mailbox_address, sending_domain, client_id,
    )

    return MailboxAssignment(
        mailbox_id=mailbox_id,
        mailbox_address=mailbox_address,
        instantly_account_email=instantly_account_email,
        client_id=client_id,
        sending_domain=sending_domain,
    )
