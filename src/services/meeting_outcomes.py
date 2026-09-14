"""Post-meeting outcome capture — the write side of the 60-second Slack
modal (blueprint §3.1.7). Upserts the current-state row in
meeting_outcomes AND mirrors structured fields onto companies/contacts so
existing readers (Owner Score engine, once built) don't need to know this
table exists; also logs the immutable audit event via the shared
EventLogger (Task 1)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from src.core.database import get_db_context
from src.services.events import log_event
from src.services.meeting_attendance_proof import get_attendance_provider

logger = logging.getLogger(__name__)

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

    # Attempt conference-provider attendance proof (Rule 2 of the future
    # 4-rule billing gate). The only provider today is the stub, which returns
    # None — so these stay NULL (unproven) rather than fabricated. A real
    # Google Meet / Zoom duration provider populates them once wired.
    proof = None
    if attendance_status == "Held":
        try:
            proof = get_attendance_provider().fetch_proof(
                client_id=client_id,
                contact_id=contact_id,
                meeting_occurred_at=meeting_occurred_at,
            )
        except Exception:
            # Best-effort: proof capture never blocks the outcome write. Logged
            # (not silent) so a real conference provider failing is visible
            # rather than looking identical to "no conference record".
            logger.warning(
                "meeting_outcomes: attendance-proof fetch failed for client=%s contact=%s — recording outcome without proof",
                client_id, contact_id, exc_info=True,
            )
            proof = None

    with get_db_context(client_id=client_id) as session:
        session.execute(
            text(
                """
                INSERT INTO meeting_outcomes
                    (client_id, contact_id, meeting_occurred_at, attendance_status,
                     pm_software, door_count_est, objections, next_action, recorded_by,
                     attendance_proof_ref, proof_source, meeting_duration_minutes,
                     both_parties_present, attendance_proof_verified_at)
                VALUES
                    (:client_id, :contact_id, :meeting_occurred_at, :attendance_status,
                     :pm_software, :door_count_est, :objections, :next_action, :recorded_by,
                     :attendance_proof_ref, :proof_source, :meeting_duration_minutes,
                     :both_parties_present, :attendance_proof_verified_at)
                ON CONFLICT (client_id, contact_id, meeting_occurred_at) DO UPDATE SET
                    attendance_status = EXCLUDED.attendance_status,
                    pm_software = EXCLUDED.pm_software,
                    door_count_est = EXCLUDED.door_count_est,
                    objections = EXCLUDED.objections,
                    next_action = EXCLUDED.next_action,
                    recorded_by = EXCLUDED.recorded_by,
                    -- A later resubmit only OVERWRITES proof when it carries a
                    -- real reading; a NULL (unproven) resubmit must never wipe
                    -- a previously-captured proof.
                    attendance_proof_ref = COALESCE(EXCLUDED.attendance_proof_ref, meeting_outcomes.attendance_proof_ref),
                    proof_source = COALESCE(EXCLUDED.proof_source, meeting_outcomes.proof_source),
                    meeting_duration_minutes = COALESCE(EXCLUDED.meeting_duration_minutes, meeting_outcomes.meeting_duration_minutes),
                    both_parties_present = COALESCE(EXCLUDED.both_parties_present, meeting_outcomes.both_parties_present),
                    attendance_proof_verified_at = COALESCE(EXCLUDED.attendance_proof_verified_at, meeting_outcomes.attendance_proof_verified_at),
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
                "attendance_proof_ref": proof.proof_ref if proof else None,
                "proof_source": proof.proof_source if proof else None,
                "meeting_duration_minutes": proof.duration_minutes if proof else None,
                "both_parties_present": proof.both_parties_present if proof else None,
                "attendance_proof_verified_at": proof.verified_at if proof else None,
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
