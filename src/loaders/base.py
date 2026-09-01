"""Base ingest loader — normalization, dedup, and safe-insert primitives.

Adapted from Forced Action's src/loaders/base.py (BaseLoader): the
normalization-statics / savepoint-insert / duplicate-check shape is ported
directly; the property-matching cascade (find_property_cascade,
_classify_match, quarantine_unmatched — all keyed to parcels/deeds/owners)
is NOT ported, since Blackink's ingest domain is companies/contacts, not
properties. The quarantine-table wiring (AkrashProspectLoader,
raw_prospect_companies/raw_prospect_contacts) lands in Week 2 once those
models exist — see the Dev 1 plan's "Scope Item 1" section. This file is
the Week 1 slice: the reusable normalization/dedup/safe-insert primitives
any concrete loader will build on.
"""

import hashlib
import logging
import re
from abc import ABC
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Company-name suffixes stripped for fuzzy-dedup comparison — the
# company-name analog of FA's owner-name noise-phrase list (trust/probate
# phrases there; entity-type suffixes here).
_COMPANY_SUFFIX_RE = re.compile(
	r"\b(LLC|L\.L\.C\.|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|"
	r"LP|L\.P\.|PLLC|PA|P\.A\.)\.?\b",
	re.IGNORECASE,
)
_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


class BaseIngestLoader(ABC):
	"""Abstract base for all Blackink ingest loaders.

	Provides normalization utilities, a savepoint-wrapped insert
	(safe_add) so one bad row doesn't abort a whole batch, and a
	duplicate-existence check — the portable primitives from Forced
	Action's BaseLoader, re-targeted at companies/contacts instead of
	properties/owners.
	"""

	def __init__(self, session: Session):
		self.session = session

	# ── Normalization ────────────────────────────────────────────────────

	@staticmethod
	def normalize_company_name(name: Optional[str]) -> str:
		"""Standardize a company name for fuzzy-dedup comparison: strip LLC/
		Inc/Corp-style suffixes, punctuation, and collapse whitespace.
		Mirrors the two-phase strip shape of FA's normalize_owner_name
		(phrase/suffix stripping, then punctuation, then whitespace)."""
		if not name:
			return ""
		value = str(name).upper().strip()
		value = _COMPANY_SUFFIX_RE.sub("", value)
		value = _PUNCTUATION_RE.sub(" ", value)
		value = _WHITESPACE_RE.sub(" ", value).strip()
		return value

	@staticmethod
	def normalize_domain(domain_or_url: Optional[str]) -> str:
		"""Normalize a raw domain/URL to a bare apex-ish domain for hashing
		and comparison: lowercase, strip scheme, strip 'www.', strip any
		path/query/fragment, strip a trailing dot."""
		if not domain_or_url:
			return ""
		value = str(domain_or_url).strip().lower()
		if "//" not in value:
			value = "//" + value
		parsed = urlsplit(value)
		host = parsed.netloc or parsed.path
		host = host.split("/")[0].split(":")[0]
		if host.startswith("www."):
			host = host[4:]
		return host.rstrip(".")

	@staticmethod
	def compute_company_id(domain: str) -> str:
		"""Deterministic SHA-256 hex digest of the normalized domain — the
		global-dedup primary key for companies.company_id.

		*** Application-computed, never DB-generated. Never gen_random_uuid().
		*** See src/core/models.py's Company docstring: the blueprint's own
		raw SQL contradicts its own prose on this point, and a random UUID
		would silently defeat the entire dedup design.
		"""
		normalized = BaseIngestLoader.normalize_domain(domain)
		if not normalized:
			raise ValueError("compute_company_id() requires a non-empty domain")
		return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

	# ── Safe insert ──────────────────────────────────────────────────────

	def safe_add(self, record: Any) -> bool:
		"""Add a record using a SAVEPOINT so a DB constraint violation on one
		row does not abort the whole batch transaction. Returns True if
		staged successfully, False if rejected (logged at WARNING)."""
		try:
			with self.session.begin_nested():
				self.session.add(record)
				self.session.flush()
			return True
		except Exception as e:
			logger.warning("Skipped record — DB rejected it: %s", e)
			return False

	# ── Duplicate check ──────────────────────────────────────────────────

	def check_duplicate(self, model: Any, unique_fields: Dict[str, Any]) -> bool:
		"""Return True if a row matching unique_fields already exists.

		Unlike FA's county-scoped variant, Blackink's dedup is global by
		design (companies/contacts are a global deduplicated layer — see
		Company docstring) so no implicit tenant/county filter is added
		here; callers pass exactly the fields that should uniquely identify
		the row (e.g. {"domain": normalized_domain})."""
		existing = self.session.query(model).filter_by(**unique_fields).first()
		return existing is not None
