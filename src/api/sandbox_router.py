"""Internal read endpoint for the Demo Friday Sandbox dashboard.

One CTE reads demo_sandbox_dashboard once and returns both the aggregate
summary and the company rows in a single round-trip. Result is cached
in-process for CACHE_TTL seconds — the view's underlying data only changes
when the seeder runs or an event is logged, so a short TTL is acceptable.
"""
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from src.api.deps import require_admin_jwt
from src.core.database import get_db_context

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"], dependencies=[Depends(require_admin_jwt)])

_SANDBOX_CLIENT = "DEMO_FRIDAY_SANDBOX"
_CACHE_TTL = 300  # 5 minutes

_cache: dict = {"data": None, "expires_at": 0.0}

_SQL = """
WITH data AS (
    SELECT
        company_id,
        company_name,
        county_slug,
        door_count_est,
        current_pm_software,
        status,
        touches_sent,
        meetings_booked
    FROM demo_sandbox_dashboard
)
SELECT
    COUNT(*)                                              AS total_companies,
    COALESCE(SUM(door_count_est), 0)                     AS total_doors,
    COALESCE(SUM(meetings_booked), 0)                    AS total_meetings,
    json_agg(
        json_build_object(
            'company_id',          company_id,
            'company_name',        company_name,
            'county_slug',         county_slug,
            'door_count_est',      door_count_est,
            'current_pm_software', current_pm_software,
            'status',              status,
            'touches_sent',        touches_sent,
            'meetings_booked',     meetings_booked
        )
        ORDER BY company_name
    ) AS companies
FROM data
"""


@router.get("/companies")
def list_sandbox_companies():
    now = time.monotonic()

    if _cache["data"] is not None and now < _cache["expires_at"]:
        return _cache["data"]

    try:
        with get_db_context(client_id=_SANDBOX_CLIENT) as session:
            row = session.execute(text(_SQL)).mappings().first()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    result = {
        "summary": {
            "total_companies": int(row["total_companies"]),
            "total_doors": int(row["total_doors"]),
            "total_meetings": int(row["total_meetings"]),
        },
        "companies": row["companies"] or [],
    }

    _cache["data"] = result
    _cache["expires_at"] = time.monotonic() + _CACHE_TTL
    return result


@router.delete("/companies/cache")
def clear_sandbox_cache():
    _cache["data"] = None
    _cache["expires_at"] = 0.0
    return {"cleared": True}
