"""Deterministic opt-out suppression.

Adapted from Forced Action's cross-channel-cascade shape (ADR 0028 /
email_suppression.py) but simpler here: Blackink's contacts table is
already the single, global, deduplicated identity record per company+role
(one row holding both email and phone) — there is no separate per-channel
identity to cascade across, so suppressing "any channel" IS suppressing the
contact record directly. Does not commit — caller's session_scope() owns
the transaction, same convention as FA's suppress_contact().
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


def suppress_contact(session: Session, contact_id: int, reason: str) -> None:
	"""Deterministic opt-out write — zero human touch, zero LLM involvement.
	Sets both is_opted_out and suppression_state so the contact is blocked
	by every path that checks either column."""
	session.execute(
		text(
			"UPDATE contacts SET is_opted_out = TRUE, suppression_state = TRUE, "
			"updated_at = NOW() WHERE contact_id = :contact_id"
		),
		{"contact_id": contact_id},
	)


def is_suppressed(session: Session, contact_id: int) -> bool:
	row = session.execute(
		text("SELECT is_opted_out, suppression_state FROM contacts WHERE contact_id = :contact_id"),
		{"contact_id": contact_id},
	).fetchone()
	if row is None:
		return False
	return bool(row.is_opted_out or row.suppression_state)
