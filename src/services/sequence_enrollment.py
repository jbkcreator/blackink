"""Cross-client sequence enrollment guard (Task 3.1.1).

`may_enroll` intentionally runs under the system role (BYPASSRLS) because
the active-sequence lock must be cross-client: `sequence_runs` has a partial
unique index `ON (contact_id) WHERE status = 'ACTIVE'` that is deliberately
not filtered by client_id. RLS would otherwise make the check tenant-scoped
and miss a contact already enrolled by a different client, defeating the
one-active-sequence-per-contact invariant.

`enroll_contact` runs as the app role (RLS applies) because it writes to
tenant-bearing tables (sequence_runs, agent_work_orders) and should be
scoped to the enrolling client.

This module must NEVER be imported from src/api/ — batch-only per CLAUDE.md.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.database import get_system_db_context

logger = logging.getLogger(__name__)

# Day offsets for each of the 5 touches (blueprint §Touch sequence table).
# Touch 2 (phone call) is Day 1; Touch 4 (LinkedIn) is Day 7 — these get
# their own action_class values and are handled outside email dispatch.
_TOUCH_DAY_OFFSETS: dict[int, int] = {
    1: 0,   # Day 0 — cold email
    2: 1,   # Day 1 — phone call (Slack dial-task, different action_class)
    3: 4,   # Day 4 — cold email
    4: 7,   # Day 7 — LinkedIn deep-link (manual task)
    5: 10,  # Day 10 — cold email
}

# Only these touch steps are email dispatches that go through dispatch_touch.
_EMAIL_TOUCH_STEPS = {1, 3, 5}


def may_enroll(contact_id: int) -> bool:
    """Return False if the contact is currently ineligible for a new sequence:
    either an ACTIVE run exists, or a COMPLETED run's 30-day cooling window has
    not yet elapsed (cooling_until in the future). True otherwise. Cross-client
    by design — see module docstring."""
    with get_system_db_context() as session:
        count = session.execute(
            text(
                "SELECT COUNT(*) FROM sequence_runs "
                "WHERE contact_id = :contact_id "
                "  AND (status = 'ACTIVE' "
                "       OR (cooling_until IS NOT NULL AND cooling_until > NOW()))"
            ),
            {"contact_id": contact_id},
        ).scalar()
    return (count or 0) == 0


def enroll_contact(
    session: Session,
    client_id: str,
    contact_id: int,
    contact_email: str,
    enrolled_at: Optional[datetime] = None,
) -> Optional[str]:
    """Create a sequence_run and enqueue all 5 touch work orders upfront.

    Returns the new run_id, or None if the contact is already enrolled
    (cross-client check via may_enroll). All 5 orders are enqueued with
    their due_at timestamps so the sequence_sweep can gate card-posting on
    day-grain timing — per MAP.md ticket 07 decision.

    Must be called within a tenant-scoped session (app role, RLS applies).
    The cross-client may_enroll check uses a separate system-role connection.
    """
    import uuid

    from sqlalchemy.exc import IntegrityError

    from src.services import work_orders as wo

    if not may_enroll(contact_id):
        logger.info("enroll_contact: contact_id=%s already in active sequence", contact_id)
        return None

    now = enrolled_at or datetime.now(timezone.utc)

    # may_enroll runs on a separate (system) connection, so between that read
    # and this INSERT a second client can enroll the same contact. The partial
    # unique index uq_sequence_runs_one_active is the real backstop — catch its
    # violation and return None rather than crashing the loser of the race.
    run_id = str(uuid.uuid4())
    try:
        with session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO sequence_runs (run_id, client_id, contact_id, status, enrolled_at) "
                    "VALUES (:run_id, :client_id, :contact_id, 'ACTIVE', :now)"
                ),
                {"run_id": run_id, "client_id": client_id, "contact_id": contact_id, "now": now},
            )
    except IntegrityError:
        logger.info(
            "enroll_contact: contact_id=%s enrolled concurrently by another client — losing the race",
            contact_id,
        )
        return None

    for touch_step, day_offset in _TOUCH_DAY_OFFSETS.items():
        due_at = now + timedelta(days=day_offset)
        is_email = touch_step in _EMAIL_TOUCH_STEPS
        action_class = (
            "DISPATCH_EMAIL_TOUCH" if is_email
            else "DIAL_TASK" if touch_step == 2
            else "LINKEDIN_TASK"
        )
        # config_fingerprint["channel"] selects the EXECUTION dispatcher (see
        # work_orders/dispatchers.py DISPATCHERS). Email touches run the real
        # send path ("setter"); phone/LinkedIn are human-performed and run the
        # "manual" dispatcher that only records completion (finding #4) — so an
        # approved DIAL_TASK is never fed to the email sender.
        channel = "setter" if is_email else "manual"
        idempotency_key = f"seq:{run_id}:touch:{touch_step}"
        wo.enqueue(
            client_id=client_id,
            entity_type="contact",
            entity_id=str(contact_id),
            agent_id="cold_outbound_sequencer",
            action_class=action_class,
            autonomy_band="BAND_2_ONE_TAP",
            risk_class="LOW",
            recipient=contact_email,
            payload={"run_id": run_id, "touch_step": touch_step},
            config_fingerprint={"channel": channel, "run_id": run_id, "touch_step": touch_step},
            idempotency_key=idempotency_key,
            due_at=due_at,
        )

    logger.info(
        "enroll_contact: enrolled contact_id=%s client_id=%s run_id=%s",
        contact_id, client_id, run_id,
    )
    return run_id
