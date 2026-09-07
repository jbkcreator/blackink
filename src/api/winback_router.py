"""Admin-authenticated Win-Back CSV upload/export endpoints (Subtask 3.1.1).

Internal admin surface, not a PM-firm-facing portal — no such portal exists
in this codebase yet (see docs/plans/2026-09-07-subtask-3.1.1-winback-csv-
ingest-assessor-frbo.md's §Admin endpoints). Ops uploads a client's
lost-owner CSV on their behalf this sprint; when Week 3's 15-State Portal
exists, its State 8 screen calls src/services/winback_ingest.py's functions
directly through a new, separately-authenticated route — not this one.

The router itself does no business logic — it creates the winback_imports
row under the tenant-scoped app role (so RLS attributes it to the right
client from the start), then hands off to winback_ingest.run_import(),
which does the real work under the system role. Mirrors the thin-router/
fat-service split used throughout this codebase (e.g. booking_webhook_router.py
-> booking_ingest.py).
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import text

from src.api.deps import require_admin_jwt
from src.core.database import get_db_context
from src.services.winback_ingest import export_csv, run_import

logger = logging.getLogger(__name__)

router = APIRouter(
	prefix="/api/v1/admin/winback",
	tags=["winback"],
	dependencies=[Depends(require_admin_jwt)],
)


@router.post("/imports")
async def upload_winback_csv(client_id: str, file: UploadFile, admin=Depends(require_admin_jwt)):
	raw_bytes = await file.read()
	if not raw_bytes:
		raise HTTPException(status_code=400, detail="Empty file")
	try:
		raw_csv = raw_bytes.decode("utf-8-sig")
	except UnicodeDecodeError:
		raise HTTPException(status_code=400, detail="File is not valid UTF-8 text")

	import_id = str(uuid.uuid4())
	uploaded_by = admin.get("sub") or admin.get("username") or "unknown-admin"

	with get_db_context(client_id=client_id) as session:
		client_exists = session.execute(
			text("SELECT 1 FROM clients WHERE client_id = :client_id"), {"client_id": client_id}
		).fetchone()
		if not client_exists:
			raise HTTPException(status_code=404, detail=f"Unknown client_id: {client_id!r}")
		session.execute(
			text(
				"INSERT INTO winback_imports (import_id, client_id, filename, uploaded_by, status, created_at) "
				"VALUES (:import_id, :client_id, :filename, :uploaded_by, 'PROCESSING', :now)"
			),
			{
				"import_id": import_id,
				"client_id": client_id,
				"filename": file.filename or "upload.csv",
				"uploaded_by": uploaded_by,
				"now": datetime.now(timezone.utc),
			},
		)
		session.commit()

	try:
		counts = run_import(import_id, client_id, raw_csv)
	except Exception:
		logger.error("winback upload: import_id=%s failed", import_id, exc_info=True)
		with get_db_context(client_id=client_id) as session:
			session.execute(
				text("UPDATE winback_imports SET status = 'FAILED' WHERE import_id = :import_id"),
				{"import_id": import_id},
			)
			session.commit()
		raise HTTPException(status_code=502, detail="Win-back import failed — see server logs")

	return {"import_id": import_id, "status": "COMPLETED", **counts}


@router.get("/imports/{import_id}/export.csv")
def download_winback_export(import_id: str, client_id: str):
	with get_db_context(client_id=client_id) as session:
		exists = session.execute(
			text("SELECT 1 FROM winback_imports WHERE import_id = :import_id AND client_id = :client_id"),
			{"import_id": import_id, "client_id": client_id},
		).fetchone()
		if not exists:
			raise HTTPException(status_code=404, detail="Import not found")
		csv_text = export_csv(session, import_id)

	return Response(
		content=csv_text,
		media_type="text/csv",
		headers={"Content-Disposition": f'attachment; filename="winback_{import_id}.csv"'},
	)
