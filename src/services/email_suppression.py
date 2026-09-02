"""Deterministic opt-out suppression — cross-channel, no LLM involvement.

Ported from Forced Action's email_suppression.py (ADR 0028 cross-channel-
cascade shape) and adapted for Blackink's data model:

- Blackink contacts table is the single, global, deduplicated identity record
  per company+role — no separate per-channel identity tables. Suppressing a
  contact IS suppressing all channels for that contact.
- `suppress_contact` sets both is_opted_out AND suppression_state so every
  path that checks either column is blocked, same as FA's all-or-nothing rule.
- The contacts row is the suppression record of truth. Suppression is global
  (cross-tenant); it is not written to the tenant-scoped events ledger.
- Does not commit — caller's session_scope() owns the transaction.
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core single-contact suppression
# ---------------------------------------------------------------------------

def suppress_contact(session: Session, contact_id: int, reason: str) -> None:
    """Deterministic opt-out — sets is_opted_out + suppression_state on the contact.

    Zero human touch, zero LLM involvement. Idempotent — safe to call multiple
    times. The contacts row is the suppression record of truth; suppression is
    global (cross-tenant), so it is NOT written to the tenant-scoped events
    ledger (events.client_id is NOT NULL). Does not commit.
    """
    session.execute(
        text(
            "UPDATE contacts "
            "SET is_opted_out = TRUE, suppression_state = TRUE, updated_at = NOW() "
            "WHERE contact_id = :contact_id"
        ),
        {"contact_id": contact_id},
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
    # Normalize the stored value the same way _normalize_phone does: strip all
    # non-digits, then drop a leading US country code 1. This makes E.164
    # (+18135550100), 11-digit (18135550100), and formatted ((813) 555-0100)
    # stored values all compare equal to the 10-digit normalized input.
    rows = session.execute(
        text(
            "SELECT contact_id FROM contacts "
            "WHERE regexp_replace("
            "        regexp_replace(phone, '[^0-9]', '', 'g'), "
            "        '^1([0-9]{10})$', '\\1'"
            "      ) = :phone"
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
    avoid N individual round-trips. Does not commit.
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
