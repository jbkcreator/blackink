"""Internal read endpoint for the pipeline metrics digest.

Reuses the same SQL as src/tasks/daily_digest.py — same 7 KPIs, same 24h
window, platform-wide (no client_id filter, system role). No auth guard yet.

Group D / D-4: this copy of the query had the identical event-name/payload-key
bug as daily_digest.py (owner_score_generated / 'county' — never written by
owner_visibility_sweep.py, which emits owner_visibility_score_calculated /
'county_slug') — see that file's comment for the full explanation.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from src.api.deps import require_admin_jwt
from src.core.database import get_system_db_context
from src.core.demo_clients import DEMO_CLIENT_IDS
from src.services.metrics_query import METRICS_SQL

router = APIRouter(prefix="/api/metrics", tags=["metrics"], dependencies=[Depends(require_admin_jwt)])

_WINDOW = timedelta(hours=24)

_METRICS_SQL = METRICS_SQL


@router.get("/digest")
def get_digest():
    try:
        with get_system_db_context() as session:
            row = session.execute(
                text(_METRICS_SQL),
                {"window": _WINDOW, "demo_client_ids": list(DEMO_CLIENT_IDS)},
            ).mappings().first()
        return dict(row) if row else {}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
