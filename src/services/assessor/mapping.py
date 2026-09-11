"""Pure logic for the county assessor sync: field mapping, per-county use-code
eligibility, and homestead derivation. No I/O, no DB — everything here is a
plain function over dicts/strings, unit-tested with no network or database
(see tests/test_assessor_mapping.py, tests/test_assessor_use_codes.py).

See docs/plans/2026-09-11-automated-county-assessor-data-sync.md §2.4 for the
full design rationale and the review history that shaped every allowlist
decision below — several were corrected against real source data (the
Hillsborough DOR code manual, the official Pinellas use-code list, and real
RP_EXEMPTIONS sample rows) after an earlier draft's assumptions turned out to
be wrong.
"""
from __future__ import annotations

import json
import re
from typing import Optional, TypedDict, Union

from src.services.address_normalize import normalize_address


# ============================================================================
# Homestead status vocabulary — shared with the CHECK constraint in
# migrations/apply_assessor_sync.py. Keep in sync if either changes.
# ============================================================================

HOMESTEAD = "HOMESTEAD"
NO_HOMESTEAD = "NO_HOMESTEAD"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"


# ============================================================================
# US state normalization — required before out_of_state_owner (a STORED
# generated column comparing owner_mailing_state to 'FL') means anything.
# Hillsborough's STATE field is C(25) and can hold "FLORIDA", not just "FL".
# ============================================================================

_STATE_NAME_TO_ABBREV = {
	"ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
	"CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
	"FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
	"ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
	"KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
	"MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
	"MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
	"NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
	"NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
	"OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
	"SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
	"VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
	"WISCONSIN": "WI", "WYOMING": "WY",
	"DISTRICT OF COLUMBIA": "DC", "PUERTO RICO": "PR",
	"AMERICAN SAMOA": "AS", "GUAM": "GU", "NORTHERN MARIANA ISLANDS": "MP",
	"US VIRGIN ISLANDS": "VI", "VIRGIN ISLANDS": "VI",
}
_VALID_ABBREVS = frozenset(_STATE_NAME_TO_ABBREV.values())


def normalize_state(raw: Optional[str]) -> Optional[str]:
	"""Full name or 2-letter abbreviation -> canonical 2-letter abbreviation.
	Unrecognized input -> None (never a guess), so a downstream generated
	column comparing to 'FL' correctly yields NULL rather than a false
	TRUE/FALSE for garbage input."""
	if not raw:
		return None
	value = str(raw).strip().upper()
	if value in _VALID_ABBREVS:
		return value
	return _STATE_NAME_TO_ABBREV.get(value)


# ============================================================================
# Per-county use-code allowlist — COUNTY-SCOPED NAMESPACES, never a shared
# vocabulary. The same 4-digit code means different (sometimes opposite)
# things in each county's own numbering — e.g. Hillsborough 0111 is
# "Residential permit pending" (excluded) while Pinellas 0111 is "Single
# Family Community Land Trust" (a real, if excluded-by-policy, residential
# use). Keying by code alone, without the county, would silently apply one
# county's rules to the other's parcels. See the plan doc's §2.4 warning box.
# ============================================================================

