"""Win-Back CSV ingest pipeline (Subtask 3.1.1 — Lost-Owner CSV Ingest with
Assessor & FRBO Cross-Reference).

Turns a client's lost-owner CSV into a dispositioned, compliance-scrubbed
row set. Ingest + qualification only — this module never sends anything;
the 3-touch email sequence that consumes its output is Subtask 3.1.2, a
separate piece of work.

Runs under the system role (BYPASSRLS), same justification as
sequence_enrollment.may_enroll and promotion_sweep.py: the non-poach check
(step 5 below) must see every client's client_pm_books rows, not just the
importing client's own, and the assessor lookup reads assessor_parcels,
which carries no client_id at all. Must NEVER be imported from
src/api/ — batch-only per CLAUDE.md's stated blackink_system posture. The
router (src/api/winback_router.py) only creates the winback_imports row
under the tenant-scoped app role, then hands off to run_import() here.

See docs/plans/2026-09-07-subtask-3.1.1-winback-csv-ingest-assessor-frbo.md
for the full design rationale, including the two documented deviations from
the spec's literal wording (the SOLD-vs-UNKNOWN split, and why suppression
lands on winback_rows rather than "contacts").
"""

from __future__ import annotations

import csv
import io
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from rapidfuzz import fuzz
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.core.database import get_system_db_context
from src.services.address_normalize import normalize_address
from src.services.email_suppression import _normalize_phone
from src.services.events import log_event

logger = logging.getLogger(__name__)

# rapidfuzz token_sort_ratio threshold for "same owner name" — same
# conservative posture and same library call as dbpr_licence.py's own
# assessor/roll-style name match (that file's docstring explains the
# false-positive risk this threshold is chosen to avoid).
_OWNER_NAME_MATCH_THRESHOLD = 85

_REQUIRED_CSV_COLUMNS = ("owner_name", "property_address", "county")
# phone/email (not in the tuple above) are legal to omit — an owner with no
# last-known contact method is exactly the row Subtask 3.2.1's enrichment
# step exists to rescue. Neither Blueprint v2 nor the client's own comments
# specify the CSV's columns at all; the five-column "minimum required" list
# that used to appear here traced only to the derived
# Week2_Tasks_Dev_Split_v1.md:236, not to the client. Requiring phone meant a
# client export with no phone column produced 500 rows of UNKNOWN and zero
# outreach — see Subtask 3.2.1's plan doc §3 Step 0.


def _email_domain(email: Optional[str]) -> Optional[str]:
	if not email or "@" not in email:
		return None
	return email.rsplit("@", 1)[-1].strip().lower() or None


# ── Disposition values — see the plan doc's matrix for the full rationale ──
STILL_OWNS_STILL_RENTING = "STILL_OWNS_STILL_RENTING"
STILL_OWNS_NOT_RENTING = "STILL_OWNS_NOT_RENTING"
SOLD = "SOLD"
UNKNOWN = "UNKNOWN"

# Dispositions that can ever proceed to outreach (Subtask 3.1.2). Used to
# skip the DNC scrub (a paid Tracerfy credit per phone) on rows that can
# never be contacted regardless of DNC status — same cost-avoidance
# reasoning already applied to the FRBO lookup being skipped when
# still_owns isn't True.
_OUTREACH_ELIGIBLE_DISPOSITIONS = {STILL_OWNS_STILL_RENTING, STILL_OWNS_NOT_RENTING}

_DISPOSITION_BUCKET_KEYS = {
	STILL_OWNS_STILL_RENTING: "still_owns_still_renting_count",
	STILL_OWNS_NOT_RENTING: "still_owns_not_renting_count",
	SOLD: "sold_count",
	UNKNOWN: "unknown_count",
}


