"""Cross-client sequence enrollment guard (Task 3.1.1).

`may_enroll` intentionally runs under the system role (BYPASSRLS) because
the active-sequence lock must be cross-client: `sequence_runs` has a partial
unique index `ON (contact_id) WHERE status = 'ACTIVE'` that is deliberately
not filtered by client_id. RLS would otherwise make the check tenant-scoped
and miss a contact already enrolled by a different client, defeating the
one-active-sequence-per-contact invariant.

This module must NEVER be imported from src/api/ — system-role queries are
batch-only per CLAUDE.md.
"""

from sqlalchemy import text

from src.core.database import get_system_db_context


def may_enroll(contact_id: int) -> bool:
    """Return False if any client already has an ACTIVE sequence_run for this
    contact, True otherwise. Cross-client by design — see module docstring."""
    with get_system_db_context() as session:
        count = session.execute(
            text(
                "SELECT COUNT(*) FROM sequence_runs "
                "WHERE contact_id = :contact_id AND status = 'ACTIVE'"
            ),
            {"contact_id": contact_id},
        ).scalar()
    return (count or 0) == 0
