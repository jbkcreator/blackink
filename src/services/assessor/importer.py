"""Staging -> validate -> upsert -> retire for the county assessor sync's
write path into assessor_parcels. See
docs/plans/2026-09-11-automated-county-assessor-data-sync.md §2.5 for the
full design rationale.

COPY-based bulk staging is a new pattern for this repo (nothing else here
does executemany/COPY/execute_values — every other sweep writes row-at-a-time
inside session.begin_nested()). That is the right choice at 20-100 rows and
wrong at 500,000+ — row-at-a-time would take hours per night.

Column ownership — the fix for a real write-conflict bug found in review.
Two independently-scheduled Pinellas files (RP_PROPERTY_INFO, RP_EXEMPTIONS)
upsert the same assessor_parcels rows. An earlier draft let either import
touch any column, which would have: (1) crashed on NOT NULL constraints if
RP_EXEMPTIONS's update tried to INSERT a row it has no address/owner data
for, and (2) silently mis-retired parcels if RP_EXEMPTIONS's update touched
source_dataset/last_seen_at, making the *next* RP_PROPERTY_INFO retire pass
stop seeing that row. Fixed by a strict partition:

  Row-owning import (Hillsborough's single file; Pinellas's
  RP_PROPERTY_INFO) owns: identity/owner/mailing/use/status/source-tracking
  columns, PLUS (Hillsborough only, since it has no separate exemptions
  file) homestead_status/homestead_raw_value/exemption_excluded. ONLY this
  import ever runs the retire step.

  RP_EXEMPTIONS (Pinellas only) owns: homestead_status,
  homestead_raw_value, homestead_roll_year, homestead_next_year,
  homestead_use_pct, homestead_previous, property_exemption_raw,
  exemption_excluded. A pure UPDATE — never an upsert, so it structurally
  cannot INSERT a row and hit the NOT NULL constraints. Never touches
  source_dataset, last_seen_at, or retired_at. A STRAP with no matching
  assessor_parcels row is silently left unmatched, never an error.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Iterator, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.assessor.mapping import derive_pinellas_exemption_update

logger = logging.getLogger(__name__)

# Absolute floor — below this, an import is almost certainly a truncated or
# placeholder file, not a real county roll. Deliberately low relative to the
# real files' true size (531k Hillsborough parcels, tens of thousands for
# Pinellas) so a --limit'd test/e2e run doesn't trip it; the *relative* floor
# below is what actually protects the real nightly run.
_ABSOLUTE_ROW_FLOOR = 10

# A new import must be at least this fraction of the last successful import's
# row count, or it's rejected as a likely truncated/corrupt download — cheap,
# high-value protection against a bad file silently replacing a good roll.
_RELATIVE_ROW_FLOOR_RATIO = 0.5

_ROW_OWNING_COLUMNS = (
	"assessor_parcel_id", "strap", "parcel_address_raw", "parcel_address_normalized",
	"parcel_city", "parcel_zip", "owner_name_on_roll", "owner_name_secondary",
	"owner_mailing_address_1", "owner_mailing_address_2", "owner_mailing_city",
	"owner_mailing_state", "owner_mailing_state_raw", "owner_mailing_zip", "owner_mailing_country",
	"property_use_code", "property_use_raw", "property_class", "parcel_status_raw",
	"unit_count", "roll_year", "source_record_updated_at", "raw_payload",
)

# Hillsborough-only extension — see module docstring's Column Ownership note.
_HILLSBOROUGH_EXTRA_COLUMNS = ("homestead_status", "homestead_raw_value", "exemption_excluded")

_EXEMPTIONS_UPDATE_COLUMNS = (
	"homestead_status", "homestead_raw_value", "homestead_roll_year",
	"homestead_next_year", "homestead_use_pct", "property_exemption_raw",
	"exemption_excluded", "homestead_previous",
)


class ImportValidationError(ValueError):
	"""A staged dataset failed validation (row-count floor). Raised before
	anything touches assessor_parcels — the caller's transaction rolls
	back and the previous good roll is left fully intact."""


@dataclass
class ImportResult:
	rows_staged: int
	rows_upserted: int
	rows_retired: int = 0


def _copy_escape(value: object) -> str:
	"""Serialize one Python value for Postgres COPY text format. None
	becomes the literal \\N null marker; everything else is stringified and
	has COPY's three special characters backslash-escaped."""
	if value is None:
		return "\\N"
	text_value = str(value)
	return text_value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _copy_rows(session: Session, temp_table: str, columns: Iterable[str], rows: Iterator[dict]) -> int:
	"""Bulk-load `rows` into `temp_table` via COPY FROM STDIN (text format).
	Streams through an in-memory buffer in chunks rather than materializing
	the whole dataset as one string — Hillsborough alone is 500k+ rows."""
	columns = tuple(columns)
	cursor = session.connection().connection.cursor()
	count = 0
	chunk = io.StringIO()
	chunk_rows = 0
	sql = f"COPY {temp_table} ({', '.join(columns)}) FROM STDIN"
	for row in rows:
		chunk.write("\t".join(_copy_escape(row.get(col)) for col in columns))
		chunk.write("\n")
		count += 1
		chunk_rows += 1
		if chunk_rows >= 50_000:
			chunk.seek(0)
			cursor.copy_expert(sql, chunk)
			chunk = io.StringIO()
			chunk_rows = 0
	if chunk_rows:
		chunk.seek(0)
		cursor.copy_expert(sql, chunk)
	return count


