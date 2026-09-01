"""Optional read-only status surface for Akrash — the primary ingestion
path is direct restricted database write access (Dev 1 plan §Key decision
6), so this router does NOT accept writes. It exists only so Akrash can
poll "what happened to the rows I wrote" (reason codes) without needing
internal-ops to relay them manually. Whether Akrash's tooling can even call
this is an open question flagged in the Dev 1 plan — ship it either way,
since it costs little and unblocks the "nothing dropped silently" guarantee
being genuinely visible to the submitter, not just internally auditable.
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.api.deps import get_current_akrash, get_db

router = APIRouter(prefix="/api/akrash", tags=["akrash"])


class ProspectStatus(BaseModel):
	id: int
	company_name: str
	domain: str
	validation_status: str
	reject_reason_code: Optional[str]
	promoted_company_id: Optional[str]
	created_at: datetime


@router.get("/prospects/status", response_model=List[ProspectStatus])
def get_prospect_status(
	submitted_by: str = Query(...),
	since: Optional[datetime] = Query(default=None),
	db: Session = Depends(get_db),
	_akrash_subject: str = Depends(get_current_akrash),
) -> List[ProspectStatus]:
	"""Scoped by submitted_by — Akrash can only see rows it submitted."""
	sql = (
		"SELECT id, company_name, domain, validation_status, reject_reason_code, "
		"promoted_company_id, created_at FROM raw_prospect_companies "
		"WHERE submitted_by = :submitted_by"
	)
	params = {"submitted_by": submitted_by}
	if since is not None:
		sql += " AND created_at >= :since"
		params["since"] = since
	sql += " ORDER BY created_at DESC LIMIT 500"

	rows = db.execute(text(sql), params).fetchall()
	return [
		ProspectStatus(
			id=r.id,
			company_name=r.company_name,
			domain=r.domain,
			validation_status=r.validation_status,
			reject_reason_code=r.reject_reason_code,
			promoted_company_id=r.promoted_company_id,
			created_at=r.created_at,
		)
		for r in rows
	]
