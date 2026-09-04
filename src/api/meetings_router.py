"""Internal read endpoint for meeting outcomes review.

Queries meeting_outcomes joined with contacts + companies for display names.
Uses system role (BYPASSRLS) — this is a cross-client internal ops view.
No auth guard yet.
"""
from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from src.core.database import get_system_db_context

router = APIRouter(prefix="/api/meetings", tags=["meetings"])

_OUTCOMES_SQL = """
    SELECT
        mo.id,
        mo.client_id,
        mo.contact_id,
        mo.meeting_occurred_at,
        mo.attendance_status,
        mo.pm_software,
        mo.door_count_est,
        mo.objections,
        mo.next_action,
        mo.recorded_by,
        mo.created_at,
        c.first_name || ' ' || c.last_name AS contact_name,
        co.company_name
    FROM meeting_outcomes mo
    JOIN contacts c ON c.contact_id = mo.contact_id
    JOIN companies co ON co.company_id = c.company_id
    ORDER BY mo.meeting_occurred_at DESC
    LIMIT 200
"""


@router.get("/outcomes")
def list_outcomes():
    try:
        with get_system_db_context() as session:
            rows = session.execute(text(_OUTCOMES_SQL)).mappings().all()
        return [
            {
                **{k: v for k, v in r.items() if k not in ("meeting_occurred_at", "created_at")},
                "meeting_occurred_at": r["meeting_occurred_at"].isoformat() if r["meeting_occurred_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