def _last_successful_row_count(session: Session, county_slug: str, dataset_name: str) -> Optional[int]:
	row = session.execute(
		text(
			"SELECT row_count FROM assessor_sync_state "
			"WHERE county_slug = :county_slug AND dataset_name = :dataset_name"
		),
		{"county_slug": county_slug, "dataset_name": dataset_name},
	).fetchone()
	return row[0] if row else None


def _validate_row_count(
	session: Session,
	county_slug: str,
	dataset_name: str,
	staged_count: int,
	min_row_count: int = _ABSOLUTE_ROW_FLOOR,
) -> None:
	if staged_count < min_row_count:
		raise ImportValidationError(
			f"{county_slug}/{dataset_name}: staged {staged_count} rows, below the absolute "
			f"floor of {min_row_count} — refusing to treat this as a real county roll"
		)
	last_count = _last_successful_row_count(session, county_slug, dataset_name)
	if last_count is not None and staged_count < last_count * _RELATIVE_ROW_FLOOR_RATIO:
		raise ImportValidationError(
			f"{county_slug}/{dataset_name}: staged {staged_count} rows, less than "
			f"{_RELATIVE_ROW_FLOOR_RATIO:.0%} of the last successful import's {last_count} — "
			f"likely a truncated or placeholder download, refusing to replace good data"
		)