# Hillsborough — built from HCPA's own _DOR_Code_Manual.docx (196 documented
# codes, read 2026-09-11). Comments are the manual's own descriptions.
_HILLSBOROUGH_ALLOWLIST: dict[str, str] = {
	# STANDARD_RESIDENTIAL. NOTE: "0111"/"0112" are DELIBERATELY ABSENT here
	# ("Residential permit pending" / "Subsidence permit pending" — not a
	# standing rental yet) — Pinellas uses the SAME code strings for a
	# completely different, genuinely-residential meaning (see
	# _PINELLAS_ALLOWLIST below). This is the concrete proof that the
	# allowlist must be keyed per county, never globally — see
	# tests/test_assessor_use_codes.py's cross-county divergence test.
	"0100": "STANDARD_RESIDENTIAL",  # Single Family Residential
	"0102": "STANDARD_RESIDENTIAL",  # SFR built around a mobile home
	"0106": "STANDARD_RESIDENTIAL",  # Townhouse/Villa
	"0200": "STANDARD_RESIDENTIAL",  # Mobile Home
	"0300": "STANDARD_RESIDENTIAL",  # MFR > 9 units (base code)
	"0310": "STANDARD_RESIDENTIAL",  # MFR Class A
	"0320": "STANDARD_RESIDENTIAL",  # MFR Class B
	"0330": "STANDARD_RESIDENTIAL",  # MFR Class C
	"0340": "STANDARD_RESIDENTIAL",  # MFR Class D
	"0350": "STANDARD_RESIDENTIAL",  # MFR Class E
	"0400": "STANDARD_RESIDENTIAL",  # Condominium
	"0403": "STANDARD_RESIDENTIAL",  # Platted as condos, now rented as apartments
	"0408": "STANDARD_RESIDENTIAL",  # Mobile home condominium
	"0500": "STANDARD_RESIDENTIAL",  # Cooperative
	"0700": "STANDARD_RESIDENTIAL",  # Miscellaneous residential
	"0800": "STANDARD_RESIDENTIAL",  # MFR < 10 units (base code)
	"0801": "STANDARD_RESIDENTIAL",  # 3-9 units, individually owned
	"0802": "STANDARD_RESIDENTIAL",  # 3-9 units rentals — the core small-multifamily target
	# MOBILE_HOME_PARK — client decision 2026-09-11: include, RV parks excluded
	"2810": "MOBILE_HOME_PARK",  # Mobile Home Park (base code)
	"2811": "MOBILE_HOME_PARK",  # MHP Class A
	"2812": "MOBILE_HOME_PARK",  # MHP Class B
	"2813": "MOBILE_HOME_PARK",  # MHP Class C
	"2814": "MOBILE_HOME_PARK",  # MHP Class D
	# MIXED_USE_RESIDENTIAL — client decision 2026-09-11: include, flagged separately
	"1201": "MIXED_USE_RESIDENTIAL",  # Mixed Use Residential (base code)
	"1203": "MIXED_USE_RESIDENTIAL",  # Mixed Use Multi-Family
	# Deliberately excluded (all confirmed against the manual, never guessed):
	#   0000/0008/9900/1000/4000 vacant; 0111/0112 permit pending;
	#   0192/0193 newly-platted subdivision land lines (vacant);
	#   0396/0397/0398/0399 student/rural-development/HUD/LIHTC housing;
	#   0600 + 0610-0641 + 0650-0680 retirement/ALF/ILF/nursing (licensed
	#   care facilities); 2815 migrant housing, 2820 RV park (transient,
	#   excluded per client decision); 7000+/8000+/9000+ institutional,
	#   government, utilities, ROW, submerged, mining.
}

