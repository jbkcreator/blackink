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


class AllMailboxesCapped(NoMailboxAvailable):
    """Warmed, un-quarantined mailboxes exist but every one has hit its
    rolling-24h send cap. Not a failure — the caller should DEFER the touch
    (push due_at forward), not dead-letter it (wayfinder ticket 08)."""


DEFAULT_DAILY_SEND_CAP = 50
# Hard platform ceiling per mailbox per rolling 24h — the blueprint's
# deliverability limit (30–50/mailbox). A per-client daily_send_ceiling may
# lower this but never raise it; a misconfigured high value can't burn a
# mailbox's reputation.
MAX_DAILY_SEND_CAP = 50


@dataclass(frozen=True)
class MailboxAssignment:
    mailbox_id: int
    mailbox_address: str
    instantly_account_email: Optional[str]
    client_id: str
    sending_domain: str  # domain of the associated sending_domains row


def _resolve_daily_send_cap(session: Session, client_id: str) -> int:
    """Resolve the PER-MAILBOX rolling-24h send cap for this client.

    This is a per-mailbox limit, NOT a per-client total — deliberately, per the
    blueprint (§653 "Strict daily ceiling of 30–50 cold emails per mailbox/day
    with automated rotation across the client's 6 assigned mailboxes"; §1277
    "enforces per-mailbox rate limits (30–50 sends/day)"). A client with N warmed
    mailboxes sending up to the cap on each is the intended rotation model — the
    spec defines no aggregate per-client volume ceiling. `daily_send_ceiling`
    lets a cautious client throttle EACH of its mailboxes below the platform max;
    it is not a client-wide budget. (A future per-client aggregate cap, if the
    product ever wants one, would be a separate check counted by client_id.)

    clients.daily_send_ceiling defaults to 0, treated as "unset" → fall back to
    DEFAULT_DAILY_SEND_CAP so a freshly-provisioned client is never floored to
    zero sends. A positive value lowers the per-mailbox cap but is bounded above
    by MAX_DAILY_SEND_CAP — a misconfigured 100 can't raise a mailbox past the
    platform max and burn its warmed reputation.
    """
    ceiling = session.execute(
        text("SELECT daily_send_ceiling FROM clients WHERE client_id = :client_id"),
        {"client_id": client_id},
    ).scalar()
    if ceiling is None or ceiling <= 0:
        return DEFAULT_DAILY_SEND_CAP
    return min(int(ceiling), MAX_DAILY_SEND_CAP)


def get_active_mailbox_for_client(
    session: Session,
    client_id: str,
    daily_send_cap: Optional[int] = None,
) -> MailboxAssignment:
    """Pick the least-recently-used warmed+active+under-cap mailbox for this client.

    Joins sending_domains to enforce domain-level quarantine — the sentinel
    quarantines sending_domains rows, not mailboxes rows directly, so checking
    only mailboxes.quarantine_state misses a quarantined domain entirely.

    The rolling-24h send cap is enforced HERE, inside the picker, so a capped
    mailbox is never handed out and last_used_at is never bumped for a mailbox
    that can't send (wayfinder ticket 08). Sends are counted against
    sequence_touch_dispatches (SENDING + SENT rows in the last 24h).

    The cap is per-client: when the caller passes no explicit daily_send_cap it
    is resolved from clients.daily_send_ceiling (0/NULL → DEFAULT_DAILY_SEND_CAP).
    An explicit argument still wins (tests, callers that already know the cap).

    Uses SELECT FOR UPDATE SKIP LOCKED so concurrent dispatch workers never
    double-pick the same mailbox. Updates last_used_at in the same transaction.

    Raises:
        AllMailboxesCapped: warmed, un-quarantined mailboxes exist but all are at cap.
        AllMailboxesQuarantined: warmed mailboxes exist but all domains are quarantined.
        NoWarmedMailbox: no mailbox has completed warmup yet.
        NoMailboxAvailable: no mailboxes provisioned at all.
    """
    if daily_send_cap is None:
        daily_send_cap = _resolve_daily_send_cap(session, client_id)

    # Check whether any warmed mailbox exists at all (ignoring domain state),
    # so we can raise a specific cause when domain quarantine is the blocker.
    warmed_count = session.execute(
        text(
            "SELECT COUNT(*) FROM mailboxes "
            "WHERE client_id = :client_id AND warmup_status = 'warmed'"
        ),
        {"client_id": client_id},
    ).scalar() or 0

    # Per-mailbox rolling-24h send count is a correlated subquery so the cap
    # filter is part of the same atomic pick — no capped mailbox is selected,
    # and its last_used_at is left untouched (keeps LRU rotation honest).
    row = session.execute(
        text(
            "SELECT m.id, m.mailbox_address, m.instantly_account_email, m.client_id, sd.domain "
            "FROM mailboxes m "
            "JOIN sending_domains sd ON sd.id = m.domain_id "
            "WHERE m.client_id = :client_id "
            "  AND m.warmup_status = 'warmed' "
            "  AND sd.quarantine_state = 'active' "
            "  AND ( "
            "    SELECT COUNT(*) FROM sequence_touch_dispatches d "
            "    WHERE d.mailbox_id = m.id "
            "      AND d.status IN ('SENDING', 'SENT') "
            "      AND d.created_at >= NOW() - INTERVAL '24 hours' "
            "  ) < :cap "
            "ORDER BY m.last_used_at ASC NULLS FIRST "
            "LIMIT 1 "
            "FOR UPDATE OF m SKIP LOCKED"
        ),
        {"client_id": client_id, "cap": daily_send_cap},
    ).fetchone()

    if row is None:
        # Distinguish "all capped" from "all quarantined" from "none warmed":
        # count warmed + un-quarantined mailboxes regardless of cap. If any
        # exist, the picker only skipped them because they were all at cap.
        warmed_active_count = session.execute(
            text(
                "SELECT COUNT(*) FROM mailboxes m "
                "JOIN sending_domains sd ON sd.id = m.domain_id "
                "WHERE m.client_id = :client_id "
                "  AND m.warmup_status = 'warmed' "
                "  AND sd.quarantine_state = 'active'"
            ),
            {"client_id": client_id},
        ).scalar() or 0
        if warmed_active_count > 0:
            logger.warning(
                "mailbox_dispatcher: %d warmed/active mailbox(es) for client_id=%s but all at 24h cap=%d",
                warmed_active_count, client_id, daily_send_cap,
            )
            raise AllMailboxesCapped(
                f"{warmed_active_count} warmed/active mailbox(es) for client_id={client_id} "
                f"but all have hit the rolling-24h send cap of {daily_send_cap}. Defer the touch."
            )
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
