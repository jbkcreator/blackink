"""Post-meeting outcome recording with freshness-guarded intelligence mirroring.

Records every Slack post-meeting form submission into meeting_outcomes, then
mirrors prospect intelligence (objections, PM software, door count) onto the
canonical contacts and companies rows — but ONLY when the submitted meeting is
at least as recent as the latest existing outcome for that contact.

Without the guard, a late correction or out-of-order submission of an older
meeting would silently overwrite the current intelligence used by OVS scoring,
campaign sequencing, and the setter context card.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

_ATTENDANCE_STATUSES = frozenset({"ATTENDED", "NO_SHOW", "RESCHEDULED", "CANCELLED"})


@dataclass(frozen=True)
class MeetingOutcomeRecord:
    client_id: str
    contact_id: int
    company_id: str
    meeting_occurred_at: datetime
    attendance_status: str
    pm_software_stated: Optional[str] = None
    door_count_stated: Optional[int] = None
    objections_stated: Optional[str] = None
    next_action: Optional[str] = None
    submitted_by: Optional[str] = None


def record_meeting_outcome(session: Session, record: MeetingOutcomeRecord) -> int:
    """Insert one meeting outcome and conditionally mirror intelligence fields.

    Returns the new outcome_id. Does not commit — caller owns the transaction.

    Mirroring contacts.prospect_objections and companies.current_pm_software /
    door_count_est is skipped when a newer meeting already exists for that
    contact (or any contact at that company), preventing a late correction from
    overwriting more current intelligence.
    """
    if record.attendance_status not in _ATTENDANCE_STATUSES:
        raise ValueError(
            f"attendance_status must be one of {sorted(_ATTENDANCE_STATUSES)}, "
            f"got {record.attendance_status!r}"
        )

    outcome_id = _insert_outcome(session, record)

    # After insert, the current row is included in both MAX queries — so each
    # returns at least record.meeting_occurred_at when no newer row exists.
    latest_contact = _latest_for_contact(session, record.contact_id)
    if latest_contact is None or record.meeting_occurred_at >= latest_contact:
        _mirror_contact(session, record.contact_id, record.objections_stated)

    latest_company = _latest_for_company(session, record.company_id)
    if latest_company is None or record.meeting_occurred_at >= latest_company:
        _mirror_company(
            session, record.company_id, record.pm_software_stated, record.door_count_stated
        )

    return outcome_id


# ---------------------------------------------------------------------------
# Internals — one SQL statement each, kept separate for testability
# ---------------------------------------------------------------------------

def _insert_outcome(session: Session, record: MeetingOutcomeRecord) -> int:
    row = session.execute(
        text("""
            INSERT INTO meeting_outcomes (
                client_id, contact_id, company_id, meeting_occurred_at,
                attendance_status, pm_software_stated, door_count_stated,
                objections_stated, next_action, submitted_by
            ) VALUES (
                :client_id, :contact_id, :company_id, :meeting_occurred_at,
                :attendance_status, :pm_software_stated, :door_count_stated,
                :objections_stated, :next_action, :submitted_by
            )
            RETURNING outcome_id
        """),
        {
            "client_id": record.client_id,
            "contact_id": record.contact_id,
            "company_id": record.company_id,
            "meeting_occurred_at": record.meeting_occurred_at,
            "attendance_status": record.attendance_status,
            "pm_software_stated": record.pm_software_stated,
            "door_count_stated": record.door_count_stated,
            "objections_stated": record.objections_stated,
            "next_action": record.next_action,
            "submitted_by": record.submitted_by,
        },
    )
    return row.scalar_one()


def _latest_for_contact(session: Session, contact_id: int) -> Optional[datetime]:
    """MAX meeting_occurred_at across all outcomes for this contact."""
    return session.execute(
        text(
            "SELECT MAX(meeting_occurred_at) FROM meeting_outcomes"
            " WHERE contact_id = :cid"
        ),
        {"cid": contact_id},
    ).scalar()


def _latest_for_company(session: Session, company_id: str) -> Optional[datetime]:
    """MAX meeting_occurred_at across all outcomes for any contact at this company."""
    return session.execute(
        text(
            "SELECT MAX(meeting_occurred_at) FROM meeting_outcomes"
            " WHERE company_id = :coid"
        ),
        {"coid": company_id},
    ).scalar()


def _mirror_contact(session: Session, contact_id: int, objections: Optional[str]) -> None:
    # Two writes, each a no-op for the other's case, so this is correct
    # regardless of the caller's tenant scope:
    #   * the plain UPDATE mirrors for a normally-allocated tenant, whose
    #     own session can see its own contacts row through RLS;
    #   * the SECURITY DEFINER function (apply_meeting_outcome_prompt_jobs.py)
    #     mirrors for a session scoped to BLACKINK_INTERNAL_SALES, whose
    #     prospect companies have owning_client_id = NULL and are therefore
    #     RLS-invisible — so the plain UPDATE above silently matches ZERO
    #     rows for that (only real) caller of this path today.
    # The function returns early for any non-internal-sales scope, so it can
    # never double-write; the plain UPDATE matches nothing under internal
    # sales, so it never does either.
    session.execute(
        text(
            "UPDATE contacts"
            " SET prospect_objections = :objections, updated_at = NOW()"
            " WHERE contact_id = :cid"
        ),
        {"objections": objections, "cid": contact_id},
    )
    session.execute(
        text("SELECT mirror_contact_objections(:cid, :objections)"),
        {"cid": contact_id, "objections": objections},
    )


def _mirror_company(
    session: Session,
    company_id: str,
    pm_software: Optional[str],
    door_count: Optional[int],
) -> None:
    # COALESCE keeps the existing value when the submitted field is NULL,
    # so a meeting where the rep didn't ask about PM software doesn't erase
    # a value captured in an earlier meeting. See _mirror_contact for why
    # both this plain UPDATE and the SECURITY DEFINER function are issued —
    # exactly one of them writes for any given caller's tenant scope.
    session.execute(
        text(
            "UPDATE companies"
            " SET current_pm_software = COALESCE(:pm_software, current_pm_software),"
            "     door_count_est       = COALESCE(:door_count, door_count_est),"
            "     updated_at           = NOW()"
            " WHERE company_id = :coid"
        ),
        {"pm_software": pm_software, "door_count": door_count, "coid": company_id},
    )
    session.execute(
        text("SELECT mirror_company_intelligence(:coid, :pm_software, :door_count)"),
        {"coid": company_id, "pm_software": pm_software, "door_count": door_count},
    )