# Pinellas — built from PCPAO's own "Use Codes - Land & Property" list
# (pcpao.gov/learn-about/use-codes, supplied 2026-09-11). Comments are the
# county's own descriptions. Note these 4-digit codes do NOT correspond to
# Hillsborough's — e.g. Pinellas writes 0110 for Single Family where
# Hillsborough writes 0100; the two counties only agree at the (unused-here)
# 2-digit LAND_USE_CD level.
_PINELLAS_ALLOWLIST: dict[str, str] = {
	# STANDARD_RESIDENTIAL. "0111"/"0112" ARE genuinely residential here
	# (Community Land Trust / Habitat Ground Lease single-family homes —
	# the SAME code strings mean "permit pending" in Hillsborough, see
	# above). Classified accurately here; excluded from targeting below by
	# an explicit, separately-reversible business-decision denylist, not by
	# omission — the reason for exclusion (an ICP choice, not a use-code
	# fact) stays traceable and independently correctable.
	"0110": "STANDARD_RESIDENTIAL",  # Single Family Home
	"0111": "STANDARD_RESIDENTIAL",  # Single Family Community Land Trust
	"0112": "STANDARD_RESIDENTIAL",  # Single Family - Habitat Ground Lease
	"0113": "STANDARD_RESIDENTIAL",  # SF with Land Condo
	"0133": "STANDARD_RESIDENTIAL",  # Planned Unit Development
	"0134": "STANDARD_RESIDENTIAL",  # Planned Unit Dev - Community Land Trust
	"0135": "STANDARD_RESIDENTIAL",  # Planned Unit Dev - Habitat Ground Lease
	"0260": "STANDARD_RESIDENTIAL",  # Manufactured Home (individually owned lot)
	"0261": "STANDARD_RESIDENTIAL",  # Manufactured Home (Co-Op, individually owned)
	"0262": "STANDARD_RESIDENTIAL",  # Manufactured Home (Land Condo, individually owned)
	"0263": "STANDARD_RESIDENTIAL",  # Manufactured Home (leased lot)
	"0310": "STANDARD_RESIDENTIAL",  # Apartments (50 units or more)
	"0311": "STANDARD_RESIDENTIAL",  # Apartments (10-49 units)
	"0410": "STANDARD_RESIDENTIAL",  # Condo conversion (predominately apartment use)
	"0430": "STANDARD_RESIDENTIAL",  # Condominium
	"0431": "STANDARD_RESIDENTIAL",  # Condominium (land lease)
	"0436": "STANDARD_RESIDENTIAL",  # Condo conversion (predominately owner-occupied)
	"0437": "STANDARD_RESIDENTIAL",  # Condo Com Apartments
	"0499": "STANDARD_RESIDENTIAL",  # Condo Com Apartments
	"0550": "STANDARD_RESIDENTIAL",  # CO-OP Apartments
	"0551": "STANDARD_RESIDENTIAL",  # CO-OP Apartments (land lease)
	"0740": "STANDARD_RESIDENTIAL",  # Miscellaneous Residential
	"0810": "STANDARD_RESIDENTIAL",  # Single Family - more than one house per parcel
	"0820": "STANDARD_RESIDENTIAL",  # Duplex-Triplex-Fourplex
	"0821": "STANDARD_RESIDENTIAL",  # Townhouse, attached, separate entrances
	"0822": "STANDARD_RESIDENTIAL",  # Apartments (5-9 units)
	"0830": "STANDARD_RESIDENTIAL",  # Single Family with Accessory Dwelling
	# MOBILE_HOME_PARK — client decision 2026-09-11: include, RV park (2815) excluded.
	# Pinellas cleanly separates these, unlike the coarser 2-digit LAND_USE_CD.
	"2814": "MOBILE_HOME_PARK",  # Manufactured Home Park (Lot Rental Community)
	"2816": "MOBILE_HOME_PARK",  # Manufactured Home Park (Lot & Unit Rental)
	"2817": "MOBILE_HOME_PARK",  # Manufactured Home Park - Mixed Usage (stores/apts, etc)
	# MIXED_USE_RESIDENTIAL — closest Pinellas analogue to Hillsborough's 1201/1203
	"1227": "MIXED_USE_RESIDENTIAL",  # Store w/Office or Apartment
	# Deliberately excluded (confirmed against the official PCPAO list):
	#   2815 Campground - RV park; 0000/0030/0033/0040/0060-0062/0090 vacant;
	#   0904-0976 association-owned common areas/ROW/submerged land;
	#   0435 condo parking/garage/storage/cabana (not a dwelling);
	#   0442/0443 interval ownership/timeshare (transient);
	#   0752/7456/7837 ALF boarding house/ALF 10+/skilled nursing (licensed
	#   care facilities); 7000-7953/8000-8913/9000+ institutional,
	#   government, utilities, ROW, submerged, mining; 9999 "To Be
	#   Determined" (unclassified, fail closed).
	#   Also excluded, a business decision (not a data gap — see Open
	#   Question 1 in the plan doc): 0111/0134 Community Land Trust and
	#   0112/0135 Habitat Ground Lease — affordable-housing ground-lease
	#   programs, not third-party-property-management prospects.
}

_ALLOWLISTS: dict[str, dict[str, str]] = {
	"hillsborough_fl": _HILLSBOROUGH_ALLOWLIST,
	"pinellas_fl": _PINELLAS_ALLOWLIST,
}

