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
from src.services.booking_link import resolve_owner_booking_link
from src.services.winback_ingest import export_csv, parse_csv, run_import
from src.services.winback_sequencer import arm_winback_run

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

	# A header-only (or otherwise data-row-free) CSV has bytes but parses to
	# zero rows — reject it before ever creating a winback_imports row, so
	# an operator never sees a misleadingly "COMPLETED" import with
	# total_rows=0 for a file that imported nothing. Re-parsed by run_import
	# below; parsing a several-hundred-row CSV twice is negligible cost for
	# the certainty of never creating a misleading import record.
	if not parse_csv(raw_csv):
		raise HTTPException(status_code=400, detail="CSV has no data rows")

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


@router.post("/imports/{import_id}/arm")
def arm_winback_import(import_id: str, client_id: str):
	"""Subtask 3.1.2 — arms the 3-touch sequence for every armable
	(STILL_OWNS_STILL_RENTING / STILL_OWNS_NOT_RENTING, not suppressed) row
	in this import. One shared `armed_at` for the whole batch, not per-row
	NOW() calls, so the priority-ordering offset in arm_winback_run (0 days
	for STILL_OWNS_STILL_RENTING, +1 day for STILL_OWNS_NOT_RENTING) lands
	exact across the batch — a per-row NOW() would let ordering drift row by
	row over the course of a long-running request."""
	armed_at = datetime.now(timezone.utc)

	with get_db_context(client_id=client_id) as session:
		exists = session.execute(
			text("SELECT 1 FROM winback_imports WHERE import_id = :import_id AND client_id = :client_id"),
			{"import_id": import_id, "client_id": client_id},
		).fetchone()
		if not exists:
			raise HTTPException(status_code=404, detail="Import not found")

		# enrichment_timestamp IS NOT NULL AND requires_enrichment_review = FALSE
		# (Subtask 3.2.1) — the dispatch gate alone (evaluate_winback_touch_gate)
		# does NOT satisfy the DoD's "no sequence record created for those
		# contacts": arm_winback_run INSERTs the agent_work_orders row and posts
		# the Slack approval card BEFORE any touch gate is evaluated, so a
		# gate-only implementation would post approval cards for un-enriched
		# rows and only block them later at dispatch. Also makes eligible_count
		# below honest rather than counting rows that would be skipped anyway.
		rows = session.execute(
			text(
				"SELECT * FROM winback_rows WHERE import_id = :import_id AND client_id = :client_id "
				"AND disposition IN ('STILL_OWNS_STILL_RENTING', 'STILL_OWNS_NOT_RENTING') "
				"AND suppression_state = FALSE AND stopped_at IS NULL "
				"AND enrichment_timestamp IS NOT NULL AND requires_enrichment_review = FALSE"
			),
			{"import_id": import_id, "client_id": client_id},
		).fetchall()

		# Reported in the response only — arm_winback_run resolves its own
		# per-row link (with GHL prefill) below rather than reusing this one,
		# since a single batch-level lookup can't carry each owner's own
		# name/email into the prefill.
		booking_link_provisioned = resolve_owner_booking_link(session, client_id) is not None

		armed_row_ids: list[int] = []
		for row in rows:
			action_ids = arm_winback_run(session, client_id, row, armed_at)
			if action_ids:
				armed_row_ids.append(row.winback_row_id)
		session.commit()

	logger.info(
		"arm_winback_import: import_id=%s client_id=%s armed %d/%d row(s)",
		import_id, client_id, len(armed_row_ids), len(rows),
	)
	return {
		"import_id": import_id,
		"armed_count": len(armed_row_ids),
		"eligible_count": len(rows),
		"booking_link_provisioned": booking_link_provisioned,
	}


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