def compute_disposition(still_owns: Optional[bool], still_renting: Optional[bool]) -> str:
	"""The disposition matrix from the plan doc, as one pure function so its
	logic has exactly one place to be right (and one place to unit-test).

	still_owns=False means a CONFIRMED name mismatch on the parcel (real
	evidence of a sale) — distinct from still_owns=None (parcel not found /
	county not staged / lookup errored), which the spec's own literal
	wording would route to the same SOLD bucket but this pipeline
	deliberately does not: an assessor-roll coverage gap (a county not yet
	synced, or a parcel the sync hasn't reached) must never silently
	suppress a real lead forever. See the plan doc's disposition matrix.
	"""
	if still_owns is False:
		return SOLD
	if still_owns is not True:
		return UNKNOWN
	if still_renting is True:
		return STILL_OWNS_STILL_RENTING
	if still_renting is False:
		return STILL_OWNS_NOT_RENTING
	# still_renting is None/UNKNOWN (FRBO feed unavailable or unresolved) —
	# not spec-defined explicitly; resolved consistently with this
	# codebase's "never guess on missing data" posture (compliance_gate's
	# ABSTAIN-always-blocks, the OVS engine's MISSING_DATA) rather than
	# defaulting to NOT_RENTING.
	return UNKNOWN


# ============================================================================
# Assessor lookup — real, sourced from the daily county assessor sync
# (Subtask 3.1.1's original design assumed Akrash; corrected 2026-09-11 —
# see assessor_sync.py and the plan doc referenced above)
# ============================================================================

class AssessorProvider(ABC):
	@abstractmethod
	def check_still_owns(self, county_slug: str, address: str, owner_name: str) -> Optional[bool]:
		"""True (name matches roll), False (name mismatch — confirmed sold),
		or None (no parcel found for this address/county — unknown, not a
		sale)."""


class StagingTableAssessorProvider(AssessorProvider):
	"""Queries assessor_parcels — the daily-synced county tax assessor roll
	(src/tasks/assessor_sync.py, migrations/apply_assessor_sync.py). Real
	implementation, not a stub. Was previously assumed to be an
	Akrash-staged feed; that was never in the client's build spec (see
	docs/plans/2026-09-11-automated-county-assessor-data-sync.md's Context
	section) — assessor_sync.py is the actual, confirmed data source.

	Only matches an eligible, non-retired parcel: a retired parcel (fallen
	off the current roll) must never answer an ownership question with a
	stale owner name, and an ineligible parcel (government/commercial/
	vacant/etc — see mapping.py's per-county allowlist) was never a
	Win-Back target to begin with."""

	def __init__(self, session: Session):
		self._session = session

	def check_still_owns(self, county_slug: str, address: str, owner_name: str) -> Optional[bool]:
		normalized = normalize_address(address)
		row = self._session.execute(
			text(
				"SELECT owner_name_on_roll FROM assessor_parcels "
				"WHERE county_slug = :county_slug AND parcel_address_normalized = :address "
				"AND retired_at IS NULL AND is_blackink_eligible "
				"ORDER BY ingested_at DESC LIMIT 1"
			),
			{"county_slug": county_slug, "address": normalized},
		).fetchone()
		if row is None:
			return None
		roll_name = str(row[0] or "")
		score = fuzz.token_sort_ratio(owner_name.upper().strip(), roll_name.upper().strip())
		return score >= _OWNER_NAME_MATCH_THRESHOLD


# ============================================================================
# FRBO lookup — disabled adapter, vendor undecided (Subtask 3.1.1, BLOCKED)
# ============================================================================

class FrboProvider(ABC):
	@abstractmethod
	def check_active_listing(self, address: str) -> Optional[bool]:
		"""True (active rental listing found), False (none found), or None
		(feed unavailable / unresolved)."""


class StubFrboProvider(FrboProvider):
	"""No FRBO vendor contracted yet — see the plan doc's §FRBO
	cross-reference. RentCast has been suggested to the client (already
	named in the blueprint for the rent-estimate landing page) but nothing
	is confirmed. Mirrors compliance_gate.py's StubDncProvider pattern and
	the blueprint's own stated precedent for an uncontracted vendor
	("Define only a disabled adapter interface. Do not start a trial on the
	client's behalf.").

	Always returns None — every still_owns=True row lands in UNKNOWN
	disposition (never a guessed STILL_OWNS_NOT_RENTING) until a real
	provider replaces this one. That is a known, explicit, documented
	consequence of shipping without an FRBO vendor named — not a bug."""

	def check_active_listing(self, address: str) -> Optional[bool]:
		return None