# Business decision (2026-09-11, Open Question 1 in the plan doc), not a
# use-code correction — kept separate from the allowlist itself precisely so
# it stays independently reversible and its reason (an ICP choice) doesn't
# get confused with "this code doesn't mean a residential use."
_PINELLAS_EXCLUDED_BY_POLICY = frozenset({"0111", "0112", "0134", "0135"})


def classify_property_use(county_slug: str, code_raw: Optional[str]) -> Optional[str]:
	"""Returns the property_class ('STANDARD_RESIDENTIAL' / 'MOBILE_HOME_PARK'
	/ 'MIXED_USE_RESIDENTIAL') for this county's finest-grain use code, or
	None if the code is not on this county's allowlist (including an
	unrecognized/unparseable code, which fails closed rather than guessing)
	or is excluded by an explicit business-decision denylist.
	`county_slug` MUST be one of the counties this sync covers; an unknown
	county is a caller bug, not a data-quality issue, so it raises."""
	allowlist = _ALLOWLISTS.get(county_slug)
	if allowlist is None:
		raise ValueError(f"no use-code allowlist registered for county_slug={county_slug!r}")
	if not code_raw:
		return None
	code = str(code_raw).strip()
	if county_slug == "pinellas_fl" and code in _PINELLAS_EXCLUDED_BY_POLICY:
		return None
	return allowlist.get(code)


# ============================================================================
# Secondary safeguard — owner-name government check. Per the task doc §5A,
# this is SECONDARY only: the use/exemption code is always the primary
# filter. Deliberately narrow (a handful of anchored patterns) to avoid
# excluding a legitimately-named private owner, e.g. "LIBERTY COUNTY LLC".
# ============================================================================

_GOVERNMENT_OWNER_PATTERNS = (
	re.compile(r"^UNITED STATES\b"),
	re.compile(r"^STATE OF FLORIDA\b"),
	re.compile(r"\bCOUNTY OF\b"),
	re.compile(r"^HILLSBOROUGH COUNTY\b"),
	re.compile(r"^PINELLAS COUNTY\b"),
	re.compile(r"^CITY OF\b"),
	re.compile(r"\bSCHOOL BOARD\b"),
)


def looks_like_government_owner(owner_name: Optional[str]) -> bool:
	"""Secondary safeguard only — never the primary eligibility filter."""
	if not owner_name:
		return False
	name = owner_name.strip().upper()
	return any(pattern.search(name) for pattern in _GOVERNMENT_OWNER_PATTERNS)


# ============================================================================
# Pinellas exemption-text classification (RP_EXEMPTIONS.PROPERTY_EXEMPTION)
# ============================================================================

# The county's own field description names "Government, Agricultural..." as
# examples, not an exhaustive enumeration (unlike the DOR/use codes, which
# ARE exhaustively documented) — so this is keyword-based, not a lookup
# table, and a blank value means "no exemption on file", not "unknown".
_EXEMPTION_EXCLUSION_KEYWORDS = (
	"GOVERNMENT", "AGRICULTURAL", "MUNICIPAL", "COUNTY", "STATE", "FEDERAL",
	"SCHOOL", "CHURCH", "RELIGIOUS", "CHARITABLE", "NON-PROFIT", "NONPROFIT",
	"INSTITUTIONAL", "HOSPITAL",
)


def classify_pinellas_exemption(property_exemption_raw: Optional[str]) -> bool:
	"""True if this exemption text excludes the parcel from Blackink
	targeting (government/agricultural/institutional/etc), False otherwise
	— including a blank value, which means "no exemption on file" (a real,
	evaluated answer), never "unknown". The caller (importer.py) is
	responsible for the separate NULL/"not yet evaluated" state at the
	database level — this function only ever returns a concrete bool."""
	if not property_exemption_raw:
		return False
	text = property_exemption_raw.strip().upper()
	return any(keyword in text for keyword in _EXEMPTION_EXCLUSION_KEYWORDS)


# ============================================================================
# Hillsborough homestead derivation (BASE column)
# ============================================================================


