"""DBPR licence signal provider — checks Florida DBPR broker licence status.

Signal: dbpr_active_licence  4 pts

Reads data/dbpr/florida_brokers.csv. If the file is absent, _ensure_csv()
downloads it automatically from the FL DBPR public extract URL — no manual
step required. The file is re-used across calls (module-level cache) and
should be refreshed monthly (delete the file to trigger a fresh download).

Download source (public record, no auth required):
  https://www2.myfloridalicense.com/sto/file_download/extracts/REALESTATE2501LICENSE_1.csv

Name matching uses rapidfuzz token_sort_ratio (>= 85 threshold) against
the company_name field. This is intentionally conservative — a false
positive here would incorrectly award points to an unlicensed firm. The
threshold was chosen to handle "ABC Property Management LLC" vs
"ABC Property Management" while rejecting "ABC PM" vs "ABCD PM Corp".

Expected CSV columns (case-insensitive): name, license_number, status,
license_type. Any extra columns are ignored.
"""

import csv
import logging
import pathlib
from typing import Any

import requests

from rapidfuzz import fuzz

from src.services.owner_visibility.signals.base import (
    SignalResult,
    SignalProvider,
    SCORED,
    MISSING_DATA,
)

logger = logging.getLogger(__name__)

_DBPR_CSV_PATH = pathlib.Path("data/dbpr/florida_brokers.csv")
_DBPR_DOWNLOAD_URL = (
    "https://www2.myfloridalicense.com/sto/file_download/extracts/REALESTATE2501LICENSE_1.csv"
)
_MATCH_THRESHOLD = 82  # token_sort_ratio — handles "LLC" vs "L L C" and minor word variants
_ACTIVE_STATUSES = {"current active"}  # cols[12]+" "+cols[13] lowercased


def _ensure_csv() -> bool:
    """Download the DBPR CSV if it doesn't exist locally. Returns True on success."""
    if _DBPR_CSV_PATH.exists():
        return True
    _DBPR_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.info("dbpr_licence: CSV not found — downloading from DBPR extract URL")
    try:
        from config.settings import get_settings
        s = get_settings()
        proxies = None
        if s.oxylabs_username and s.oxylabs_password:
            pwd = s.oxylabs_password.get_secret_value()
            proxy_url = f"http://{s.oxylabs_username}:{pwd}@pr.oxylabs.io:7777"
            proxies = {"http": proxy_url, "https": proxy_url}
            logger.info("dbpr_licence: using Oxylabs residential proxy for download")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www2.myfloridalicense.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = requests.get(
            _DBPR_DOWNLOAD_URL, timeout=60, stream=True, headers=headers, proxies=proxies
        )
        resp.raise_for_status()
        with _DBPR_CSV_PATH.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                fh.write(chunk)
        size_mb = _DBPR_CSV_PATH.stat().st_size / (1024 * 1024)
        logger.info("dbpr_licence: downloaded %.1f MB to %s", size_mb, _DBPR_CSV_PATH)
        return True
    except Exception as exc:
        logger.warning("dbpr_licence: auto-download failed (%s) — DBPR signals will be MISSING_DATA", exc)
        # Remove partial file so next call retries cleanly.
        _DBPR_CSV_PATH.unlink(missing_ok=True)
        return False


def _load_csv(path: pathlib.Path) -> list[dict[str, str]]:
    """Load and normalise CSV rows.

    The FL DBPR real estate extract has no header row. Column positions:
      1  — individual licensee name (LAST, FIRST)
      11 — licence number (digits)
      12 — status word 1 ("Current" / "Invol Inactive" / etc.)
      13 — status word 2 ("Active" / "Inactive")
      17 — full licence code (e.g. "BK468849")
      19 — employer / business name (blank for solo practitioners)
    """
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for cols in csv.reader(fh):
            if len(cols) < 20:
                continue
            business = cols[19].strip()
            if not business:
                continue  # skip solo practitioners — no business name to match
            rows.append({
                "name":           business,
                "licensee_name":  cols[1].strip(),
                "license_number": cols[17].strip(),
                "status":         f"{cols[12].strip()} {cols[13].strip()}".lower(),
            })
    return rows


# Module-level cache so repeated calls within the same process pay the I/O
# cost once. The sweep task runs once per invocation; stale cache is fine.
_csv_cache: list[dict[str, str]] | None = None


def _get_records() -> list[dict[str, str]]:
    global _csv_cache
    if _csv_cache is None:
        if _ensure_csv():
            _csv_cache = _load_csv(_DBPR_CSV_PATH)
        else:
            _csv_cache = []
    return _csv_cache


def _is_active(row: dict[str, str]) -> bool:
    return row.get("status", "").strip().lower() in _ACTIVE_STATUSES


def _first_word_compatible(query: str, candidate: str) -> bool:
    """Guard against fuzzy ratio matching words that start differently.

    Requires the first token of query to be a prefix of the first token of
    candidate, or vice versa. This kills coincidental high scores where the
    distinctive first word differs (e.g. 'Bay' vs 'Beam') while still
    allowing abbreviation variants ('Rent' → 'Rental', 'Mgmt' → 'Management').
    """
    q0 = query.lower().split()[0] if query.split() else ""
    c0 = candidate.lower().split()[0] if candidate.split() else ""
    return c0.startswith(q0) or q0.startswith(c0)


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
        if score > best_score and _first_word_compatible(name_lower, candidate):
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
            return [
                SignalResult(
                    "dbpr_active_licence", 0, 4, MISSING_DATA,
                    "DBPR CSV unavailable (download failed or empty)",
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