# ============================================================================
# Non-poach check — client_pm_books, NOT is_claimed_by_other_client()
# ============================================================================

def _check_non_poach(session: Session, email: Optional[str]) -> bool:
	"""True if this owner is currently under active management by ANY
	client (including the importing client's own current book) — per the
	plan doc's confirmed design, this queries client_pm_books directly
	rather than the is_claimed_by_other_client() SQL function, which is
	company_id-keyed for the PM-firm-prospecting flow and does not apply to
	individual property owners with no company_id at all.

	Cross-client and cross-domain by design: an owner currently managed by
	ANYONE must never be contacted as a "lost" lead, whether that's another
	client's active poach conflict or this same client's own current
	customer mistakenly appearing in a "lost" export."""
	if not email:
		return False
	domain = _email_domain(email)
	# Case-insensitive on both sides — email addresses compare
	# case-insensitively in practice, and client_pm_books.owner_email is
	# PMS-synced data whose casing this pipeline doesn't control. An exact
	# match would silently miss a real conflict (Jane@Example.com vs
	# jane@example.com), same reasoning _resolve_county_slug already
	# applies to its county-name comparison.
	row = session.execute(
		text(
			"SELECT 1 FROM client_pm_books "
			"WHERE LOWER(owner_email) = LOWER(:email) "
			"   OR (owner_domain IS NOT NULL AND LOWER(owner_domain) = LOWER(:domain)) "
			"LIMIT 1"
		),
		{"email": email, "domain": domain},
	).fetchone()
	return row is not None


# ============================================================================
# CSV parsing / validation
# ============================================================================

@dataclass
class ParsedRow:
	owner_name: str
	property_address: str
	county_input: str
	phone: Optional[str]
	email: Optional[str]
	validation_error: Optional[str] = None


def parse_csv(raw_csv: str) -> list[ParsedRow]:
	"""Parse the uploaded CSV into ParsedRow objects. A row missing a
	required column is NOT dropped — it's kept with validation_error set, so
	it still surfaces in the export with disposition=UNKNOWN rather than
	silently vanishing (same "every row gets a disposition, never a silent
	drop" posture as promotion_sweep's reject_reason_code)."""
	reader = csv.DictReader(io.StringIO(raw_csv))
	rows: list[ParsedRow] = []
	for raw_row in reader:
		missing = [c for c in _REQUIRED_CSV_COLUMNS if not (raw_row.get(c) or "").strip()]
		error = f"missing required column(s): {', '.join(missing)}" if missing else None
		rows.append(
			ParsedRow(
				owner_name=(raw_row.get("owner_name") or "").strip(),
				property_address=(raw_row.get("property_address") or "").strip(),
				county_input=(raw_row.get("county") or "").strip(),
				phone=(raw_row.get("phone") or "").strip() or None,
				email=(raw_row.get("email") or "").strip() or None,
				validation_error=error,
			)
		)
	return rows


def _resolve_county_slug(session: Session, county_input: str) -> Optional[str]:
	"""Match the CSV's free-text county value against the real counties
	table — never geocoded, never guessed (no domain-to-county geocoding
	exists anywhere in this repo, per self_serve_audit_submissions' own
	documented precedent). Accepts either the slug or the display name."""
	if not county_input:
		return None
	row = session.execute(
		text(
			"SELECT county_slug FROM counties "
			"WHERE county_slug = :v OR LOWER(county_name) = LOWER(:v) LIMIT 1"
		),
		{"v": county_input},
	).fetchone()
	return row[0] if row else None


# ============================================================================
# Orchestration
# ============================================================================

