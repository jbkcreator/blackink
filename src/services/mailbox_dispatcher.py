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


def _resolve_client_daily_ceiling(session: Session, client_id: str) -> int:
    """Per-CLIENT total daily send ceiling across ALL of the client's mailboxes,
    from clients.daily_send_ceiling (PR #35 review — enforced by client_id total,
    not per mailbox). 0/NULL means "no aggregate cap": only the per-mailbox cap
    (MAX_DAILY_SEND_CAP, the blueprint's 30–50/mailbox deliverability limit)
    applies. A positive value caps the client's combined daily volume."""
    ceiling = session.execute(
        text("SELECT daily_send_ceiling FROM clients WHERE client_id = :client_id"),
        {"client_id": client_id},
    ).scalar()
    return int(ceiling) if ceiling and ceiling > 0 else 0


def _client_sends_last_24h(session: Session, client_id: str) -> int:
    """Count the client's sends across all mailboxes in the rolling 24h window.

    Covers both cold-sequence touches and Speed-to-Lead cadence follow-ups
    (Task 4.2.2) — an STL send is a real send against the client's daily volume,
    so it must count toward the per-client ceiling too."""
    return session.execute(
        text(
            "SELECT ( "
            "  SELECT COUNT(*) FROM sequence_touch_dispatches "
            "  WHERE client_id = :client_id "
            "    AND status IN ('SENDING', 'SENT') "
            "    AND created_at >= NOW() - INTERVAL '24 hours' "
            ") + ( "
            "  SELECT COUNT(*) FROM stl_cadence_dispatches "
            "  WHERE client_id = :client_id "
            "    AND status IN ('SENDING', 'SENT', 'SENT_UNCONFIRMED') "
            "    AND created_at >= NOW() - INTERVAL '24 hours' "
            ")"
        ),
        {"client_id": client_id},
    ).scalar() or 0


def get_active_mailbox_for_client(
    session: Session,
    client_id: str,
    daily_send_cap: Optional[int] = None,
) -> MailboxAssignment:
    """Pick the least-recently-used warmed+active+under-cap mailbox for this client.

    Joins sending_domains to enforce domain-level quarantine — the sentinel
    quarantines sending_domains rows, not mailboxes rows directly, so checking
    only mailboxes.quarantine_state misses a quarantined domain entirely.

    TWO limits are enforced here (PR #35 review):
      - Per-mailbox: MAX_DAILY_SEND_CAP (the blueprint's 30–50/mailbox
        deliverability limit) — no single mailbox is handed out past it, and
        last_used_at is never bumped for a capped mailbox (wayfinder ticket 08).
        `daily_send_cap` overrides this per-mailbox value (tests/callers).
      - Per-client: clients.daily_send_ceiling, counted as the client's TOTAL
        sends across ALL its mailboxes in the last 24h. 0/NULL = no aggregate
        cap. Without this, N mailboxes each under the per-mailbox cap could
        together exceed the client's configured daily volume.

    Per-client serialization: a pg advisory xact lock keyed on client_id is
    taken first, so two concurrent workers for the same client can't both pass
    the ceiling check and overshoot the remaining capacity. The lock releases at
    transaction end.

    Uses SELECT FOR UPDATE SKIP LOCKED so concurrent dispatch workers never
    double-pick the same mailbox. Updates last_used_at in the same transaction.

    Raises:
        AllMailboxesCapped: the per-client daily ceiling is reached, OR warmed
            un-quarantined mailboxes exist but all are at the per-mailbox cap.
        AllMailboxesQuarantined: warmed mailboxes exist but all domains are quarantined.
        NoWarmedMailbox: no mailbox has completed warmup yet.
        NoMailboxAvailable: no mailboxes provisioned at all.
    """
    per_mailbox_cap = daily_send_cap if daily_send_cap is not None else MAX_DAILY_SEND_CAP

    # Serialize per client so concurrent workers can't both read a below-ceiling
    # count and each claim a send, overshooting the client's daily total.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:client_id))"),
        {"client_id": client_id},
    )

    # Per-client aggregate ceiling — checked before any mailbox is handed out.
    client_ceiling = _resolve_client_daily_ceiling(session, client_id)
    if client_ceiling > 0 and _client_sends_last_24h(session, client_id) >= client_ceiling:
        logger.warning(
            "mailbox_dispatcher: client_id=%s reached per-client daily ceiling of %d — deferring",
            client_id, client_ceiling,
        )
        raise AllMailboxesCapped(
            f"client_id={client_id} reached its per-client daily send ceiling of "
            f"{client_ceiling}. Defer the touch."
        )

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
            "    ( "
            "      SELECT COUNT(*) FROM sequence_touch_dispatches d "
            "      WHERE d.mailbox_id = m.id "
            "        AND d.status IN ('SENDING', 'SENT') "
            "        AND d.created_at >= NOW() - INTERVAL '24 hours' "
            "    ) + ( "
            # Speed-to-Lead auto-responses (Task 4.2.1) also consume this
            # mailbox's rolling-24h capacity — count them here, or repeated
            # inbound responses could blow past the cap while every check passes.
            # SENT_UNCONFIRMED (Group D defect D-1 fix) also counts — the SMTP
            # send happened even though the post-send status write failed, so
            # excluding it would let repeated post-send failures bypass the
            # cap entirely. responded_at is never set on that path (the write
            # that would have set it is what failed), so fall back to
            # received_at, which is always set.
            "      SELECT COUNT(*) FROM inbound_messages im "
            "      WHERE im.mailbox_id = m.id "
            "        AND im.status IN ('RESPONDED', 'SENT_UNCONFIRMED') "
            "        AND COALESCE(im.responded_at, im.received_at) >= NOW() - INTERVAL '24 hours' "
            "    ) + ( "
            # Speed-to-Lead cadence follow-ups (Task 4.2.2) also consume this
            # mailbox's rolling-24h capacity — without this a mailbox at cap
            # could still send cadence touches and burn its warmed reputation.
            "      SELECT COUNT(*) FROM stl_cadence_dispatches sd2 "
            "      WHERE sd2.mailbox_id = m.id "
            "        AND sd2.status IN ('SENDING', 'SENT', 'SENT_UNCONFIRMED') "
            "        AND sd2.created_at >= NOW() - INTERVAL '24 hours' "
            "    ) "
            "  ) < :cap "
            "ORDER BY m.last_used_at ASC NULLS FIRST "
            "LIMIT 1 "
            "FOR UPDATE OF m SKIP LOCKED"
        ),
        {"client_id": client_id, "cap": per_mailbox_cap},
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
                "mailbox_dispatcher: %d warmed/active mailbox(es) for client_id=%s but all at per-mailbox 24h cap=%d",
                warmed_active_count, client_id, per_mailbox_cap,
            )
            raise AllMailboxesCapped(
                f"{warmed_active_count} warmed/active mailbox(es) for client_id={client_id} "
                f"but all have hit the per-mailbox rolling-24h cap of {per_mailbox_cap}. Defer the touch."
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