def derive_hillsborough_homestead(property_class: Optional[str], base_raw: Optional[int]) -> tuple[str, Optional[str]]:
	"""Task doc §4's rule, exactly: BASE is only a homestead-approval year
	for a residential parcel; for anything else it can represent a
	different kind of cap and must not be read as homestead at all.

	"Residential" here is deliberately defined as
	property_class == 'STANDARD_RESIDENTIAL' — the classic homesteadable
	category (single family / condo / co-op / small multifamily) — not the
	broader literal sense that would include a mobile home PARK (a
	business, not an owner-occupied residence) or a mixed-use commercial
	parcel, neither of which is eligible for Florida's homestead exemption
	in the first place. Returns (homestead_status, homestead_raw_value)."""
	raw_value = None if base_raw is None else str(base_raw)
	if property_class != "STANDARD_RESIDENTIAL":
		return NOT_APPLICABLE, raw_value
	if base_raw is None:
		return UNKNOWN, raw_value
	return (HOMESTEAD, raw_value) if base_raw > 0 else (NO_HOMESTEAD, raw_value)


# ============================================================================
# Field mapping — county-specific parsed-row dict -> canonical column dict.
# Keys on the right match assessor_parcels' real column names.
# ============================================================================


def _raw_payload_json(raw: dict) -> str:
	"""Serializes the original, unmapped source row for storage in
	raw_payload — NOT NULL on assessor_parcels per §5A's raw-retention
	requirement, so both row mappers compute this themselves rather than
	leaving it as a footgun for the caller to remember. default=str covers
	dBase's date objects and anything else json.dumps can't natively
	handle."""
	return json.dumps(raw, default=str)


class CanonicalParcel(TypedDict, total=False):
	assessor_parcel_id: str
	strap: Optional[str]
	parcel_address_raw: str
	parcel_address_normalized: str
	raw_payload: str
	parcel_city: Optional[str]
	parcel_zip: Optional[str]
	owner_name_on_roll: str
	owner_name_secondary: Optional[str]
	owner_mailing_address_1: Optional[str]
	owner_mailing_address_2: Optional[str]
	owner_mailing_city: Optional[str]
	owner_mailing_state: Optional[str]
	owner_mailing_state_raw: Optional[str]
	owner_mailing_zip: Optional[str]
	owner_mailing_country: Optional[str]
	property_use_code: Optional[str]
	property_use_raw: Optional[str]
	property_class: Optional[str]
	parcel_status_raw: Optional[str]
	unit_count: Optional[int]
	source_record_updated_at: object
	# Pinellas-only (RP_PROPERTY_INFO.ROLL_YEAR) — the exemptions join's
	# pinning target; see derive_pinellas_exemption_update's docstring.
	roll_year: Optional[int]
	# Only ever set by map_hillsborough_row — Hillsborough has no separate
	# exemptions file, so its one import owns these columns outright (see
	# importer.py's Column Ownership rule). map_pinellas_property_info_row
	# deliberately omits all three; RP_EXEMPTIONS owns them for Pinellas.
	homestead_status: str
	homestead_raw_value: Optional[str]
	exemption_excluded: bool


def _to_int(value: Optional[Union[str, int, float]]) -> Optional[int]:
	if value is None:
		return None
	try:
		return int(float(value))
	except (TypeError, ValueError):
		return None