def import_row_owning_dataset(
	session: Session,
	*,
	county_slug: str,
	dataset_name: str,
	mapped_rows: Iterator[dict],
	as_of: datetime,
	owns_homestead: bool,
	min_row_count: int = _ABSOLUTE_ROW_FLOOR,
) -> ImportResult:
	"""Imports Hillsborough's single file, or Pinellas's RP_PROPERTY_INFO.
	`owns_homestead=True` only for Hillsborough (see module docstring).
	`min_row_count` overrides the absolute floor — tests and bounded
	(--limit'd) runs pass a smaller value; production callers should leave
	it at the default. Caller owns the transaction: on any exception,
	nothing here has committed and the caller's rollback leaves the
	previous roll intact."""
	columns = _ROW_OWNING_COLUMNS + (_HILLSBOROUGH_EXTRA_COLUMNS if owns_homestead else ())
	temp_table = "tmp_assessor_row_owning_staging"
	session.execute(text(f"DROP TABLE IF EXISTS {temp_table}"))
	session.execute(
		text(
			f"CREATE TEMP TABLE {temp_table} "
			f"({', '.join(f'{c} TEXT' for c in columns)}) ON COMMIT DROP"
		)
	)
	staged_count = _copy_rows(session, temp_table, columns, mapped_rows)
	_validate_row_count(session, county_slug, dataset_name, staged_count, min_row_count)

	# ON CONFLICT DO UPDATE can only reference EXCLUDED.<col> (the row that
	# would have been inserted), never the source table alias `t` — a real
	# Postgres syntax rule, confirmed by hitting it against the live DB.
	# homestead_status/homestead_raw_value get their own transition-aware
	# clause below (Hillsborough only) instead of a plain overwrite here.
	_plain_overwrite_exclusions = {"assessor_parcel_id"}
	if owns_homestead:
		_plain_overwrite_exclusions |= {"homestead_status", "homestead_raw_value"}
	set_clause = ",\n            ".join(
		f"{c} = EXCLUDED.{c}" for c in columns if c not in _plain_overwrite_exclusions
	)
	set_clause += ",\n            last_seen_at = :as_of, last_imported_at = :as_of, source_dataset = :dataset_name"
	# A parcel present in this run's staged batch is, by definition, back
	# on the roll — unconditionally clear any prior retirement. Without
	# this, a parcel that legitimately reappears (a county data
	# correction, a transient prior-run drop) would stay permanently
	# invisible to Win-Back even after it's really back on the roll —
	# found by the live test suite, not by inspection.
	set_clause += ",\n            retired_at = NULL"
	if owns_homestead:
		# Hillsborough-only transition detection (see mapping.py's
		# derive_hillsborough_homestead and the plan doc's §2.4/§2.5 —
		# Pinellas uses a file-derived homestead_previous instead, applied
		# in import_pinellas_exemptions). `assessor_parcels.<col>` here
		# refers to the PRE-update row; EXCLUDED.<col> is the new value —
		# standard ON CONFLICT DO UPDATE semantics. Guarded by
		# `<> 'UNKNOWN'` so a parcel's first-ever evaluation can never
		# emit a spurious "drop" signal.
		set_clause += """,
            homestead_status = EXCLUDED.homestead_status,
            homestead_raw_value = EXCLUDED.homestead_raw_value,
            homestead_previous = CASE
                WHEN assessor_parcels.homestead_status <> EXCLUDED.homestead_status
                 AND assessor_parcels.homestead_status <> 'UNKNOWN'
                THEN assessor_parcels.homestead_status
                ELSE assessor_parcels.homestead_previous END,
            homestead_changed_at = CASE
                WHEN assessor_parcels.homestead_status <> EXCLUDED.homestead_status
                 AND assessor_parcels.homestead_status <> 'UNKNOWN'
                THEN :as_of ELSE assessor_parcels.homestead_changed_at END"""
	insert_columns = ", ".join(columns)
	insert_values = ", ".join(f"t.{c}::{_pg_type(c)}" for c in columns)
	result = session.execute(
		text(
			f"""
			INSERT INTO assessor_parcels
			    (county_slug, source_dataset, last_seen_at, last_imported_at, {insert_columns})
			SELECT :county_slug, :dataset_name, :as_of, :as_of, {insert_values}
			FROM {temp_table} t
			ON CONFLICT (county_slug, assessor_parcel_id) DO UPDATE SET
			    {set_clause}
			"""
		),
		{"county_slug": county_slug, "dataset_name": dataset_name, "as_of": as_of},
	)
	rows_upserted = result.rowcount

	retire_result = session.execute(
		text(
			"UPDATE assessor_parcels SET retired_at = :as_of "
			"WHERE county_slug = :county_slug AND source_dataset = :dataset_name "
			"AND last_seen_at < :as_of AND retired_at IS NULL"
		),
		{"county_slug": county_slug, "dataset_name": dataset_name, "as_of": as_of},
	)
	return ImportResult(rows_staged=staged_count, rows_upserted=rows_upserted, rows_retired=retire_result.rowcount)


