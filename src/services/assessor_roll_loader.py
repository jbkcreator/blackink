"""County assessor tax-roll loader (S-24, W2 §3.2.4 A).

raw_assessor_parcels (migrations/apply_raw_assessor_parcels.py) has sat
empty since Akrash was never contracted to supply it — reclassified as
platform dev work rather than an external blocker, since the source data
is public county tax-roll information, not a vendor feed. It is NOT a
stable self-serve HTTP download, though: Hillsborough's own site sells its
full assessment extract as a paid, manually-ordered product, and Florida
DOR's exact current-year download path is unverified. Acquisition is
therefore a manual operator step — buy/download the county's roll extract,
place it at the path config/settings.py names for that county — and this
module is what turns that file into rows in raw_assessor_parcels.

Column names are DELIBERATELY alias-tolerant, not a fixed schema: the exact
header layout of a real Hillsborough or Pinellas extract is unverified
until an operator obtains one (same class of caveat portal_parsers.py
already carries for its own regexes, built against representative samples
rather than real captured data). REQUIRED_COLUMN_ALIASES lists every header
spelling this loader currently recognizes; parse_roll_file() raises
UnrecognizedColumnsError naming exactly which required field it could not
find, rather than silently importing partial/wrong data — a real file with
different headers needs this list extended, not a fallback guess.

A county's tax roll is a full point-in-time snapshot, not an incremental
feed — so a real file change means the WHOLE county's prior rows are
replaced, not merged (see import_county_roll()'s docstring for the
transactional DELETE+INSERT this implies).
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.winback_ingest import normalize_address

logger = logging.getLogger(__name__)

# Header aliases this loader recognizes, lowercased for case-insensitive
# matching. `site_addr`/`owner`/`folio` are CONFIRMED against a real prior
# Hillsborough County Property Appraiser (HCPA) bulk parcel export's actual
# column schema (FOLIO/OWNER/SITE_ADDR/SITE_CITY/SITE_ZIP/...), per the
# canonical `master_data` schema in a sibling repo's column-mapping module
# (jbkcreator/Forced-action-, src/loaders/column_mapper.py) that has
# previously downloaded and loaded this exact county's parcel data — not a
# guess. Every other alias here remains a documented assumption pending
# verification against a real Pinellas file, which uses a different portal
# and is not covered by that confirmation.
_ADDRESS_ALIASES = {"parcel_address", "situs_address", "property_address", "address", "site_addr", "phy_addr1"}
_OWNER_ALIASES = {"owner_name", "owner_name_on_roll", "own_name", "owner"}
_PARCEL_ID_ALIASES = {"parcel_id", "assessor_parcel_id", "folio", "parcel_number", "parcel"}

_REQUIRED_FIELD_ALIASES = {
	"address": _ADDRESS_ALIASES,
	"owner_name": _OWNER_ALIASES,
}


class UnrecognizedColumnsError(ValueError):
	"""Raised when a required field (address or owner_name) has no
	recognized header alias in the file. Names exactly which field —
	never silently imports a file missing a column this lookup depends
	on."""


@dataclass
class ParsedParcelRow:
	parcel_address_raw: str
	parcel_address_normalized: str
	owner_name_on_roll: str
	assessor_parcel_id: Optional[str]
	raw_payload: dict


def _resolve_header_map(fieldnames: list[str]) -> dict[str, str]:
	"""Maps each of our canonical field names -> the actual header string
	present in this file, or raises UnrecognizedColumnsError for a required
	field with no matching alias present."""
	lowered = {fn.strip().lower(): fn for fn in fieldnames if fn}
	resolved: dict[str, str] = {}

	for canonical, aliases in _REQUIRED_FIELD_ALIASES.items():
		match = next((lowered[a] for a in aliases if a in lowered), None)
		if match is None:
			raise UnrecognizedColumnsError(
				f"no recognized column for required field {canonical!r} — "
				f"file headers were {fieldnames!r}, recognized aliases are {sorted(aliases)!r}"
			)
		resolved[canonical] = match

	parcel_id_match = next((lowered[a] for a in _PARCEL_ID_ALIASES if a in lowered), None)
	if parcel_id_match:
		resolved["parcel_id"] = parcel_id_match

	return resolved


def parse_roll_file(file_bytes: bytes) -> list[ParsedParcelRow]:
	"""Parses a CSV (or any csv.Sniffer-detectable delimited text) county
	roll extract into ParsedParcelRow objects. A row missing a required
	value (blank address or owner name) is skipped, not fabricated — logged
	at debug, not raised, since a handful of blank rows in a real county
	extract is expected, not a file-format problem."""
	text_content = file_bytes.decode("utf-8-sig", errors="replace")
	sample = text_content[:4096]
	try:
		dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
	except csv.Error:
		dialect = csv.excel  # default to comma-delimited if sniffing fails

	reader = csv.DictReader(io.StringIO(text_content), dialect=dialect)
	if not reader.fieldnames:
		raise UnrecognizedColumnsError("file has no header row")

	header_map = _resolve_header_map(list(reader.fieldnames))

	rows: list[ParsedParcelRow] = []
	skipped = 0
	for raw_row in reader:
		address = (raw_row.get(header_map["address"]) or "").strip()
		owner_name = (raw_row.get(header_map["owner_name"]) or "").strip()
		if not address or not owner_name:
			skipped += 1
			continue
		parcel_id = None
		if "parcel_id" in header_map:
			parcel_id = (raw_row.get(header_map["parcel_id"]) or "").strip() or None
		rows.append(
			ParsedParcelRow(
				parcel_address_raw=address,
				parcel_address_normalized=normalize_address(address),
				owner_name_on_roll=owner_name,
				assessor_parcel_id=parcel_id,
				raw_payload=dict(raw_row),
			)
		)

	if skipped:
		logger.info("assessor_roll_loader: skipped %d row(s) missing address or owner_name", skipped)

	return rows


@dataclass
class ImportResult:
	status: str  # SUCCESS | UNCHANGED | FAILED | MISSING
	row_count: Optional[int] = None
	error: Optional[str] = None
	file_sha256: Optional[str] = None


def import_county_roll(
	session: Session,
	county_slug: str,
	file_bytes: Optional[bytes],
) -> ImportResult:
	"""Full replace of county_slug's raw_assessor_parcels rows, only when
	the file's content actually changed since the last SUCCESS for this
	county (compared by sha256, not mtime — a copy/redeploy that doesn't
	change content must not trigger a needless reimport). Does not commit —
	caller's transaction owns that, same convention as suppress_contact().

	file_bytes=None means the configured path didn't resolve to a readable
	file — returns MISSING without touching raw_assessor_parcels at all.

	A parse/validation failure (UnrecognizedColumnsError or any other
	exception from parse_roll_file) returns FAILED and leaves existing
	raw_assessor_parcels rows for this county completely untouched — a
	fail-closed guarantee: a botched new file can never wipe good existing
	data, mirroring this repo's own compliance-gate/settlement posture of
	never guessing past an unverified precondition."""
	if file_bytes is None:
		return ImportResult(status="MISSING")

	file_sha256 = hashlib.sha256(file_bytes).hexdigest()

	last = session.execute(
		text(
			"SELECT file_sha256 FROM assessor_roll_imports "
			"WHERE county_slug = :county_slug AND status = 'SUCCESS' "
			"ORDER BY imported_at DESC LIMIT 1"
		),
		{"county_slug": county_slug},
	).first()
	if last is not None and last[0] == file_sha256:
		return ImportResult(status="UNCHANGED", file_sha256=file_sha256)

	try:
		parsed_rows = parse_roll_file(file_bytes)
	except Exception as exc:
		logger.error("assessor_roll_loader: parse failed for county_slug=%s: %s", county_slug, exc)
		return ImportResult(status="FAILED", error=str(exc), file_sha256=file_sha256)

	if not parsed_rows:
		return ImportResult(
			status="FAILED", error="file parsed but produced zero usable rows", file_sha256=file_sha256
		)

	# Atomic full replace: a tax roll is a point-in-time snapshot, not an
	# incremental feed, so a genuine content change means the WHOLE
	# county's prior rows are stale and must go, not be merged alongside
	# the new ones (which would leave check_still_owns()'s own
	# ORDER BY ingested_at DESC LIMIT 1 picking the newest of two
	# potentially-conflicting rows for the same address, rather than there
	# being exactly one live snapshot per county at a time).
	session.execute(text("DELETE FROM raw_assessor_parcels WHERE county_slug = :county_slug"), {"county_slug": county_slug})

	import json as _json
	for row in parsed_rows:
		session.execute(
			text(
				"INSERT INTO raw_assessor_parcels "
				"(county_slug, parcel_address_raw, parcel_address_normalized, "
				" owner_name_on_roll, assessor_parcel_id, raw_payload) "
				"VALUES (:county_slug, :addr_raw, :addr_norm, :owner, :parcel_id, :raw_payload ::jsonb)"
			),
			{
				"county_slug": county_slug,
				"addr_raw": row.parcel_address_raw,
				"addr_norm": row.parcel_address_normalized,
				"owner": row.owner_name_on_roll,
				"parcel_id": row.assessor_parcel_id,
				"raw_payload": _json.dumps(row.raw_payload),
			},
		)

	return ImportResult(status="SUCCESS", row_count=len(parsed_rows), file_sha256=file_sha256)
