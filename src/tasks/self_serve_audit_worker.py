"""Scheduled Owner Visibility Score worker for self-serve landing-page
submissions (Subtask 3.2.3). The public POST handler
(src/api/public_landing_router.py) only ever inserts a PENDING row into
self_serve_audit_submissions — this is the only code path that ever
creates/finds a companies row or calls score_one_company() (which can
call the real Google Places API) for a self-serve submission.

Runs under the BYPASSRLS system session — same posture as
promotion_sweep.py/owner_visibility_sweep.py, since it writes an
unclaimed, un-tenant-owned company row.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.loaders.base import BaseIngestLoader
from src.tasks.owner_visibility_sweep import _current_month_key, score_one_company

logger = logging.getLogger(__name__)

_CLAIM_LEASE_MINUTES = 10
_MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT = 3


def claim_submissions(session, *, claim_time: datetime, limit: int = 20):
	"""Claims PENDING rows, claim-expired SENDING rows (crashed mid-flight),
	and FAILED rows whose next_retry_at has arrived — a bounded retry, not
	a dead end. FAILED_PERMANENT/BLOCKED_MISSING_COUNTY/SCORED/
	SKIPPED_RECENT/REJECTED_DOMAIN are never reclaimed."""
	rows = session.execute(
		text(
			f"""
			UPDATE self_serve_audit_submissions SET status = 'SENDING', attempts = attempts + 1, claimed_at = :claim_time
			WHERE submission_id IN (
				SELECT submission_id FROM self_serve_audit_submissions
				WHERE (status = 'PENDING')
				   OR (status = 'FAILED' AND (next_retry_at IS NULL OR next_retry_at <= :claim_time))
				   OR (status = 'SENDING' AND claimed_at < :claim_time - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes')
				ORDER BY submission_id FOR UPDATE SKIP LOCKED LIMIT :limit
			)
			RETURNING submission_id
			"""
		),
		{"claim_time": claim_time, "limit": limit},
	).fetchall()
	return [r.submission_id for r in rows]


def _mark(session, submission_id: int, status: str, *, error: str = None, company_id: str = None, next_retry_at=None) -> None:
	session.execute(
		text(
			"UPDATE self_serve_audit_submissions SET status = :status, last_error = :error, "
			"company_id = COALESCE(:company_id, company_id), next_retry_at = :next_retry_at, "
			"updated_at = NOW() WHERE submission_id = :id"
		),
		{"status": status, "error": error, "company_id": company_id, "next_retry_at": next_retry_at, "id": submission_id},
	)


def _mark_failed(session, submission_id: int, attempts: int, error: str, *, company_id: str = None) -> None:
	"""Bounded retry: FAILED with an exponential-backoff next_retry_at
	until attempts exceeds the bound, then FAILED_PERMANENT (a real
	terminal state — never reclaimed, never retried again)."""
	if attempts >= _MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT:
		_mark(session, submission_id, "FAILED_PERMANENT", error=error, company_id=company_id)
		return
	backoff_minutes = 2 ** attempts
	_mark(
		session, submission_id, "FAILED", error=error, company_id=company_id,
		next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes),
	)


def process_submission(session, submission_id: int) -> None:
	row = session.execute(
		text(
			"SELECT submission_id, domain_normalized, visitor_company, county_slug, attempts "
			"FROM self_serve_audit_submissions WHERE submission_id = :id"
		),
		{"id": submission_id},
	).one()

	# companies.county_slug is NOT NULL (a FK to counties) -- the INSERT
	# below would fail its own constraint for a submission with no county,
	# and there is no domain-to-county geocoding step anywhere in this
	# repo to infer one. Checked BEFORE attempting the INSERT (not after,
	# and not by inspecting the created row) so a missing county never
	# reaches the database as a crash -- the public router's AuditSubmission
	# model requires county_slug and validates it against a real counties
	# row, so this should only trip on a request that bypassed that
	# validation somehow (defense in depth, not the expected path).
	if not row.county_slug:
		_mark(session, submission_id, "BLOCKED_MISSING_COUNTY", error="submission has no county_slug")
		return

	try:
		company_id = BaseIngestLoader.compute_company_id(row.domain_normalized)
	except ValueError as exc:
		# A malformed domain is a permanent, non-retriable failure — not a
		# transient condition a backoff would ever resolve.
		_mark(session, submission_id, "FAILED_PERMANENT", error=f"invalid domain: {exc}")
		return

	# visitor_company (the name the visitor actually typed) is the
	# company_name on a newly-created row — never the raw domain, which
	# would poison DbprLicenceSignalProvider's fuzzy name match against
	# meaningless input. AuditSubmission requires visitor_company, so this
	# is always a real value for a row that made it this far. If the
	# company already exists (a real prospected row), its own county_slug
	# is kept as-is -- ON CONFLICT DO NOTHING never overwrites it with the
	# visitor's self-reported one.
	session.execute(
		text(
			"INSERT INTO companies (company_id, company_name, domain, county_slug, status) "
			"VALUES (:company_id, :company_name, :domain, :county_slug, 'PROSPECTING') "
			"ON CONFLICT (company_id) DO NOTHING"
		),
		{
			"company_id": company_id, "company_name": row.visitor_company, "domain": row.domain_normalized,
			"county_slug": row.county_slug,
		},
	)

	month_key = _current_month_key()
	existing = session.execute(
		text("SELECT 1 FROM owner_visibility_scores WHERE company_id = :cid AND month_key = :mk"),
		{"cid": company_id, "mk": month_key},
	).first()
	if existing is not None:
		# Bounds Google Places spend on repeat submissions of the same
		# domain — the event row is still logged by the public handler on
		# every submission, only rescoring is skipped.
		_mark(session, submission_id, "SKIPPED_RECENT", company_id=company_id)
		return

	company_row = session.execute(
		text(
			"SELECT company_id, company_name, domain, website, county_slug, google_place_id "
			"FROM companies WHERE company_id = :cid"
		),
		{"cid": company_id},
	).one()

	if company_row.county_slug is None:
		# Defensive: an already-existing company row (found, not created
		# by us) could in principle have a NULL county_slug if some other
		# path ever allowed it -- companies.county_slug is NOT NULL today,
		# so this is currently unreachable, but the check stays rather
		# than assuming that invariant never changes.
		_mark(session, submission_id, "BLOCKED_MISSING_COUNTY", error="company has no county_slug", company_id=company_id)
		return

	company = {
		"company_id": company_row.company_id, "company_name": company_row.company_name,
		"domain": company_row.domain, "website": company_row.website,
		"county_slug": company_row.county_slug, "google_place_id": company_row.google_place_id,
	}

	try:
		score_one_company(session, company, month_key)
	except Exception as exc:  # noqa: BLE001 - a scoring failure is a definite, bounded-retry failure
		# A DB-level failure inside score_one_company (a constraint
		# violation, a bad statement) leaves the session's transaction
		# aborted at the Postgres level even though Python only sees this
		# exception -- rolling back here is what makes the _mark_failed()
		# UPDATE below actually able to run, instead of itself failing
		# with "current transaction is aborted". run_sweep() shares one
		# session across every claimed row in a batch, so this must not
		# be skipped just because this row is being abandoned.
		session.rollback()
		_mark_failed(session, submission_id, row.attempts, str(exc), company_id=company_id)
		return

	_mark(session, submission_id, "SCORED", company_id=company_id)


def run_sweep(limit: int = 20) -> int:
	processed = 0
	with get_system_db_context() as session:
		claimed = claim_submissions(session, claim_time=datetime.now(timezone.utc), limit=limit)
		for submission_id in claimed:
			process_submission(session, submission_id)
			processed += 1
	logger.info("self_serve_audit_worker: processed %d submission(s)", processed)
	return processed


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_sweep()
