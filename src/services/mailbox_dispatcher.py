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
    """No warmed, active mailbox is assigned to this client_id.

    Possible causes:
    - No mailboxes provisioned for client yet
    - All mailboxes quarantined (deliverability_sentinel will promote reserve)
    - All mailboxes still warming (not yet warmed)
    """


@dataclass(frozen=True)
class MailboxAssignment:
    mailbox_id: int
    mailbox_address: str
    instantly_account_email: Optional[str]
    client_id: str


def get_active_mailbox_for_client(
    session: Session,
    client_id: str,
) -> MailboxAssignment:
    """Pick the least-recently-used warmed+active mailbox for this client.

    Uses SELECT FOR UPDATE SKIP LOCKED so concurrent dispatch workers never
    double-pick the same mailbox. Updates last_used_at in the same transaction.

    Args:
        session: Must be a blackink_app (RLS-subject) session scoped to client_id.
        client_id: The tenant whose mailbox pool to pick from.

    Returns:
        MailboxAssignment — the chosen mailbox details.

    Raises:
        NoMailboxAvailable: if no warmed+active mailbox exists for this client.
    """
    row = session.execute(
        text(
            "SELECT id, mailbox_address, instantly_account_email, client_id "
            "FROM mailboxes "
            "WHERE client_id = :client_id "
            "  AND warmup_status = 'warmed' "
            "  AND quarantine_state = 'active' "
            "ORDER BY last_used_at ASC NULLS FIRST "
            "LIMIT 1 "
            "FOR UPDATE SKIP LOCKED"
        ),
        {"client_id": client_id},
    ).fetchone()

    if row is None:
        logger.warning(
            "mailbox_dispatcher: no warmed+active mailbox for client_id=%s", client_id
        )
        raise NoMailboxAvailable(
            f"No warmed+active mailbox available for client_id={client_id}. "
            "Check mailbox provisioning or deliverability sentinel reserve promotion."
        )

    mailbox_id, mailbox_address, instantly_account_email, mb_client_id = row

    # Hard assertion — belt-and-suspenders on top of the WHERE clause
    if mb_client_id != client_id:
        raise RuntimeError(
            f"CROSS-TENANT BREACH: picked mailbox client_id={mb_client_id} "
            f"for request client_id={client_id}. Blocking send."
        )

    session.execute(
        text(
            "UPDATE mailboxes SET last_used_at = :now WHERE id = :mailbox_id"
        ),
        {"now": datetime.now(timezone.utc), "mailbox_id": mailbox_id},
    )

    logger.info(
        "mailbox_dispatcher: assigned mailbox_id=%s (%s) to client_id=%s",
        mailbox_id,
        mailbox_address,
        client_id,
    )

    return MailboxAssignment(
        mailbox_id=mailbox_id,
        mailbox_address=mailbox_address,
        instantly_account_email=instantly_account_email,
        client_id=client_id,
    )