# Explicit cast per column — the staging table is TEXT-only (COPY's simplest,
# most robust form; letting Postgres cast on the way into the typed real
# table avoids fighting COPY's text-format type inference for booleans,
# ints, and timestamps).
def _pg_type(column: str) -> str:
	if column in ("unit_count", "roll_year", "homestead_roll_year", "homestead_use_pct"):
		return "integer"
	if column == "exemption_excluded":
		return "boolean"
	if column == "source_record_updated_at":
		return "date"
	if column == "raw_payload":
		return "jsonb"
	return "text"


def import_pinellas_exemptions(
	session: Session,
	*,
	raw_rows: Iterator[dict],
	as_of: datetime,
	batch_size: int = 5_000,
	min_row_count: int = _ABSOLUTE_ROW_FLOOR,
) -> ImportResult:
	"""RP_EXEMPTIONS — a pure UPDATE, never an upsert. See module
	docstring's Column Ownership section for why. `raw_rows` are the
	source CSV rows exactly as read (STRAP, HX_YR, HX_YN, HX_USE,
	HX_STATUS, PROPERTY_EXEMPTION), NOT pre-mapped — the 3-row-per-parcel
	windowing (current/previous/next roll year) happens here via a SQL
	join against assessor_parcels.roll_year (set by RP_PROPERTY_INFO's own
	import), then derive_pinellas_exemption_update (mapping.py, already
	unit-tested) computes each parcel's update from the joined raw rows —
	one implementation of that logic, not a second copy re-encoded in SQL.
	"""
	staging_columns = ("strap", "hx_yr", "hx_yn", "hx_use", "hx_status", "property_exemption")
	temp_table = "tmp_assessor_pinellas_exemptions_staging"
	session.execute(text(f"DROP TABLE IF EXISTS {temp_table}"))
	session.execute(
		text(f"CREATE TEMP TABLE {temp_table} ({', '.join(f'{c} TEXT' for c in staging_columns)}) ON COMMIT DROP")
	)
	# Explicit key mapping, not a bare row.get(lowercase_col) — the real
	# CSV's columns are uppercase (STRAP, HX_YR, ...); a bare lowercase
	# lookup would silently return None for every field on a case
	# mismatch (confirmed by hitting exactly this against the live DB: a
	# 3-row staged import that produced zero updates, no error anywhere).
	def _adapt_exemption_row(raw_row: dict) -> dict:
		return {
			"strap": raw_row.get("STRAP"),
			"hx_yr": raw_row.get("HX_YR"),
			"hx_yn": raw_row.get("HX_YN"),
			"hx_use": raw_row.get("HX_USE"),
			"hx_status": raw_row.get("HX_STATUS"),
			"property_exemption": raw_row.get("PROPERTY_EXEMPTION"),
		}

	staged_count = _copy_rows(session, temp_table, staging_columns, (_adapt_exemption_row(r) for r in raw_rows))
	_validate_row_count(session, "pinellas_fl", "RP_EXEMPTIONS", staged_count, min_row_count)

	joined = session.execute(
		text(
			f"""
			SELECT
			    ap.assessor_parcel_id AS strap, ap.roll_year AS parcel_roll_year,
			    cur.hx_yn AS cur_hx_yn, cur.hx_use AS cur_hx_use, cur.hx_status AS cur_hx_status,
			    cur.property_exemption AS cur_exemption,
			    prev.hx_yn AS prev_hx_yn, prev.property_exemption AS prev_exemption,
			    nxt.hx_yn AS nxt_hx_yn, nxt.property_exemption AS nxt_exemption
			FROM assessor_parcels ap
			LEFT JOIN {temp_table} cur ON cur.strap = ap.assessor_parcel_id AND cur.hx_yr::int = ap.roll_year
			LEFT JOIN {temp_table} prev ON prev.strap = ap.assessor_parcel_id AND prev.hx_yr::int = ap.roll_year - 1
			LEFT JOIN {temp_table} nxt ON nxt.strap = ap.assessor_parcel_id AND nxt.hx_yr::int = ap.roll_year + 1
			WHERE ap.county_slug = 'pinellas_fl' AND ap.roll_year IS NOT NULL
			  AND (cur.strap IS NOT NULL OR prev.strap IS NOT NULL OR nxt.strap IS NOT NULL)
			"""
		)
	)

	rows_upserted = 0
	batch: list[dict] = []

	def _flush(batch_rows: list[dict]) -> int:
		if not batch_rows:
			return 0
		update_temp = "tmp_assessor_exemptions_computed"
		session.execute(text(f"DROP TABLE IF EXISTS {update_temp}"))
		session.execute(
			text(
				f"CREATE TEMP TABLE {update_temp} "
				f"(strap TEXT, {', '.join(f'{c} TEXT' for c in _EXEMPTIONS_UPDATE_COLUMNS)}) ON COMMIT DROP"
			)
		)
		_copy_rows(session, update_temp, ("strap",) + _EXEMPTIONS_UPDATE_COLUMNS, iter(batch_rows))
		set_clause = ",\n                ".join(
			f"{c} = c.{c}::{_pg_type(c)}" for c in _EXEMPTIONS_UPDATE_COLUMNS if c != "homestead_previous"
		)
		# homestead_previous is FILE-derived (from the computed batch, above)
		# — the authoritative source for Pinellas, per the plan doc's
		# resolution of a genuine conflict between two possible sources.
		# homestead_changed_at is the exception: the file only says two
		# years differ, never *when* the observed change happened, so it
		# is derived here via ordinary DB-side transition detection —
		# `ap.homestead_status` on the right-hand side refers to the
		# pre-update value, standard SQL UPDATE...SET semantics, even
		# though homestead_status is also being SET in this same
		# statement. Never fires on the row's first-ever evaluation
		# (`<> 'UNKNOWN'` guard) — that would be a spurious drop signal,
		# not a real one.
		result = session.execute(
			text(
				f"""
				UPDATE assessor_parcels ap SET
				    {set_clause},
				    homestead_previous = c.homestead_previous::text,
				    homestead_changed_at = CASE
				        WHEN ap.homestead_status IS DISTINCT FROM c.homestead_status::text
				         AND ap.homestead_status <> 'UNKNOWN'
				        THEN :as_of ELSE ap.homestead_changed_at END
				FROM {update_temp} c
				WHERE ap.county_slug = 'pinellas_fl' AND ap.assessor_parcel_id = c.strap
				"""
			),
			{"as_of": as_of},
		)
		return result.rowcount

	for row in joined:
		current_row = (
			{"HX_YN": row.cur_hx_yn, "HX_USE": row.cur_hx_use, "HX_STATUS": row.cur_hx_status,
			 "PROPERTY_EXEMPTION": row.cur_exemption, "HX_YR": row.parcel_roll_year}
			if row.cur_hx_yn is not None or row.cur_exemption is not None
			else None
		)
		previous_row = (
			{"HX_YN": row.prev_hx_yn, "PROPERTY_EXEMPTION": row.prev_exemption}
			if row.prev_hx_yn is not None or row.prev_exemption is not None
			else None
		)
		next_row = (
			{"HX_YN": row.nxt_hx_yn, "PROPERTY_EXEMPTION": row.nxt_exemption}
			if row.nxt_hx_yn is not None or row.nxt_exemption is not None
			else None
		)
		update = derive_pinellas_exemption_update(current_row, previous_row, next_row)
		batch.append({"strap": row.strap, **update})
		if len(batch) >= batch_size:
			rows_upserted += _flush(batch)
			batch = []
	rows_upserted += _flush(batch)

	return ImportResult(rows_staged=staged_count, rows_upserted=rows_upserted, rows_retired=0)
