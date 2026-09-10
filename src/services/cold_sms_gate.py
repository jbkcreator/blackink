"""Cold SMS gate — deterministic pre-send block.

Architectural invariant (Dev 4 / Week 0 AC #5):
  Outbound SMS to a contact where inbound_sms_count = 0 AND
  booked_appointment_id IS NULL is ALWAYS blocked.

Cold contacts have no prior engagement — sending SMS to them violates A2P
10DLC campaign use-case ("Customer Care + Account Notification only after
inbound contact or confirmed booking"). CI/CD build fails if this gate is
bypassed — see tests/test_cold_sms_gate.py.

Both signals are derived from the events ledger (source of record) so no
denormalized columns are needed on contacts.

Code-review correction (2026-09-10): this docstring previously called for a
BYPASSRLS/system session, on the reasoning that the gate must see events
across all channels regardless of which client initiated them. That was
never actually exercised — before the D-7 fix below, this module had zero
production callers. The two real callers wired in by that fix
(campaign_readiness_gate.evaluate_full_readiness(), sms_dispatch.dispatch_sms())
both pass their own regular RLS-scoped, per-tenant session — matching every
other check in both of those gates (DNC, opt-out, quiet hours), not a
cross-tenant BYPASSRLS one. Accepted as correct: per docs/
SOURCE_OF_TRUTH_RECONCILIATION.md §1.3, outbound SMS is cancelled for all of
2026 ("No SMS this year" — W1-4) and this module "remain[s] correct as a
block but has nothing to gate this year" — StubSmsProvider unconditionally
raises NotImplementedError regardless of engagement, so the narrow edge
case a BYPASSRLS session would additionally catch (a contact's inbound-SMS
event logged under a company's PRIOR owning client_id, before a
county_allocation_reassessment.py reassignment) has no live consequence
this year. Revisit this decision if/when a real SMS vendor is contracted
and this gate starts actually gating real sends.
"""

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Event types that constitute inbound SMS engagement
_INBOUND_SMS_EVENT_TYPES = ("reply_received", "sms_inbound")

# Event types that constitute a confirmed booking
_BOOKING_EVENT_TYPES = ("meeting_booked",)


def get_inbound_sms_count(session: Session, contact_id: int) -> int:
    """Count inbound SMS events for a contact from the events ledger."""
    row = session.execute(
        text(
            "SELECT COUNT(*) FROM events "
            "WHERE entity_type = 'contact' "
            "AND entity_id = :entity_id "
            "AND event_type = ANY(:event_types)"
        ),
        {
            "entity_id": str(contact_id),
            "event_types": list(_INBOUND_SMS_EVENT_TYPES),
        },
    ).fetchone()
    return int(row[0]) if row else 0


def get_booked_appointment_id(session: Session, contact_id: int) -> Optional[str]:
    """Return the most recent booked appointment event ID, or None."""
    row = session.execute(
        text(
            "SELECT id FROM events "
            "WHERE entity_type = 'contact' "
            "AND entity_id = :entity_id "
            "AND event_type = ANY(:event_types) "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {
            "entity_id": str(contact_id),
            "event_types": list(_BOOKING_EVENT_TYPES),
        },
    ).fetchone()
    return str(row[0]) if row else None


def is_cold_contact(session: Session, contact_id: int) -> bool:
    """Return True if contact has no inbound SMS AND no booked appointment.

    A cold contact must never receive outbound SMS — only email is permitted
    as the first cold touch (A2P 10DLC compliance).
    """
    inbound_count = get_inbound_sms_count(session, contact_id)
    if inbound_count > 0:
        return False
    booked = get_booked_appointment_id(session, contact_id)
    return booked is None


def assert_not_cold_sms(session: Session, contact_id: int) -> None:
    """Raise ValueError if this contact is cold (no inbound SMS + no booking).

    Called as the final pre-dispatch gate before any outbound SMS is sent.
    This is the runtime enforcement that CI/CD tests verify is present.

    Raises:
        ValueError: with contact_id and reason — caller logs and routes to
                    dead-letter queue, never swallows silently.
    """
    inbound_count = get_inbound_sms_count(session, contact_id)
    booked_id = get_booked_appointment_id(session, contact_id)

    if inbound_count == 0 and booked_id is None:
        logger.warning(
            "cold_sms_gate: blocked outbound SMS to cold contact contact_id=%s "
            "(inbound_sms_count=0, booked_appointment_id=NULL)",
            contact_id,
        )
        raise ValueError(
            f"Cold SMS blocked for contact_id={contact_id}: "
            "inbound_sms_count=0 AND booked_appointment_id IS NULL. "
            "SMS is only permitted after inbound engagement or a confirmed booking."
        )