def run_import(
	import_id: str,
	client_id: str,
	raw_csv: str,
	assessor_provider: Optional[AssessorProvider] = None,
	frbo_provider: Optional[FrboProvider] = None,
) -> dict:
	"""Process one uploaded CSV end to end: parse -> validate -> assessor +
	FRBO lookup -> disposition -> non-poach check -> DNC scrub -> persist ->
	event. Returns the per-bucket count dict written into the
	winback_import_completed event payload.

	Runs entirely under the system role — see module docstring for why.
	The winback_imports row itself is created by the caller (the router)
	under the tenant-scoped app role before this function is invoked; this
	function transitions it to COMPLETED itself — the router's caller sets
	FAILED only if this whole function raises (which, per row, it no longer
	should — see the per-row SAVEPOINT below).

	Each row is processed inside its own SAVEPOINT (session.begin_nested())
	so one row's failure — a NUL byte or other value Postgres rejects, a
	future live FRBO/assessor provider timing out — rolls back only that
	row, never the rows already inserted before it. Same "one bad row must
	not abort the whole batch" principle as BaseIngestLoader.safe_add's own
	SAVEPOINT pattern; without this, a single mid-batch exception would
	silently wipe every row processed so far (system_session_scope rolls
	back the ENTIRE session on any uncaught exception).

	Deliberately does NOT call any enrichment/skip-trace vendor (Subtask
	3.2.1) — this function is invoked synchronously from an async FastAPI
	handler (src/api/winback_router.py's upload_winback_csv), and Tracerfy's
	submit-then-poll API can block up to 10 minutes; doing that per-row here
	would badly worsen an already-blocking request path. Every
	outreach-eligible row this function inserts simply lands with
	enrichment_timestamp IS NULL, which is exactly
	src/tasks/enrichment_verification.py's claim-query predicate — so every
	such row is automatically queued for enrichment with no extra plumbing.
	That sweep (not this function) is what clears enrichment_timestamp
	before evaluate_winback_touch_gate or the /arm endpoint will allow a row
	to be sequenced.
	"""
	settings = get_settings()
	rows = parse_csv(raw_csv)

	counts = {
		"total_rows": len(rows),
		"still_owns_still_renting_count": 0,
		"still_owns_not_renting_count": 0,
		"sold_count": 0,
		"unknown_count": 0,
		"suppressed_count": 0,
	}

	with get_system_db_context() as session:
		provider = assessor_provider or StagingTableAssessorProvider(session)
		frbo = frbo_provider or StubFrboProvider()
		now = datetime.now(timezone.utc)
		dnc_check_targets: list[tuple[int, str]] = []  # (winback_row_id, normalized_phone)

		for row in rows:
			try:
				with session.begin_nested():
					_process_row(session, import_id, client_id, row, provider, frbo, now, counts, dnc_check_targets)
			except Exception:
				logger.error(
					"winback_ingest: row failed unexpectedly (owner_name=%r) — flagging for review",
					row.owner_name, exc_info=True,
				)
				try:
					with session.begin_nested():
						row.validation_error = (row.validation_error or "") + " | processing error, see server logs"
						_insert_row(
							session, import_id, client_id, row, county_slug=None,
							still_owns=None, still_renting=None, disposition=UNKNOWN,
							requires_human_review=True, now=now,
						)
					counts["unknown_count"] += 1
				except Exception:
					logger.error(
						"winback_ingest: fallback insert also failed (owner_name=%r) — row dropped entirely",
						row.owner_name, exc_info=True,
					)

		session.commit()

		if dnc_check_targets:
			_run_dnc_scrub(session, dnc_check_targets, settings, counts)
			session.commit()

		session.execute(
			text(
				"UPDATE winback_imports SET status = 'COMPLETED', completed_at = :now, "
				"row_count = :row_count WHERE import_id = :import_id"
			),
			{"now": now, "row_count": len(rows), "import_id": import_id},
		)
		session.commit()

		log_event(
			client_id,
			"winback_import_completed",
			entity_type="winback_import",
			entity_id=import_id,
			payload=counts,
			session=session,
		)
		session.commit()

	return counts


