"""DBPR licence signal provider — checks Florida DBPR broker licence status.

Signal: dbpr_active_licence  4 pts

Reads a manually-downloaded CSV from data/dbpr/florida_brokers.csv.
The CSV is not committed to the repo (it's a public-record download, not
PII, but it's large and changes monthly). The sweep task logs a warning and
returns MISSING_DATA for all companies if the file is absent, so missing
the CSV degrades gracefully without crashing the run.

Name matching uses rapidfuzz token_sort_ratio (>= 85 threshold) against
the company_name field. This is intentionally conservative — a false
positive here would incorrectly award points to an unlicensed firm. The
threshold was chosen to handle "ABC Property Management LLC" vs
"ABC Property Management" while rejecting "ABC PM" vs "ABCD PM Corp".

Expected CSV columns (case-insensitive): name, license_number, status,
license_type. Any extra columns are ignored.

    data/dbpr/florida_brokers.csv  — download from
    https://www.myfloridalicense.com/DBPR/os/documents/DataDownload.zip
    (RE broker / RE broker associate export, unzip and rename)
"""

import csv
import logging
import pathlib
from typing import Any

from rapidfuzz import fuzz

from src.services.owner_visibility.signals.base import (
    SignalResult,
    SignalProvider,
    SCORED,
    MISSING_DATA,
)

logger = logging.getLogger(__name__)

_DBPR_CSV_PATH = pathlib.Path("data/dbpr/florida_brokers.csv")
_MATCH_THRESHOLD = 85  # rapidfuzz token_sort_ratio
_ACTIVE_STATUSES = {"current active", "current,active", "active"}


def _load_csv(path: pathlib.Path) -> list[dict[str, str]]:
    """Load and normalise CSV rows. Returns empty list if file is absent."""
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            # Normalise keys to lowercase so column name casing doesn't matter.
            rows.append({k.strip().lower(): v.strip() for k, v in row.items()})
    return rows


# Module-level cache so repeated calls within the same process pay the I/O
# cost once. The sweep task runs once per invocation; stale cache is fine.
_csv_cache: list[dict[str, str]] | None = None


def _get_records() -> list[dict[str, str]]:
    global _csv_cache
    if _csv_cache is None:
        _csv_cache = _load_csv(_DBPR_CSV_PATH)
    return _csv_cache


def _is_active(row: dict[str, str]) -> bool:
    status = row.get("status", "").lower().replace(" ", "")
    return status in {s.replace(" ", "").replace(",", "") for s in _ACTIVE_STATUSES}


def _find_match(company_name: str, records: list[dict[str, str]]) -> dict[str, str] | None:
    """Return the best-matching record above threshold, or None."""
    best_score = 0
    best_row: dict[str, str] | None = None
    name_lower = company_name.lower()
    for row in records:
        candidate = row.get("name", "").lower()
        if not candidate:
            continue
        score = fuzz.token_sort_ratio(name_lower, candidate)
        if score > best_score:
            best_score = score
            best_row = row
    if best_score >= _MATCH_THRESHOLD:
        return best_row
    return None


class DbprLicenceSignalProvider(SignalProvider):
    """Awards 4 pts when the firm has a current-active FL DBPR broker licence."""

    def collect(self, company: dict[str, Any]) -> list[SignalResult]:
        company_name: str = company.get("company_name", "")
        if not company_name:
            return [SignalResult("dbpr_active_licence", 0, 4, MISSING_DATA, "no company_name")]

        records = _get_records()
        if not records:
            logger.warning(
                "dbpr_licence: %s not found — DBPR signals will be MISSING_DATA for all companies. "
                "Download from MyFloridaLicense.com and place at %s",
                _DBPR_CSV_PATH,
                _DBPR_CSV_PATH,
            )
            return [
                SignalResult(
                    "dbpr_active_licence", 0, 4, MISSING_DATA,
                    f"DBPR CSV not found at {_DBPR_CSV_PATH}",
                )
            ]

        match = _find_match(company_name, records)
        if match is None:
            return [
                SignalResult(
                    "dbpr_active_licence", 0, 4, SCORED,
                    f"no DBPR record matched {company_name!r} above threshold {_MATCH_THRESHOLD}",
                )
            ]

        if _is_active(match):
            licence_num = match.get("license_number", "unknown")
            return [
                SignalResult(
                    "dbpr_active_licence", 4, 4, SCORED,
                    f"active licence {licence_num} matched to {match.get('name', '')!r}",
                )
            ]

        return [
            SignalResult(
                "dbpr_active_licence", 0, 4, SCORED,
                f"matched {match.get('name', '')!r} but status={match.get('status', 'unknown')!r}",
            )
        ]