def map_hillsborough_row(raw: dict) -> CanonicalParcel:
	"""raw is one dBase record dict, keyed by the source file's own field
	names (FOLIO, OWNER, DOR_C, ...) — see dbase.py's iter_dbf_records."""
	dor_c = raw.get("DOR_C")
	property_class = classify_property_use("hillsborough_fl", dor_c)
	owner_name = str(raw.get("OWNER") or "").strip()
	# Secondary safeguard (task doc §5A) — the use code is always primary;
	# this only catches a government owner the use code alone missed.
	if property_class is not None and looks_like_government_owner(owner_name):
		property_class = None
	state = normalize_state(raw.get("STATE"))
	homestead_status, homestead_raw_value = derive_hillsborough_homestead(property_class, _to_int(raw.get("BASE")))
	address_raw = str(raw.get("SITE_ADDR") or "").strip()
	return CanonicalParcel(
		assessor_parcel_id=str(raw.get("FOLIO") or "").strip(),
		strap=(str(raw.get("STRAP")).strip() or None) if raw.get("STRAP") else None,
		parcel_address_raw=address_raw,
		parcel_address_normalized=normalize_address(address_raw),
		raw_payload=_raw_payload_json(raw),
		parcel_city=raw.get("SITE_CITY"),
		parcel_zip=raw.get("SITE_ZIP"),
		owner_name_on_roll=owner_name,
		owner_name_secondary=raw.get("DBA"),
		owner_mailing_address_1=raw.get("ADDR_1"),
		owner_mailing_address_2=raw.get("ADDR_2"),
		owner_mailing_city=raw.get("CITY"),
		owner_mailing_state=state,
		owner_mailing_state_raw=raw.get("STATE"),
		owner_mailing_zip=raw.get("ZIP"),
		owner_mailing_country=raw.get("COUNTRY"),
		property_use_code=(str(dor_c).strip()[:2] if dor_c else None),
		property_use_raw=dor_c,
		property_class=property_class,
		parcel_status_raw=None,  # Hillsborough's file has no STATUS-equivalent column
		unit_count=_to_int(raw.get("tUNITS")),
		source_record_updated_at=raw.get("Edit_dt"),
		homestead_status=homestead_status,
		homestead_raw_value=homestead_raw_value,
		# No RP_EXEMPTIONS-equivalent file exists for Hillsborough, so its
		# own import must set this explicitly (never leaving it NULL /
		# "not yet evaluated", which is the Pinellas-only bootstrap state).
		exemption_excluded=False,
	)


def map_pinellas_property_info_row(raw: dict) -> CanonicalParcel:
	"""raw is one CSV row dict from RP_PROPERTY_INFO, keyed by the source
	file's own column names. Deliberately does NOT set any homestead_* or
	property_exemption_raw/exemption_excluded column — those are owned
	exclusively by map_pinellas_exemptions_row / the RP_EXEMPTIONS import
	(see importer.py's Column Ownership rule)."""
	land_use_cd = raw.get("LAND_USE_CD")
	property_use = raw.get("PROPERTY_USE")
	property_class = classify_property_use("pinellas_fl", property_use)
	owner_name = str(raw.get("OWNER1") or "").strip()
	# Secondary safeguard (task doc §5A) — the use code is always primary;
	# this only catches a government owner the use code alone missed.
	if property_class is not None and looks_like_government_owner(owner_name):
		property_class = None
	state = normalize_state(raw.get("MAILING_STATE"))
	status_raw = raw.get("STATUS")
	# "Delete in Progress" / "New parcel in progress" rows may carry partial
	# data (the county's own field description says so explicitly) — never
	# eligible regardless of what their use code says.
	if status_raw and status_raw.strip():
		property_class = None
	mailing_address_1 = raw.get("MAILING_ADDRESS_1")
	mailing_address_2 = raw.get("MAILING_ADDRESS_2")
	address_raw = str(raw.get("SITE_ADDRESS") or "").strip()
	return CanonicalParcel(
		assessor_parcel_id=str(raw.get("STRAP") or "").strip(),
		strap=str(raw.get("STRAP") or "").strip() or None,
		parcel_address_raw=address_raw,
		parcel_address_normalized=normalize_address(address_raw),
		raw_payload=_raw_payload_json(raw),
		parcel_city=raw.get("SITE_CITYZIP"),
		parcel_zip=None,  # SITE_CITYZIP is combined; no separate zip field in this file
		owner_name_on_roll=owner_name,
		owner_name_secondary=raw.get("OWNER2"),
		owner_mailing_address_1=mailing_address_1,
		owner_mailing_address_2=mailing_address_2,
		owner_mailing_city=raw.get("MAILING_CITY"),
		owner_mailing_state=state,
		owner_mailing_state_raw=raw.get("MAILING_STATE"),
		owner_mailing_zip=raw.get("MAILING_ZIP"),
		owner_mailing_country=None,  # MAILING_ADDRESS_4 "may contain an address line" — not trusted positionally
		property_use_code=(str(land_use_cd).strip()[:2] if land_use_cd else None),
		property_use_raw=property_use,
		property_class=property_class,
		parcel_status_raw=status_raw,
		unit_count=_to_int(raw.get("TOTAL_LIVING_UNITS")),
		source_record_updated_at=None,  # RP_PROPERTY_INFO has no per-row edit-date column
		roll_year=_to_int(raw.get("ROLL_YEAR")),
	)


