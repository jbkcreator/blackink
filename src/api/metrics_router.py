"""Internal read endpoint for the pipeline metrics digest.

Reuses the same SQL as src/tasks/daily_digest.py — same 7 KPIs, same 24h
window, platform-wide (no client_id filter, system role). No auth guard yet.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from src.api.deps import require_admin_jwt
from src.core.database import get_system_db_context

router = APIRouter(prefix="/api/metrics", tags=["metrics"], dependencies=[Depends(require_admin_jwt)])

_WINDOW = timedelta(hours=24)

_METRICS_SQL = """
    SELECT
        COUNT(*) FILTER (WHERE event_type = 'owner_score_generated') AS scores_generated,
        COUNT(DISTINCT payload->>'county') FILTER (WHERE event_type = 'owner_score_generated') AS county_rank_reports_delivered,
        COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched' AND payload->>'channel' = 'email') AS cold_emails_dispatched,
        (COUNT(*) FILTER (WHERE event_type = 'email_opened')::numeric
            / NULLIF(COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched' AND payload->>'channel' = 'email'), 0) * 100) AS open_rate_pct,
        (COUNT(*) FILTER (WHERE event_type = 'email_clicked')::numeric
            / NULLIF(COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched' AND payload->>'channel' = 'email'), 0) * 100) AS click_rate_pct,
        (COUNT(*) FILTER (WHERE event_type = 'email_replied')::numeric
            / NULLIF(COUNT(*) FILTER (WHERE event_type = 'outbound_touch_dispatched' AND payload->>'channel' = 'email'), 0) * 100) AS reply_rate_pct,
        COUNT(*) FILTER (WHERE event_type = 'meeting_booked') AS appointments_booked
    FROM events
    WHERE created_at >= NOW() - :window
      AND client_id <> 'DEMO_FRIDAY_SANDBOX'
"""


@router.get("/digest")
def get_digest():
    try:
        with get_system_db_context() as session:
            row = session.execute(text(_METRICS_SQL), {"window": _WINDOW}).mappings().first()
        return dict(row) if row else {}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