def _process_row(
	session: Session,
	import_id: str,
	client_id: str,
	row: ParsedRow,
	provider: AssessorProvider,
	frbo: FrboProvider,
	now: datetime,
	counts: dict,
	dnc_check_targets: list[tuple[int, str]],
) -> None:
	"""One row's worth of lookup + disposition + insert. Runs inside the
	caller's per-row SAVEPOINT (run_import) — mutates `counts` and
	`dnc_check_targets` only after the insert has actually happened, so a
	mid-function exception leaves both untouched (the savepoint rollback
	then undoes only the DB side)."""
	if row.validation_error:
		_insert_row(
			session, import_id, client_id, row, county_slug=None,
			still_owns=None, still_renting=None, disposition=UNKNOWN,
			requires_human_review=True, now=now,
		)
		counts["unknown_count"] += 1
		return

	county_slug = _resolve_county_slug(session, row.county_input)
	if county_slug is None:
		row.validation_error = f"unrecognized county: {row.county_input!r}"
		_insert_row(
			session, import_id, client_id, row, county_slug=None,
			still_owns=None, still_renting=None, disposition=UNKNOWN,
			requires_human_review=True, now=now,
		)
		counts["unknown_count"] += 1
		return

	still_owns = provider.check_still_owns(county_slug, row.property_address, row.owner_name)
	# Only spend an FRBO lookup when it can actually change the outcome — a
	# confirmed-sold or unresolved-ownership row is SOLD/UNKNOWN regardless
	# of rental status (see compute_disposition).
	still_renting = frbo.check_active_listing(row.property_address) if still_owns else None
	disposition = compute_disposition(still_owns, still_renting)
	requires_review = disposition == UNKNOWN

	non_poach_hit = _check_non_poach(session, row.email)
	if disposition == SOLD:
		# The DoD's own requirement: a confirmed-sold owner is suppressed
		# outright, independent of DNC/non-poach — there is no scenario
		# where a SOLD row should ever re-enter consideration.
		suppression_state = True
		suppression_reason = "SOLD"
	elif non_poach_hit:
		suppression_state = True
		suppression_reason = "NON_POACH_MATCH"
	else:
		suppression_state = False
		suppression_reason = None

	winback_row_id = _insert_row(
		session, import_id, client_id, row, county_slug=county_slug,
		still_owns=still_owns, still_renting=still_renting, disposition=disposition,
		requires_human_review=requires_review, now=now,
		suppression_state=suppression_state, suppression_reason=suppression_reason,
	)

	counts[_DISPOSITION_BUCKET_KEYS.get(disposition, "unknown_count")] += 1
	if suppression_state:
		counts["suppressed_count"] += 1
	elif disposition in _OUTREACH_ELIGIBLE_DISPOSITIONS:
		# DNC scrub runs AFTER disposition and non-poach, before any
		# sequence could arm — per the spec's explicit ordering requirement.
		# Gated on outreach-eligible dispositions (not just "isn't already
		# suppressed") — a SOLD/UNKNOWN row can never be sequenced regardless
		# of DNC status, so scrubbing its phone would only spend a Tracerfy
		# credit for nothing.
		if row.phone is None:
			# Legal since Subtask 3.2.1 (phone is an optional CSV column) —
			# an email-only owner is a perfectly valid win-back target with
			# nothing to scrub. Must NOT go through _flag_unscrubbed: that
			# sets suppression_state = TRUE, which would suppress every
			# email-only row outright — exactly the rows this optional-column
			# change exists to stop discarding.
			pass
		else:
			normalized_phone = _normalize_phone(row.phone)
			if normalized_phone:
				dnc_check_targets.append((winback_row_id, normalized_phone))
			else:
				# A phone value IS present in the CSV but doesn't normalize to
				# any digits at all (e.g. "n/a", "unknown") — previously fell
				# through here silently — never scrubbed, never flagged, never
				# suppressed (confirmed review finding). It can never be
				# scrubbed at all, so it gets the same fail-closed treatment as
				# a Tracerfy outage, not a silent skip. Distinct from "no phone
				# supplied at all" above: here the client supplied *something*
				# that turned out to be ungradeable, which stays fail-closed.
				_flag_unscrubbed(session, [winback_row_id], counts)


