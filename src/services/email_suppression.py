"""Deterministic opt-out suppression — cross-channel, no LLM involvement.

Ported from Forced Action's email_suppression.py (ADR 0028 cross-channel-
cascade shape) and adapted for Blackink's data model:

- Blackink contacts table is the single, global, deduplicated identity record
  per company+role — no separate per-channel identity tables. Suppressing a
  contact IS suppressing all channels for that contact.
- `suppress_contact` sets both is_opted_out AND suppression_state so every
  path that checks either column is blocked, same as FA's all-or-nothing rule.
- All suppression writes log an event to the events ledger for audit trail.
- Does not commit — caller's session_scope() owns the transaction.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core single-contact suppression
# ---------------------------------------------------------------------------

def suppress_contact(session: Session, contact_id: int, reason: str) -> None:
    """Deterministic opt-out — sets is_opted_out + suppression_state + logs event.

    Zero human touch, zero LLM involvement. Idempotent — safe to call multiple
    times. Logs a suppression event to the events ledger for audit trail.
    Does not commit.
    """
    session.execute(
        text(
            "UPDATE contacts "
            "SET is_opted_out = TRUE, suppression_state = TRUE, updated_at = NOW() "
            "WHERE contact_id = :contact_id"
        ),
        {"contact_id": contact_id},
    )

    # Fetch client_id for the events ledger (contacts are global but events are tenant-scoped)
    row = session.execute(
        text("SELECT company_id FROM contacts WHERE contact_id = :contact_id"),
        {"contact_id": contact_id},
    ).fetchone()

    if row:
        _log_suppression_event(
            session=session,
            contact_id=contact_id,
            company_id=row[0],
            reason=reason,
        )

    logger.info("suppression: contact_id=%s reason=%s", contact_id, reason)


def is_suppressed(session: Session, contact_id: int) -> bool:
    """Return True if contact is suppressed on any channel."""
    row = session.execute(
        text(
            "SELECT is_opted_out, suppression_state "
            "FROM contacts WHERE contact_id = :contact_id"
        ),
        {"contact_id": contact_id},
    ).fetchone()
    if row is None:
        return False
    return bool(row[0] or row[1])


# ---------------------------------------------------------------------------
# Lookup-based suppression (by email or phone identifier)
# ---------------------------------------------------------------------------

def suppress_by_email(session: Session, email: str, reason: str) -> int:
    """Suppress all contacts with this email address. Returns count suppressed."""
    rows = session.execute(
        text("SELECT contact_id FROM contacts WHERE lower(email) = :email"),
        {"email": email.strip().lower()},
    ).fetchall()
    for row in rows:
        suppress_contact(session, row[0], reason)
    if rows:
        logger.info("suppression by email: email=%s count=%d reason=%s", email, len(rows), reason)
    return len(rows)


def _normalize_phone(phone: str) -> str:
    """Strip to digits only, drop leading country code 1 — matches sms_quiet_hours convention."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    return digits


def suppress_by_phone(session: Session, phone: str, reason: str) -> int:
    """Suppress all contacts with this phone number. Returns count suppressed.

    Normalizes both sides of the comparison so +18135550100, 8135550100,
    and (813) 555-0100 all resolve to the same contact.
    """
    normalized = _normalize_phone(phone)
    rows = session.execute(
        text(
            "SELECT contact_id FROM contacts "
            "WHERE regexp_replace(phone, '[^0-9]', '', 'g') = :phone "
            "   OR regexp_replace(phone, '^1?([0-9]{10})$', '\\1') = :phone"
        ),
        {"phone": normalized},
    ).fetchall()
    for row in rows:
        suppress_contact(session, row[0], reason)
    if rows:
        logger.info("suppression by phone: phone=%s count=%d reason=%s", phone, len(rows), reason)
    return len(rows)


# ---------------------------------------------------------------------------
# Domain-level suppression
# ---------------------------------------------------------------------------

def suppress_by_domain(session: Session, domain: str, reason: str) -> int:
    """Suppress all contacts at a company domain. Returns count suppressed.

    Used when a company explicitly opts out (e.g. sends STOP, files complaint)
    — suppresses both OWNER_BROKER_MD and OFFICE_MANAGER_OPS contacts.
    """
    rows = session.execute(
        text(
            "SELECT c.contact_id FROM contacts c "
            "JOIN companies co ON co.company_id = c.company_id "
            "WHERE co.domain = :domain"
        ),
        {"domain": domain.strip().lower()},
    ).fetchall()
    for row in rows:
        suppress_contact(session, row[0], reason)
    if rows:
        logger.info(
            "suppression by domain: domain=%s count=%d reason=%s", domain, len(rows), reason
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Bulk suppression
# ---------------------------------------------------------------------------

def bulk_suppress(
    session: Session,
    contact_ids: list[int],
    reason: str,
) -> int:
    """Suppress a list of contacts in a single batch. Returns count suppressed.

    Used for DNC import lists and compliance scrubs. Batches the UPDATE to
    avoid N individual round-trips. Still logs one event per contact for
    full audit trail.
    """
    if not contact_ids:
        return 0

    session.execute(
        text(
            "UPDATE contacts "
            "SET is_opted_out = TRUE, suppression_state = TRUE, updated_at = NOW() "
            "WHERE contact_id = ANY(:ids)"
        ),
        {"ids": contact_ids},
    )

    # Fetch company_ids for event logging
    rows = session.execute(
        text(
            "SELECT contact_id, company_id FROM contacts "
            "WHERE contact_id = ANY(:ids)"
        ),
        {"ids": contact_ids},
    ).fetchall()

    for contact_id, company_id in rows:
        _log_suppression_event(session, contact_id, company_id, reason)

    logger.info("bulk_suppress: count=%d reason=%s", len(contact_ids), reason)
    return len(contact_ids)


# ---------------------------------------------------------------------------
# DNC import
# ---------------------------------------------------------------------------

def import_dnc_list(
    session: Session,
    phones: list[str],
    source: str = "dnc_import",
) -> dict:
    """Mark contacts from a DNC phone list as suppressed.

    Returns summary: {"matched": N, "unmatched": N}
    Unmatched phones (no contact record) are logged but not errored —
    they may arrive before the contact is ingested.
    """
    matched = 0
    unmatched = 0
    for phone in phones:
        count = suppress_by_phone(session, _normalize_phone(phone.strip()), reason=source)
        if count:
            matched += count
        else:
            unmatched += 1
            logger.debug("dnc_import: no contact found for phone=%s", phone)

    logger.info(
        "dnc_import: source=%s matched=%d unmatched=%d", source, matched, unmatched
    )
    return {"matched": matched, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log_suppression_event(
    session: Session,
    contact_id: int,
    company_id: Optional[str],
    reason: str,
) -> None:
    """Append a suppression event to the events ledger.

    client_id is NULL for suppression events — suppression is platform-wide,
    not tenant-scoped. The events table allows NULL client_id for system-level
    audit records.
    """
    try:
        session.execute(
            text(
                "INSERT INTO events "
                "(client_id, event_type, entity_type, entity_id, payload, actor, created_at) "
                "VALUES "
                "(NULL, 'contact_suppressed', 'contact', :entity_id, "
                " jsonb_build_object('reason', :reason, 'company_id', :company_id), "
                " 'suppression_gate', :now)"
            ),
            {
                "entity_id": str(contact_id),
                "reason": reason,
                "company_id": company_id,
                "now": datetime.now(timezone.utc),
            },
        )
    except Exception:
        # Best-effort — suppression write must never be blocked by event log failure
        logger.warning(
            "suppression event log failed for contact_id=%s", contact_id, exc_info=True
        )
