"""Sequence halt service — global opt-out for a contact (Task 3.1.3).

halt_sequence_for_contact() is the single service boundary for the Mark
Opt-Out action on #sales-replies cards (ticket 27). It makes three
writes in one session:

  1. contacts.is_opted_out = TRUE     — the global flag; evaluate_touch_gate
     (via compliance_gate) already checks this and returns FAIL, so all
     future touches for this contact under any client are blocked
     automatically without any additional gate changes.

  2. sequence_runs.status = 'HALTED'  — belt: terminates the active run
     with no cooling_until (permanence comes from is_opted_out, not the
     run state). HALTED is a new terminal status added to sequence_runs via
     apply_sequence_runs.py's idempotent ALTER TABLE.

  3. agent_work_orders CANCELLED      — suspenders: cancels any QUEUED or
     SNOOZED orders referencing this run so stale cards in Slack can no
     longer be actioned (the run is gone; there is nothing left to send).

Uses blackink_system (BYPASSRLS) because opt-out is cross-client:
  - contacts is join-scoped through companies.owning_client_id — the
    app role can only UPDATE contacts for its current client, but a
    contact opting out must be globally flagged regardless of which
    client initiated the conversation.
  - sequence_runs / agent_work_orders may belong to a different client
    than the one handling the inbound reply (ownership can shift after
    a county reallocation — the halt must still reach the right run).

blackink_system is NOT imported from src/api/ (CLAUDE.md invariant is
honoured — this module lives in src/services/, not src/api/).
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.core.database import get_system_db_context

logger = logging.getLogger(__name__)


def halt_sequence_for_contact(contact_id: int) -> None:
    """Globally halt all active sequences for a contact and mark them opted out.

    Idempotent: re-calling on an already-opted-out contact is safe (UPDATE
    where is_opted_out=FALSE is a no-op; same for HALTED runs and CANCELLED
    work orders).

    Args:
        contact_id: The contacts.contact_id (BigInt) of the contact to halt.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: on DB error (caller should catch and
            surface to the operator, not silently swallow).
    """
    with get_system_db_context() as session:
        # 1. Global opt-out flag — blocks all future sends under any client.
        session.execute(
            text(
                "UPDATE contacts SET is_opted_out = TRUE, updated_at = NOW() "
                "WHERE contact_id = :contact_id AND is_opted_out = FALSE"
            ),
            {"contact_id": contact_id},
        )

        # 2. Halt any ACTIVE sequence runs (cross-client — BYPASSRLS).
        run_rows = session.execute(
            text(
                "UPDATE sequence_runs "
                "SET status = 'HALTED', updated_at = NOW() "
                "WHERE contact_id = :contact_id AND status = 'ACTIVE' "
                "RETURNING run_id::text, client_id"
            ),
            {"contact_id": contact_id},
        ).mappings().all()

        halted_runs = list(run_rows)
        logger.info(
            "[sequence_halt] contact_id=%d halted %d run(s)",
            contact_id,
            len(halted_runs),
        )

        # 3. Cancel pending work orders for the halted runs so stale Slack
        #    cards cannot be actioned. payload->>'run_id' is a JSONB text
        #    lookup — works because run_id is stored as a string in the payload.
        for row in halted_runs:
            run_id = row["run_id"]
            client_id = row["client_id"]
            result = session.execute(
                text(
                    "UPDATE agent_work_orders "
                    "SET status = 'CANCELLED', updated_at = NOW() "
                    "WHERE client_id = :client_id "
                    "  AND status IN ('QUEUED', 'SNOOZED') "
                    "  AND payload->>'run_id' = :run_id"
                ),
                {"client_id": client_id, "run_id": run_id},
            )
            logger.info(
                "[sequence_halt] cancelled %d pending work order(s) for run %s",
                result.rowcount,
                run_id[:8],
            )

        session.commit()
