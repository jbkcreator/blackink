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
"""
import re

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


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