class ExemptionUpdate(TypedDict):
	homestead_status: str
	homestead_raw_value: Optional[str]
	homestead_roll_year: Optional[int]
	homestead_next_year: Optional[str]
	homestead_use_pct: Optional[int]
	property_exemption_raw: Optional[str]
	exemption_excluded: bool
	homestead_previous: Optional[str]


def _parse_hx_use_pct(raw: Optional[str]) -> Optional[int]:
	if not raw:
		return None
	digits = str(raw).strip().rstrip("%")
	try:
		return int(float(digits))
	except ValueError:
		return None


def _hx_yn_to_status(hx_yn: Optional[str]) -> Optional[str]:
	if hx_yn is None:
		return None
	value = str(hx_yn).strip().upper()
	if value == "YES":
		return HOMESTEAD
	if value == "NO":
		return NO_HOMESTEAD
	return None


def derive_pinellas_exemption_update(
	current_year_row: Optional[dict],
	previous_year_row: Optional[dict],
	next_year_row: Optional[dict],
) -> ExemptionUpdate:
	"""Combines the (up to) three RP_EXEMPTIONS rows for one STRAP — the
	roll year, the year before, and the year after — into one canonical
	update. Caller (importer.py) is responsible for selecting these three
	rows per STRAP via SQL, pinned to RP_PROPERTY_INFO.ROLL_YEAR; NEVER via
	MAX(HX_YR), which would select the *projected* next-year row and report
	a forecast as current status (a real bug found in review — confirmed
	from real sample data showing a 2027 row carrying
	"Assuming no ownership changes before Jan. 1, 2027.").

	homestead_previous is FILE-DERIVED here (from previous_year_row) and
	always overwrites unconditionally — it is the authoritative source for
	Pinellas, not the upsert-time transition-detection CASE that
	Hillsborough uses (which has no year-history in its own file)."""
	if current_year_row is None:
		status = UNKNOWN
		raw_value = None
		roll_year = None
		use_pct = None
	else:
		status = _hx_yn_to_status(current_year_row.get("HX_YN")) or UNKNOWN
		raw_value = current_year_row.get("HX_STATUS")
		roll_year = _to_int(current_year_row.get("HX_YR"))
		use_pct = _parse_hx_use_pct(current_year_row.get("HX_USE"))

	previous_status = _hx_yn_to_status(previous_year_row.get("HX_YN")) if previous_year_row else None
	next_status = _hx_yn_to_status(next_year_row.get("HX_YN")) if next_year_row else None

	# Bug found in review: an earlier version took the first non-blank
	# PROPERTY_EXEMPTION across current/previous/next, in that priority
	# order — which meant a parcel that was government-owned LAST year but
	# sold to a private owner THIS year (current year's field correctly
	# blank) stayed wrongly, permanently excluded, because a stale prior-
	# year value was borrowed instead of trusting the current year's own
	# blank answer. The current year's row is the only one that should
	# ever answer "is this parcel excluded *now*" — previous/next only
	# fill in when the current roll year's row is missing entirely (a
	# real gap, not merely "this year says no exemption").
	if current_year_row is not None:
		exemption_text = current_year_row.get("PROPERTY_EXEMPTION")
	elif previous_year_row is not None:
		exemption_text = previous_year_row.get("PROPERTY_EXEMPTION")
	elif next_year_row is not None:
		exemption_text = next_year_row.get("PROPERTY_EXEMPTION")
	else:
		exemption_text = None
	exemption_excluded = classify_pinellas_exemption(exemption_text)

	return ExemptionUpdate(
		homestead_status=status,
		homestead_raw_value=raw_value,
		homestead_roll_year=roll_year,
		homestead_next_year=next_status,
		homestead_use_pct=use_pct,
		property_exemption_raw=exemption_text,
		exemption_excluded=exemption_excluded,
		homestead_previous=previous_status,
	)
