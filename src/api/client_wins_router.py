"""Client Wins Dashboard export endpoints (Subtask 3.2.5 Stage 6 / S-21, AC#5).

Internal, admin-JWT-guarded — same guard as src/api/metrics_router.py. An admin
operator can pull any tenant's wins as JSON (for a quick check) or CSV (the AC#5
downloadable export). The client_id is validated against a real clients row so a
typo returns 404 rather than a silently empty export.

This is NOT the tenant-facing dashboard surface — that is the Google Sheet the
sweep writes, rendered by Looker Studio (see src/services/client_wins.py). This
router is the operator/export path only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from src.api.deps import require_admin_jwt
from src.core.database import get_system_db_context
from src.services.client_wins import compute_wins, wins_csv

router = APIRouter(
    prefix="/api/metrics/wins",
    tags=["metrics"],
    dependencies=[Depends(require_admin_jwt)],
)


def _require_client(client_id: str) -> None:
    with get_system_db_context() as session:
        exists = session.execute(
            text("SELECT 1 FROM clients WHERE client_id = :client_id"),
            {"client_id": client_id},
        ).first()
    if not exists:
        raise HTTPException(status_code=404, detail=f"unknown client_id: {client_id}")


@router.get("/{client_id}")
def get_wins(client_id: str):
    """Wins for one client as JSON (list of {metric, value})."""
    _require_client(client_id)
    return {"client_id": client_id, "wins": [{"metric": m, "value": v} for m, v in compute_wins(client_id)]}


@router.get("/{client_id}/export.csv")
def export_wins_csv(client_id: str) -> PlainTextResponse:
    """Wins for one client as a downloadable CSV (AC#5)."""
    _require_client(client_id)
    csv_text = wins_csv(client_id)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="wins_{client_id}.csv"'},
    )