def _insert_row(
	session: Session,
	import_id: str,
	client_id: str,
	row: ParsedRow,
	*,
	county_slug: Optional[str],
	still_owns: Optional[bool],
	still_renting: Optional[bool],
	disposition: str,
	requires_human_review: bool,
	now: datetime,
	suppression_state: bool = False,
	suppression_reason: Optional[str] = None,
) -> int:
	result = session.execute(
		text(
			"INSERT INTO winback_rows ("
			"  import_id, client_id, owner_name, property_address_raw, property_address_normalized,"
			"  county_slug, phone, email, still_owns, still_renting, disposition,"
			"  requires_human_review, validation_error, assessor_checked_at, frbo_checked_at,"
			"  suppression_state, suppression_reason, created_at, updated_at"
			") VALUES ("
			"  :import_id, :client_id, :owner_name, :address_raw, :address_norm,"
			"  :county_slug, :phone, :email, :still_owns, :still_renting, :disposition,"
			"  :requires_review, :validation_error, :assessor_checked_at, :frbo_checked_at,"
			"  :suppression_state, :suppression_reason, :now, :now"
			") RETURNING winback_row_id"
		),
		{
			"import_id": import_id,
			"client_id": client_id,
			"owner_name": row.owner_name,
			"address_raw": row.property_address,
			"address_norm": normalize_address(row.property_address),
			"county_slug": county_slug,
			"phone": row.phone,
			"email": row.email,
			"still_owns": still_owns,
			"still_renting": still_renting,
			"disposition": disposition,
			"requires_review": requires_human_review,
			"validation_error": row.validation_error,
			"assessor_checked_at": now if county_slug else None,
			"frbo_checked_at": now if still_owns else None,
			"suppression_state": suppression_state,
			"suppression_reason": suppression_reason,
			"now": now,
		},
	)
	return result.scalar_one()


def dnc_scrub_rows(session: Session, targets: list[tuple[int, str]], settings, counts: dict) -> None:
	"""Public entry point to the DNC-scrub batch path, for callers outside
	this module — src/tasks/enrichment_verification.py (Subtask 3.2.1) scrubs
	phone numbers newly discovered by skip-trace through this same path, to
	preserve 3.1.1's ordering invariant (DNC scrub before any sequence can
	arm) for numbers that didn't exist at import time. Thin wrapper, not a
	re-implementation — see _run_dnc_scrub for the actual logic."""
	_run_dnc_scrub(session, targets, settings, counts)


def _run_dnc_scrub(
	session: Session,
	targets: list[tuple[int, str]],
	settings,
	counts: dict,
) -> None:
	"""Batch-scrubs all outreach-eligible rows' phones in one Tracerfy call
	(or as many BATCH_SIZE-sized calls as needed), per the spec's explicit
	"DNC scrub runs after disposition, before any sequence arm" ordering.

	Calls tracerfy_client.scrub_phones() directly rather than through
	compliance_gate.py's per-contact DncProvider interface — winback_rows
	isn't a `contacts` row, so evaluate_enrollment_gate doesn't apply, and
	(separately) compliance_gate.py deliberately has no live per-contact
	Tracerfy provider at all: a real submit/poll round trip can take up to
	10 minutes, fine for this function's own batch call but wrong for a
	synchronous per-contact gate. See compliance_gate.py's own comment on
	why that gate stays on StubDncProvider, and the plan doc's §Wiring
	Tracerfy for the full rationale.

	A total scrub failure (bad key, network error, vendor timeout) does NOT
	default rows to clean — _flag_unscrubbed sets suppression_state = TRUE
	(reason DNC_UNVERIFIED) and requires_human_review = TRUE, fail-closed,
	same "never guess on missing data" posture as everywhere else in this
	pipeline. See _flag_unscrubbed's own docstring for why
	requires_human_review alone was not enough (confirmed review finding).
	"""
	from src.services.tracerfy_client import BATCH_SIZE, scrub_phones

	if not settings.tracerfy_api_key:
		logger.warning("winback_ingest: TRACERFY_API_KEY not set — %d rows left unscrubbed, flagged for review",
					   len(targets))
		_flag_unscrubbed(session, [wid for wid, _ in targets], counts)
		return

	api_key = settings.tracerfy_api_key.get_secret_value()
	now = datetime.now(timezone.utc)
	phone_to_row_ids: dict[str, list[int]] = {}
	for winback_row_id, phone in targets:
		phone_to_row_ids.setdefault(phone, []).append(winback_row_id)

	phones = list(phone_to_row_ids.keys())
	unscrubbed_row_ids: list[int] = []

	for batch_start in range(0, len(phones), BATCH_SIZE):
		batch = phones[batch_start: batch_start + BATCH_SIZE]
		try:
			results = scrub_phones(batch, api_key)
		except Exception:
			logger.error("winback_ingest: Tracerfy batch scrub failed", exc_info=True)
			for phone in batch:
				unscrubbed_row_ids.extend(phone_to_row_ids[phone])
			continue

		for phone in batch:
			row_ids = phone_to_row_ids[phone]
			is_clean = results.get(phone)
			if is_clean is None:
				unscrubbed_row_ids.extend(row_ids)
				continue
			session.execute(
				text(
					"UPDATE winback_rows SET dnc_clean = :clean, dnc_checked_at = :now, "
					"suppression_state = suppression_state OR :hit, "
					"suppression_reason = CASE WHEN :hit THEN 'DNC_LISTED' ELSE suppression_reason END, "
					"updated_at = :now "
					"WHERE winback_row_id = ANY(:ids)"
				),
				{"clean": is_clean, "hit": not is_clean, "now": now, "ids": row_ids},
			)
			if not is_clean:
				counts["suppressed_count"] += len(row_ids)

	if unscrubbed_row_ids:
		_flag_unscrubbed(session, unscrubbed_row_ids, counts)


