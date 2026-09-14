"""Shared property-address normalization — the exact-match key both the
county assessor sync (src/tasks/assessor_sync.py) and Win-Back ingest
(src/services/winback_ingest.py) must agree on.

Moved out of winback_ingest.py (2026-09-11) rather than duplicated: if the
writer (assessor sync) and the reader (Win-Back's assessor lookup) ever
normalized an address differently, every lookup would silently return
None — a total, invisible failure with no error anywhere. One
implementation used by both sides is the only way to make that
structurally impossible. See
docs/plans/2026-09-11-automated-county-assessor-data-sync.md §2.1.

Behaviour preserved byte-for-byte from the original winback_ingest.py
function it replaces.

`build_address_match_key` and its two parsers below (added 2026-09-14) fix
a real matching bug found by inspecting production data: street-only
matching (the original design) collides whenever the same street name
exists twice within one county in different cities/zips — real, not
hypothetical, since street names like "Main St"/"Park Ave" repeat across a
county's many cities. The match key now includes city+zip on both sides:
mapping.py (the writer, using each county's own real site/parcel address
fields — SITE_ADDR/SITE_CITY/SITE_ZIP for Hillsborough,
SITE_ADDRESS/SITE_CITYZIP for Pinellas — never the OWNER'S mailing address,
which can be anywhere and has nothing to do with where the parcel is) and
winback_ingest.py (the reader, parsing the client's own freeform CSV
address string). State is deliberately excluded from the key itself: every
county this platform covers is Florida-only, so it adds no discriminating
power over city+zip alone, and keeping the formula to street+city+zip
avoids a state-abbreviation-casing mismatch becoming a stray fourth source
of divergence between the two sides.
"""
import re

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# "STREET, CITY, ST[ ZIP]" — the one client-CSV convention confirmed in
# this repo (see owner_enrichment.py's own _split_address, which this
# supersedes as the shared implementation). Anything else (no commas,
# city/state space-separated, a bare street with no city at all) is left
# unparsed — never a guess; the caller decides what "unparseable" means for
# its own use case (Tracerfy submission skips the row; Win-Back matching
# returns None rather than risk a wrong street-only collision).
_STREET_CITY_STATE_ZIP_RE = re.compile(
	r"^(?P<street>.+?),\s*(?P<city>[^,]+?),\s*(?P<state>[A-Za-z]{2})\b\s*(?P<zip>\d{5}(?:-\d{4})?)?\s*$"
)

# County source files' own combined "CITY, ST ZIP" field (Pinellas's real
# SITE_CITYZIP — confirmed against live production data, e.g.
# "PALM HARBOR, FL 34683"). Same shape as the tail of the regex above,
# without a leading street component.
_CITY_STATE_ZIP_RE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Za-z]{2})\b\s*(?P<zip>\d{5}(?:-\d{4})?)?\s*$")


def normalize_address(raw: str) -> str:
	"""Standardize a property address for exact-match lookup: uppercase,
	strip punctuation, collapse whitespace. Deliberately simple (no
	USPS-style unit/suffix expansion) — a future geocoding pass can replace
	this if address variance turns out to matter in practice; not built
	speculatively here."""
	if not raw:
		return ""
	value = str(raw).upper().strip()
	value = _PUNCTUATION_RE.sub(" ", value)
	value = _WHITESPACE_RE.sub(" ", value).strip()
	return value


def split_street_city_state_zip(raw_address):
	"""Parses a freeform "STREET, CITY, ST[ ZIP]" string into
	(street, city, state, zip5_or_None). Returns None (never a guess) when
	the format doesn't match — a bare street with no city, or any other
	convention, since matching on a partial key would risk exactly the
	street-name-collision bug this module exists to close."""
	m = _STREET_CITY_STATE_ZIP_RE.match((raw_address or "").strip())
	if not m:
		return None
	street = m.group("street").strip()
	city = m.group("city").strip()
	state = m.group("state").strip().upper()
	zip_code = (m.group("zip") or "")[:5] or None
	if not street or not city or len(state) != 2:
		return None
	return street, city, state, zip_code


def split_city_state_zip(raw):
	"""Parses a combined "CITY, ST ZIP" field (Pinellas's real SITE_CITYZIP
	column) into (city, state, zip5_or_None). Returns None when it doesn't
	match this exact convention — the caller falls back to using the raw
	string as-is for parcel_city rather than silently blanking it, since an
	unparsed value still contributes real (if noisier) matching signal."""
	m = _CITY_STATE_ZIP_RE.match((raw or "").strip())
	if not m:
		return None
	city = m.group("city").strip()
	state = m.group("state").strip().upper()
	zip_code = (m.group("zip") or "")[:5] or None
	if not city or len(state) != 2:
		return None
	return city, state, zip_code


def build_address_match_key(street, city, zip_code) -> str:
	"""The single match-key formula the assessor sync (writer) and Win-Back
	ingest (reader) must build identically. Concatenates street+city+zip
	(zip truncated to its first 5 digits, so a zip+4 on one side never
	mismatches a plain 5-digit zip on the other) and runs the result
	through normalize_address — divergence here would silently make every
	lookup miss or, worse, collide across two different streets that
	happen to share a name, exactly the bug this module exists to close."""
	zip5 = (str(zip_code)[:5] if zip_code else "")
	return normalize_address(f"{street or ''} {city or ''} {zip5}")
