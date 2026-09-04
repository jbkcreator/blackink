"""Two-tier attribution for inbound forwarded replies (Task 3.1.3 / ticket 20).

Attribution resolves a forwarded email to a (client, contact, run) triple:

  Tier 1 — In-Reply-To match:
    Parse the In-Reply-To (and References) header from the forwarded mail.
    Look up the value in sequence_touch_dispatches.message_id. If found,
    that gives run_id → contact_id directly. Exact, collision-free.

  Tier 2 — sender-email match:
    Match the original From address against contacts.email, scoped to the
    client derived from the to_alias. Covers fresh inbound (no prior touch)
    that has no thread header.

  Neither → 'unattributed':
    Post an unattributed card to #sales-replies. Never guess — mis-attribution
    would leak one prospect's message into another's thread.

BCC-loop break (ticket 28 Q6):
    Before any attribution, check if the inbound message_id matches one of our
    own outbound sequence_touch_dispatches.message_id. If so, it is our own
    BCC echoing back through the client's forward — drop it entirely (no card,
    no storage). This is checked at the router layer before calling this module.

Client resolution from alias:
    The to_alias is expected to be {client_id}@inbound.getblackink.com.
    The client_id is extracted as the handle portion (before '@') and validated
    against the clients table.

Per CLAUDE.md: all DB reads use sqlalchemy.text() with named binds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def strip_display_name(from_raw: str) -> str:
    """Reduce a From header to a bare email address.

    'First Last <email@example.com>' → 'email@example.com'. If there is no
    angle-bracket form, the trimmed input is returned unchanged.
    """
    value = (from_raw or "").strip()
    if "<" in value and value.endswith(">"):
        return value[value.rfind("<") + 1 : -1].strip() or value
    return value


@dataclass
class AttributionResult:
    """Resolved attribution for one inbound forwarded reply."""

    client_id: str                   # always set — derived from alias
    contact_id: Optional[int]        # None if unattributed
    run_id: Optional[str]            # None if unattributed or sender-email tier
    touch_step: Optional[int]        # None if unattributed or sender-email tier
    attribution_status: str          # 'attributed' or 'unattributed'
    contact_name: Optional[str]      # first + last for card display
    firm_name: Optional[str]         # company_name for card display


def resolve_client_from_alias(session: Session, to_alias: str) -> Optional[str]:
    """Extract client_id from a Blackink inbound alias.

    Expected format: {client_id}@inbound.getblackink.com
    The handle (before '@') is used as the client_id and validated against
    the clients table.

    Returns None if the alias format is invalid or the client does not exist.
    Note: session is scoped with get_system_db_context (BYPASSRLS) by the
    caller because the clients table check is cross-tenant by nature.
    """
    if "@" not in to_alias:
        return None
    handle = to_alias.split("@")[0]
    if not handle:
        return None
    row = session.execute(
        text("SELECT client_id FROM clients WHERE client_id = :handle AND is_active = TRUE"),
        {"handle": handle},
    ).first()
    return handle if row else None


def is_bcc_echo(session: Session, message_id: str) -> bool:
    """Return True if this message_id matches one of our own outbound sends.

    The client BCCs themselves on every send and forwards their inbox to us,
    so our outbound BCC can arrive at the alias and appear to be an inbound
    reply. We detect this by checking sequence_touch_dispatches — if the
    message_id belongs to one of our sends, drop it (no card, no storage).

    Uses get_system_db_context (BYPASSRLS) to check across all clients.
    """
    row = session.execute(
        text(
            "SELECT 1 FROM sequence_touch_dispatches "
            "WHERE message_id = :message_id AND status = 'SENT' "
            "LIMIT 1"
        ),
        {"message_id": message_id},
    ).first()
    return row is not None


def attribute(
    session: Session,
    *,
    client_id: str,
    in_reply_to: Optional[str],
    from_address: str,
) -> AttributionResult:
    """Resolve attribution for a forwarded inbound reply.

    Args:
        session: DB session with BYPASSRLS (get_system_db_context) — needed
            because we join across contacts (join-mode RLS) and check
            sequence_touch_dispatches across any client.
        client_id: The client derived from the to_alias (already validated).
        in_reply_to: The In-Reply-To (or first References) header value, if any.
        from_address: The original From address of the inbound message.

    Returns:
        AttributionResult with all resolved fields, or unattributed if neither
        tier matched.
    """
    _base = AttributionResult(
        client_id=client_id,
        contact_id=None,
        run_id=None,
        touch_step=None,
        attribution_status="unattributed",
        contact_name=None,
        firm_name=None,
    )

    # Tier 1: In-Reply-To → sequence_touch_dispatches → run → contact
    if in_reply_to:
        tier1 = _attribute_by_message_id(session, client_id=client_id, message_id=in_reply_to)
        if tier1 is not None:
            return tier1

    # Tier 2: sender email → contacts.email scoped to client
    if from_address:
        tier2 = _attribute_by_sender_email(session, client_id=client_id, from_address=from_address)
        if tier2 is not None:
            return tier2

    return _base


def _attribute_by_message_id(
    session: Session,
    *,
    client_id: str,
    message_id: str,
) -> Optional[AttributionResult]:
    """Tier 1: In-Reply-To header → sequence_touch_dispatches → contact."""
    row = session.execute(
        text(
            "SELECT std.run_id::text, std.touch_step, std.client_id, "
            "       sr.contact_id, "
            "       c.first_name, c.last_name, co.company_name "
            "FROM sequence_touch_dispatches std "
            "JOIN sequence_runs sr ON sr.run_id = std.run_id "
            "JOIN contacts c ON c.contact_id = sr.contact_id "
            "JOIN companies co ON co.company_id = c.company_id "
            "WHERE std.message_id = :message_id AND std.status = 'SENT' "
            "LIMIT 1"
        ),
        {"message_id": message_id},
    ).mappings().first()

    if row is None:
        return None

    first = row["first_name"] or ""
    last = row["last_name"] or ""
    contact_name = f"{first} {last}".strip() or None

    logger.debug(
        "[inbound_attribution] tier1 match: message_id=%s → run=%s touch=%s contact=%s",
        message_id[:20],
        str(row["run_id"])[:8],
        row["touch_step"],
        row["contact_id"],
    )
    return AttributionResult(
        client_id=row["client_id"],  # use the client_id from the dispatch, not alias
        contact_id=row["contact_id"],
        run_id=row["run_id"],
        touch_step=row["touch_step"],
        attribution_status="attributed",
        contact_name=contact_name,
        firm_name=row["company_name"],
    )


def _attribute_by_sender_email(
    session: Session,
    *,
    client_id: str,
    from_address: str,
) -> Optional[AttributionResult]:
    """Tier 2: from_address → contacts.email, scoped to the alias's client.

    Because contacts is join-scoped via companies.owning_client_id, and we're
    using BYPASSRLS, this correctly limits to the client's own contacts.
    """
    # Normalize: strip display name from "First Last <email@example.com>"
    email = strip_display_name(from_address)

    row = session.execute(
        text(
            "SELECT c.contact_id, c.first_name, c.last_name, co.company_name "
            "FROM contacts c "
            "JOIN companies co ON co.company_id = c.company_id "
            "WHERE LOWER(c.email) = LOWER(:email) "
            "  AND co.owning_client_id = :client_id "
            "LIMIT 1"
        ),
        {"email": email, "client_id": client_id},
    ).mappings().first()

    if row is None:
        return None

    first = row["first_name"] or ""
    last = row["last_name"] or ""
    contact_name = f"{first} {last}".strip() or None

    logger.debug(
        "[inbound_attribution] tier2 match: from=%s → contact=%s",
        email,
        row["contact_id"],
    )
    return AttributionResult(
        client_id=client_id,
        contact_id=row["contact_id"],
        run_id=None,  # sender-email tier has no run context
        touch_step=None,
        attribution_status="attributed",
        contact_name=contact_name,
        firm_name=row["company_name"],
    )
