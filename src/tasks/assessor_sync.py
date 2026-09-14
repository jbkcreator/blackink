"""County assessor roll sync — daily check-and-import sweep for Pinellas
and Hillsborough (Tasks/blackink_assessor_sync_task.md; see
docs/plans/2026-09-11-automated-county-assessor-data-sync.md for the full
design). One task, one cron entry, both counties — Pinellas's two files are
just two calls into the same Pinellas functions with a different
dataset_name.

Runs under blackink_system (BYPASSRLS) — batch-only, same posture as every
other sweep in src/tasks/. Never imported from src/api/. `assessor_parcels`
carries no client_id at all, so there is no tenant to scope a session to.

Deliberately no `events` rows — see the module-level rationale where
log_event() is discussed elsewhere in this repo: logging an infrastructure
job under a fake client_id would misuse the tenant audit trail.
assessor_sync_state already holds status, row count, hash, timestamps,
last_error, and failure_category; it *is* the audit trail here.

    PYTHONPATH=. python -m src.tasks.assessor_sync                       # all 3 datasets
    PYTHONPATH=. python -m src.tasks.assessor_sync --county pinellas_fl
    PYTHONPATH=. python -m src.tasks.assessor_sync --dataset RP_EXEMPTIONS
    PYTHONPATH=. python -m src.tasks.assessor_sync --dry-run             # metadata check only
    PYTHONPATH=. python -m src.tasks.assessor_sync --force               # ignore published_at comparison
    PYTHONPATH=. python -m src.tasks.assessor_sync --limit 5000          # cap rows, for bounded runs (retirement disabled while limited)
    PYTHONPATH=. python -m src.tasks.assessor_sync --county pinellas_fl --dataset RP_EXEMPTIONS --reset  # reset a FAILED_PERMANENT dataset to PENDING
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.core.database import get_system_db_context
from src.services.assessor.dbase import iter_dbf_records, read_dbf_header_from_stream
from src.services.assessor.importer import (
	ImportResult,
	ImportValidationError,
	import_pinellas_exemptions,
	import_row_owning_dataset,
)
from src.services.assessor.mapping import map_hillsborough_row, map_pinellas_property_info_row
from src.services.assessor.sources import (
	DownloadSizeExceededError,
	SourceStructureError,
	SourceVersion,
	capped_hillsborough_stream,
	check_hillsborough,
	check_pinellas,
	download_pinellas,
	open_hillsborough_download,
)
from src.services.slack.post import post_notice

logger = logging.getLogger(__name__)

# All three datasets this sweep covers — matches the seed rows in
# migrations/apply_assessor_sync.py exactly.
_DATASETS = (
	("hillsborough_fl", "PARCEL_SPREADSHEET"),
	("pinellas_fl", "RP_PROPERTY_INFO"),
	("pinellas_fl", "RP_EXEMPTIONS"),
)

# A dataset's lease expires after this long — a crashed run's claim doesn't
# block the next sweep forever. Generous relative to the real import time
# (minutes, not hours) so a slow-but-alive run is never double-claimed.
_CLAIM_LEASE_HOURS = 2

# Failure categories this repo's own code/schema genuinely can't self-heal
# from — a human fix (a parser update, a validation-rule change) is needed,
# so these are allowed to escalate to FAILED_PERMANENT after repeated
# attempts. NETWORK_TIMEOUT is deliberately excluded: a county site or
# network being briefly unreachable is expected to clear on its own, and
# must never permanently disable a dataset that a later, healthy run would
# import fine — PR review finding: three consecutive daily network blips
# previously hit the same shared attempt counter as every other failure and
# permanently killed the dataset, with _claim() offering no automatic way
# back (`--force` doesn't bypass its `!= 'FAILED_PERMANENT'` check either).
_NON_RECOVERABLE_FAILURE_CATEGORIES = frozenset({
	"SITE_STRUCTURE_CHANGED", "VALIDATION_FAILED", "DOWNLOAD_SIZE_EXCEEDED", "INTERNAL_ERROR",
})

# Caps the 2**attempts backoff growth for a NETWORK_TIMEOUT failure, which
# (per the above) never escalates to FAILED_PERMANENT and so can keep
# incrementing `attempts` indefinitely over a genuinely long outage —
# without this cap the interval would eventually grow to an absurd number
# of minutes rather than settling at a sane daily-ish retry cadence.
_MAX_RETRY_BACKOFF_MINUTES = 24 * 60


@dataclass
class DatasetSyncResult:
	county_slug: str
	dataset_name: str
	status: str  # SUCCESS | SKIPPED_UNCHANGED | SKIPPED_CLAIMED | FAILED | FAILED_PERMANENT


def _claim(session: Session, county_slug: str, dataset_name: str, claim_time: datetime) -> bool:
	"""Atomically decides claimability and stamps the lease in one UPDATE —
	no separate read-then-write race window. Committed immediately (not
	held with the import's own transaction) so the lease is visible even
	if the import itself takes a while."""
	result = session.execute(
		text(
			"""
			UPDATE assessor_sync_state
			SET claimed_at = :claim_time, import_status = 'CHECKING'
			WHERE county_slug = :county_slug AND dataset_name = :dataset_name
			  AND import_status != 'FAILED_PERMANENT'
			  AND (claimed_at IS NULL OR claimed_at < :claim_time - make_interval(hours => :lease_hours))
			  AND (import_status != 'FAILED' OR next_retry_at IS NULL OR next_retry_at <= :claim_time)
			RETURNING id
			"""
		),
		{"claim_time": claim_time, "county_slug": county_slug, "dataset_name": dataset_name, "lease_hours": _CLAIM_LEASE_HOURS},
	)
	claimed = result.fetchone() is not None
	session.commit()
	return claimed


def _get_stored_state(session: Session, county_slug: str, dataset_name: str) -> Optional[dict]:
	row = session.execute(
		text(
			"SELECT source_published_at, row_count, attempts FROM assessor_sync_state "
			"WHERE county_slug = :county_slug AND dataset_name = :dataset_name"
		),
		{"county_slug": county_slug, "dataset_name": dataset_name},
	).fetchone()
	return dict(row._mapping) if row else None


def _mark(
	session: Session,
	county_slug: str,
	dataset_name: str,
	*,
	status: str,
	version: Optional[SourceVersion] = None,
	result: Optional[ImportResult] = None,
	error: Optional[str] = None,
	failure_category: Optional[str] = None,
	as_of: datetime,
) -> Optional[str]:
	"""Returns the actual persisted status when `status == "FAILED"`
	(either "FAILED" or "FAILED_PERMANENT", decided by attempt count) —
	None otherwise. Every FAILED call site MUST use this return value for
	its DatasetSyncResult rather than assuming "FAILED": a bug found in
	review had every caller hardcode the literal string "FAILED"
	regardless of what was actually written here, which made
	run_sweep's "N datasets permanently failed" alert permanently
	unreachable dead code — it checked for a status string this function
	never returned."""
	settings = get_settings()
	final_status: Optional[str] = None
	if status == "FAILED":
		stored = _get_stored_state(session, county_slug, dataset_name) or {"attempts": 0}
		attempts = stored["attempts"] + 1
		# PR review finding: escalating on attempt count alone treated a
		# transient network blip identically to a genuinely broken parser —
		# only a non-recoverable category may ever reach FAILED_PERMANENT.
		if attempts >= settings.assessor_sync_max_attempts and failure_category in _NON_RECOVERABLE_FAILURE_CATEGORIES:
			final_status = "FAILED_PERMANENT"
			next_retry_at = None
		else:
			final_status = "FAILED"
			next_retry_at = as_of + timedelta(minutes=min(2**attempts, _MAX_RETRY_BACKOFF_MINUTES))
		session.execute(
			text(
				"UPDATE assessor_sync_state SET import_status = :status, attempts = :attempts, "
				"next_retry_at = :next_retry_at, last_error = :error, failure_category = :failure_category, "
				"last_checked_at = :as_of, updated_at = :as_of "
				"WHERE county_slug = :county_slug AND dataset_name = :dataset_name"
			),
			{
				"status": final_status, "attempts": attempts, "next_retry_at": next_retry_at,
				"error": error, "failure_category": failure_category, "as_of": as_of,
				"county_slug": county_slug, "dataset_name": dataset_name,
			},
		)
	elif status == "SKIPPED_UNCHANGED":
		session.execute(
			text(
				"UPDATE assessor_sync_state SET import_status = 'SKIPPED_UNCHANGED', "
				"source_published_at_seen = :seen, last_checked_at = :as_of, updated_at = :as_of "
				"WHERE county_slug = :county_slug AND dataset_name = :dataset_name"
			),
			{"seen": version.published_at if version else None, "as_of": as_of,
			 "county_slug": county_slug, "dataset_name": dataset_name},
		)
	elif status == "SUCCESS":
		session.execute(
			text(
				"UPDATE assessor_sync_state SET import_status = 'SUCCESS', "
				"source_published_at = :published_at, source_published_at_seen = :published_at, "
				"source_filename = :filename, row_count = :row_count, attempts = 0, next_retry_at = NULL, "
				"last_error = NULL, failure_category = NULL, "
				"last_checked_at = :as_of, last_downloaded_at = :as_of, last_imported_at = :as_of, updated_at = :as_of "
				"WHERE county_slug = :county_slug AND dataset_name = :dataset_name"
			),
			{
				"published_at": version.published_at if version else None,
				"filename": version.source_filename if version else None,
				"row_count": result.rows_staged if result else None,
				"as_of": as_of, "county_slug": county_slug, "dataset_name": dataset_name,
			},
		)
	session.commit()
	assert (final_status is not None) == (status == "FAILED"), "final_status must be set iff status == 'FAILED'"
	return final_status


def _hillsborough_mapped_rows(response: requests.Response) -> Iterator[dict]:
	# Never response.raw directly — capped_hillsborough_stream enforces
	# ASSESSOR_MAX_DOWNLOAD_BYTES, matching download_pinellas's own cap
	# (a real gap found in review: the Hillsborough path had none).
	stream = capped_hillsborough_stream(response)
	header = read_dbf_header_from_stream(stream)
	for raw_row in iter_dbf_records(stream, header):
		yield map_hillsborough_row(raw_row)


def _pinellas_property_info_mapped_rows(zip_path: Path) -> Iterator[dict]:
	with zipfile.ZipFile(zip_path) as zf:
		member = zf.namelist()[0]
		with zf.open(member) as raw_fh:
			text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8", errors="replace", newline="")
			for raw_row in csv.DictReader(text_fh):
				yield map_pinellas_property_info_row(raw_row)


def _pinellas_exemption_raw_rows(zip_path: Path) -> Iterator[dict]:
	with zipfile.ZipFile(zip_path) as zf:
		member = zf.namelist()[0]
		with zf.open(member) as raw_fh:
			text_fh = io.TextIOWrapper(raw_fh, encoding="utf-8", errors="replace", newline="")
			yield from csv.DictReader(text_fh)


def _limited(rows: Iterator[dict], limit: Optional[int]) -> Iterator[dict]:
	if limit is None:
		yield from rows
		return
	for i, row in enumerate(rows):
		if i >= limit:
			return
		yield row


def sync_one_dataset(
	*,
	county_slug: str,
	dataset_name: str,
	claim_time: Optional[datetime] = None,
	force: bool = False,
	limit: Optional[int] = None,
	dry_run: bool = False,
) -> DatasetSyncResult:
	claim_time = claim_time or datetime.now(timezone.utc)
	settings = get_settings()

	with get_system_db_context() as session:
		# dry-run does no writes to assessor_parcels and shouldn't compete
		# for the same lease a real import needs — found by hand: running
		# --dry-run then a real import moments later got wrongly
		# SKIPPED_CLAIMED for the full 2-hour lease window.
		if not dry_run and not _claim(session, county_slug, dataset_name, claim_time):
			logger.info("assessor_sync: %s/%s is already claimed — skipping", county_slug, dataset_name)
			return DatasetSyncResult(county_slug, dataset_name, "SKIPPED_CLAIMED")

		try:
			if county_slug == "hillsborough_fl":
				version = check_hillsborough()
			else:
				version = check_pinellas(dataset_name)
		except (SourceStructureError, requests.RequestException) as exc:
			category = "SITE_STRUCTURE_CHANGED" if isinstance(exc, SourceStructureError) else "NETWORK_TIMEOUT"
			logger.exception("assessor_sync: %s/%s metadata check failed", county_slug, dataset_name)
			final_status = _mark(session, county_slug, dataset_name, status="FAILED", error=str(exc), failure_category=category, as_of=claim_time)
			return DatasetSyncResult(county_slug, dataset_name, final_status)

		if dry_run:
			_mark(session, county_slug, dataset_name, status="SKIPPED_UNCHANGED", version=version, as_of=claim_time)
			logger.info("assessor_sync: %s/%s dry-run — published_at=%s", county_slug, dataset_name, version.published_at)
			return DatasetSyncResult(county_slug, dataset_name, "SKIPPED_UNCHANGED")

		stored = _get_stored_state(session, county_slug, dataset_name)
		if not force and stored and stored["source_published_at"] and version.published_at <= stored["source_published_at"]:
			_mark(session, county_slug, dataset_name, status="SKIPPED_UNCHANGED", version=version, as_of=claim_time)
			return DatasetSyncResult(county_slug, dataset_name, "SKIPPED_UNCHANGED")

		# PR review finding: a --limit'd run's own row-count floor does not
		# protect the retire step, which has no way to tell "genuinely gone
		# from the roll" from "outside this run's truncated prefix" — a
		# limit above 50% of the prior roll still passes both floor checks
		# while retiring every real parcel outside the limited batch.
		# Disabling retirement whenever a limit is in effect keeps --limit
		# useful for its documented purpose (bounded verification against a
		# real feed) without letting it corrupt the live roll.
		skip_retire = limit is not None
		if skip_retire:
			logger.warning(
				"assessor_sync: %s/%s running with --limit=%d — parcel retirement is disabled for this run",
				county_slug, dataset_name, limit,
			)

		try:
			if county_slug == "hillsborough_fl":
				response = open_hillsborough_download(version)
				try:
					rows = _limited(_hillsborough_mapped_rows(response), limit)
					result = import_row_owning_dataset(
						session, county_slug=county_slug, dataset_name=dataset_name,
						mapped_rows=rows, as_of=claim_time, owns_homestead=True, skip_retire=skip_retire,
					)
				finally:
					response.close()
			elif dataset_name == "RP_PROPERTY_INFO":
				download_dir = Path(settings.assessor_download_dir)
				zip_path = download_pinellas(dataset_name, download_dir)
				try:
					rows = _limited(_pinellas_property_info_mapped_rows(zip_path), limit)
					result = import_row_owning_dataset(
						session, county_slug=county_slug, dataset_name=dataset_name,
						mapped_rows=rows, as_of=claim_time, owns_homestead=False, skip_retire=skip_retire,
					)
				finally:
					zip_path.unlink(missing_ok=True)
			else:  # RP_EXEMPTIONS
				download_dir = Path(settings.assessor_download_dir)
				zip_path = download_pinellas(dataset_name, download_dir)
				try:
					raw_rows = _limited(_pinellas_exemption_raw_rows(zip_path), limit)
					result = import_pinellas_exemptions(session, raw_rows=raw_rows, as_of=claim_time)
				finally:
					zip_path.unlink(missing_ok=True)
			session.commit()
		except ImportValidationError as exc:
			session.rollback()
			logger.error("assessor_sync: %s/%s validation failed: %s", county_slug, dataset_name, exc)
			final_status = _mark(session, county_slug, dataset_name, status="FAILED", error=str(exc), failure_category="VALIDATION_FAILED", as_of=claim_time)
			return DatasetSyncResult(county_slug, dataset_name, final_status)
		except DownloadSizeExceededError as exc:
			# Caught before SourceStructureError — found in review that
			# reusing SourceStructureError for both made the schema's
			# dedicated DOWNLOAD_SIZE_EXCEEDED category unreachable.
			session.rollback()
			logger.error("assessor_sync: %s/%s download size exceeded: %s", county_slug, dataset_name, exc)
			final_status = _mark(session, county_slug, dataset_name, status="FAILED", error=str(exc), failure_category="DOWNLOAD_SIZE_EXCEEDED", as_of=claim_time)
			return DatasetSyncResult(county_slug, dataset_name, final_status)
		except SourceStructureError as exc:
			session.rollback()
			logger.exception("assessor_sync: %s/%s site structure changed mid-download", county_slug, dataset_name)
			final_status = _mark(session, county_slug, dataset_name, status="FAILED", error=str(exc), failure_category="SITE_STRUCTURE_CHANGED", as_of=claim_time)
			return DatasetSyncResult(county_slug, dataset_name, final_status)
		except requests.RequestException as exc:
			session.rollback()
			logger.exception("assessor_sync: %s/%s download failed", county_slug, dataset_name)
			final_status = _mark(session, county_slug, dataset_name, status="FAILED", error=str(exc), failure_category="NETWORK_TIMEOUT", as_of=claim_time)
			return DatasetSyncResult(county_slug, dataset_name, final_status)
		except Exception as exc:  # noqa: BLE001
			session.rollback()
			logger.exception("assessor_sync: %s/%s failed unexpectedly", county_slug, dataset_name)
			final_status = _mark(session, county_slug, dataset_name, status="FAILED", error=str(exc), failure_category="INTERNAL_ERROR", as_of=claim_time)
			return DatasetSyncResult(county_slug, dataset_name, final_status)

		_mark(session, county_slug, dataset_name, status="SUCCESS", version=version, result=result, as_of=claim_time)
		logger.info(
			"assessor_sync: %s/%s SUCCESS — staged=%d upserted=%d retired=%d",
			county_slug, dataset_name, result.rows_staged, result.rows_upserted, result.rows_retired,
		)
		return DatasetSyncResult(county_slug, dataset_name, "SUCCESS")


def run_sweep(
	*,
	county: Optional[str] = None,
	dataset: Optional[str] = None,
	claim_time: Optional[datetime] = None,
	force: bool = False,
	limit: Optional[int] = None,
	dry_run: bool = False,
) -> list[DatasetSyncResult]:
	settings = get_settings()
	if not settings.assessor_sync_enabled and not dry_run:
		logger.warning("assessor_sync: ASSESSOR_SYNC_ENABLED is False — skipping (dry-run is always allowed)")
		return []

	targets = [
		(c, d) for c, d in _DATASETS
		if (county is None or c == county) and (dataset is None or d == dataset)
	]
	if not targets and (county is not None or dataset is not None):
		# A nonsensical --county/--dataset combination (e.g. RP_EXEMPTIONS
		# is Pinellas-only) would otherwise silently match nothing and
		# exit 0 — indistinguishable from a real, successful no-op run —
		# a real usability gap found in review. Fail loud instead.
		raise ValueError(
			f"no dataset matches county={county!r} dataset={dataset!r} — check the combination "
			f"against the valid pairs: {_DATASETS}"
		)
	results = []
	for county_slug, dataset_name in targets:
		results.append(
			sync_one_dataset(
				county_slug=county_slug, dataset_name=dataset_name,
				claim_time=claim_time, force=force, limit=limit, dry_run=dry_run,
			)
		)
	failed_permanent = [r for r in results if r.status == "FAILED_PERMANENT"]
	if failed_permanent:
		names = [f"{r.county_slug}/{r.dataset_name}" for r in failed_permanent]
		logger.error("assessor_sync: %d dataset(s) permanently failed: %s", len(failed_permanent), names)
		# Found in review: this alert was documented ("A failure Slack alert
		# to #blackink-qa on FAILED_PERMANENT covers operator visibility")
		# but never actually implemented — only the log line above existed.
		# A FAILED_PERMANENT dataset (most likely a site redesign) needs a
		# human to actually notice; a log line in a cron-redirected file
		# nobody actively watches is not that. post_notice is `async def`
		# (src/services/slack/post.py) and "never raises" — asyncio.run is
		# correct here since this is a standalone CLI/cron process with no
		# running event loop, matching daily_digest.py's identical pattern.
		asyncio.run(
			post_notice(
				channel_key="qa",
				text=f"assessor_sync: {len(failed_permanent)} dataset(s) permanently failed: {', '.join(names)}",
			)
		)
	return results


def reset_dataset(county_slug: str, dataset_name: str, as_of: Optional[datetime] = None) -> bool:
	"""Controlled, explicit reset path for a FAILED_PERMANENT dataset (PR
	review finding: escalation had no automatic way back, and no manual way
	back either short of an operator hand-editing assessor_sync_state).
	Meant for the case where the underlying issue really has been fixed
	(e.g. a parser update shipped for a SITE_STRUCTURE_CHANGED failure) —
	an operator confirms the fix, runs this, then the next sweep tick
	claims and retries normally. Returns True if a row was actually reset
	(False if the county_slug/dataset_name pair doesn't exist)."""
	as_of = as_of or datetime.now(timezone.utc)
	with get_system_db_context() as session:
		result = session.execute(
			text(
				"UPDATE assessor_sync_state SET import_status = 'PENDING', attempts = 0, "
				"next_retry_at = NULL, claimed_at = NULL, last_error = NULL, failure_category = NULL, "
				"updated_at = :as_of "
				"WHERE county_slug = :county_slug AND dataset_name = :dataset_name"
			),
			{"as_of": as_of, "county_slug": county_slug, "dataset_name": dataset_name},
		)
		session.commit()
		return result.rowcount > 0


def _main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--county", choices=["hillsborough_fl", "pinellas_fl"], default=None)
	parser.add_argument("--dataset", choices=["PARCEL_SPREADSHEET", "RP_PROPERTY_INFO", "RP_EXEMPTIONS"], default=None)
	parser.add_argument("--dry-run", action="store_true")
	parser.add_argument("--force", action="store_true")
	parser.add_argument("--limit", type=int, default=None)
	parser.add_argument(
		"--reset", action="store_true",
		help="Reset a FAILED_PERMANENT dataset back to PENDING (retryable) instead of running a sync. "
		     "Requires --county AND --dataset naming exactly one dataset — never resets all three at once.",
	)
	args = parser.parse_args()

	logging.basicConfig(level=logging.INFO)

	if args.reset:
		if not args.county or not args.dataset:
			parser.error("--reset requires both --county and --dataset (naming exactly the dataset to reset)")
		did_reset = reset_dataset(args.county, args.dataset)
		if not did_reset:
			print(f"{args.county}/{args.dataset}: no such dataset — nothing reset")
			return 1
		print(f"{args.county}/{args.dataset}: reset to PENDING")
		return 0

	results = run_sweep(county=args.county, dataset=args.dataset, force=args.force, limit=args.limit, dry_run=args.dry_run)
	for r in results:
		print(f"{r.county_slug}/{r.dataset_name}: {r.status}")
	return 1 if any(r.status in ("FAILED", "FAILED_PERMANENT") for r in results) else 0


if __name__ == "__main__":
	raise SystemExit(_main())
