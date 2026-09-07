"""Post-meeting outcome capture — the write side of the 60-second Slack
modal (blueprint §3.1.7). Upserts the current-state row in
meeting_outcomes AND mirrors structured fields onto companies/contacts so
existing readers (Owner Score engine, once built) don't need to know this
table exists; also logs the immutable audit event via the shared
EventLogger (Task 1)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import text

from src.core.database import get_db_context
from src.services.events import log_event

_VALID_ATTENDANCE = {"Held", "No-Show", "Rescheduled"}


def record_outcome(
    client_id: str,
    *,
    contact_id: int,
    meeting_occurred_at: datetime,
    attendance_status: str,
    pm_software: Optional[str],
    door_count_est: Optional[int],
    objections: list[str],
    next_action: str,
    recorded_by: str,
) -> None:
    if attendance_status not in _VALID_ATTENDANCE:
        raise ValueError(f"attendance_status must be one of {_VALID_ATTENDANCE}, got {attendance_status!r}")

    with get_db_context(client_id=client_id) as session:
        session.execute(
            text(
                """
                INSERT INTO meeting_outcomes
                    (client_id, contact_id, meeting_occurred_at, attendance_status,
                     pm_software, door_count_est, objections, next_action, recorded_by)
                VALUES
                    (:client_id, :contact_id, :meeting_occurred_at, :attendance_status,
                     :pm_software, :door_count_est, :objections, :next_action, :recorded_by)
                ON CONFLICT (client_id, contact_id, meeting_occurred_at) DO UPDATE SET
                    attendance_status = EXCLUDED.attendance_status,
                    pm_software = EXCLUDED.pm_software,
                    door_count_est = EXCLUDED.door_count_est,
                    objections = EXCLUDED.objections,
                    next_action = EXCLUDED.next_action,
                    recorded_by = EXCLUDED.recorded_by,
                    updated_at = NOW()
                """
            ),
            {
                "client_id": client_id,
                "contact_id": contact_id,
                "meeting_occurred_at": meeting_occurred_at,
                "attendance_status": attendance_status,
                "pm_software": pm_software,
                "door_count_est": door_count_est,
                "objections": objections,
                "next_action": next_action,
                "recorded_by": recorded_by,
            },
        )

        session.execute(
            text("UPDATE contacts SET prospect_objections = :objections WHERE contact_id = :contact_id"),
            {"objections": objections, "contact_id": contact_id},
        )

        if pm_software or door_count_est is not None:
            session.execute(
                text(
                    """
                    UPDATE companies SET
                        current_pm_software = COALESCE(:pm_software, current_pm_software),
                        door_count_est = COALESCE(:door_count_est, door_count_est)
                    WHERE company_id = (SELECT company_id FROM contacts WHERE contact_id = :contact_id)
                    """
                ),
                {"pm_software": pm_software, "door_count_est": door_count_est, "contact_id": contact_id},
            )

    log_event(
        client_id,
        "meeting_outcome_recorded",
        entity_type="contact",
        entity_id=str(contact_id),
        actor=recorded_by,
        payload={
            "attendance_status": attendance_status,
            "pm_software": pm_software,
            "door_count_est": door_count_est,
            "objections": objections,
            "next_action": next_action,
        },
    )