def _flag_unscrubbed(session: Session, winback_row_ids: list[int], counts: Optional[dict] = None) -> None:
	"""A row whose DNC status could not be affirmatively verified — missing
	API key, a batch call failure, a batch response omitting this phone, or
	(from _process_row) a phone that couldn't be normalized at all.

	Fail-closed (review finding, confirmed): `requires_human_review = TRUE`
	alone is NOT enough — nothing downstream (the /arm endpoint's SQL,
	evaluate_winback_touch_gate) inspects that column, only
	`suppression_state`. Setting `suppression_state = TRUE` with a distinct
	`DNC_UNVERIFIED` reason reuses every existing consumer of that flag
	(same posture as `StubDncProvider`/ABSTAIN in compliance_gate.py — never
	guess clean on missing data). A human clearing this — confirming
	DNC-clean out of band, or a later successful re-scrub — must flip
	`suppression_state` back to `FALSE` explicitly; nothing here does that
	automatically."""
	if not winback_row_ids:
		return
	session.execute(
		text(
			"UPDATE winback_rows SET requires_human_review = TRUE, "
			"suppression_state = TRUE, suppression_reason = 'DNC_UNVERIFIED', updated_at = NOW() "
			"WHERE winback_row_id = ANY(:ids)"
		),
		{"ids": winback_row_ids},
	)
	if counts is not None:
		counts["suppressed_count"] += len(winback_row_ids)


_FORMULA_LEADING_CHARS = ("=", "+", "-", "@")


def _csv_safe(value) -> object:
	"""Neutralize a leading formula-trigger character (=, +, -, @) so a
	client-CSV-controlled string (owner_name, property_address — this
	pipeline's input, not a fully trusted first-party source) can't execute
	as a formula when the exported CSV is opened in Excel/Sheets. Prefixing
	a single quote is the standard mitigation; non-strings pass through
	unchanged."""
	if isinstance(value, str) and value.startswith(_FORMULA_LEADING_CHARS):
		return "'" + value
	return value


def export_csv(session: Session, import_id: str) -> str:
	"""Render the dispositioned rows for one import back out as CSV — the
	admin router's GET /export.csv endpoint. Column order matches the
	original input columns plus the pipeline's own output columns."""
	rows = session.execute(
		text(
			"SELECT owner_name, property_address_raw, county_slug, phone, email,"
			" disposition, still_owns, still_renting, requires_human_review,"
			" suppression_state, suppression_reason, validation_error"
			" FROM winback_rows WHERE import_id = :import_id ORDER BY winback_row_id"
		),
		{"import_id": import_id},
	).fetchall()

	buf = io.StringIO()
	writer = csv.writer(buf)
	writer.writerow(
		[
			"owner_name", "property_address", "county", "phone", "email",
			"disposition", "still_owns", "still_renting", "requires_human_review",
			"suppression_state", "suppression_reason", "validation_error",
		]
	)
	for row in rows:
		writer.writerow([_csv_safe(v) for v in row])
	return buf.getvalue()
