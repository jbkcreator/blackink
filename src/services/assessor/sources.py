"""Per-county metadata check + download for the assessor sync — the only
module in this package that does network I/O. Mechanics verified directly
against both live sites during planning (2026-09-11); see
docs/plans/2026-09-11-automated-county-assessor-data-sync.md's Finding 2/3.

No SSRF guard (unlike src/services/owner_visibility/signals/website.py) —
these two hosts are hardcoded, first-party government URLs, not
attacker-suppliable input. Timeouts, a redirect ban, and a byte cap are
still kept.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from config.settings import get_settings

_REQUEST_TIMEOUT = (10, 30)  # (connect, read) seconds — generous for a 500+ MB body

_HILLSBOROUGH_URL = "https://downloads.hcpafl.org/"
_HILLSBOROUGH_FILENAME = "PARCEL_SPREADSHEET.xls"  # named .xls; contents are dBase III — see dbase.py

_PINELLAS_URL = "https://www.pcpao.gov/tools-data/data-downloads/raw-database-files"
_PINELLAS_DOWNLOAD_URL = "https://www.pcpao.gov/dal/databasefile/downloadDatabaseFile"


class SourceStructureError(RuntimeError):
	"""The page's HTML no longer matches what this scraper expects — the
	row/link/postback-target it was looking for wasn't found. Maps to
	assessor_sync_state.failure_category = 'SITE_STRUCTURE_CHANGED'. Never
	silently proceed with a guessed value."""


class DownloadSizeExceededError(RuntimeError):
	"""A download exceeded ASSESSOR_MAX_DOWNLOAD_BYTES. Deliberately NOT a
	SourceStructureError subclass — found in review that reusing the same
	exception class for both made assessor_sync_state's dedicated
	'DOWNLOAD_SIZE_EXCEEDED' failure_category unreachable (everything
	collapsed into 'SITE_STRUCTURE_CHANGED'), which defeats the point of
	having a distinct category an operator can act on differently."""


@dataclass(frozen=True)
class SourceVersion:
	dataset_name: str
	published_at: datetime
	source_filename: str
	extra: dict = field(default_factory=dict)


def _headers() -> dict:
	settings = get_settings()
	return {"User-Agent": settings.assessor_http_user_agent}


def _parse_hillsborough_datetime(text_value: str) -> datetime:
	# e.g. "9/4/2026 7:10AM" — confirmed format against the live page.
	naive = datetime.strptime(text_value.strip(), "%m/%d/%Y %I:%M%p")
	return naive.replace(tzinfo=timezone.utc)


def check_hillsborough() -> SourceVersion:
	"""GET the Hillsborough downloads page and locate the
	PARCEL_SPREADSHEET.xls row. Returns its published date and the
	__EVENTTARGET needed to trigger the download — resolved from the
	matching row, NEVER a hardcoded postback index (the index is
	positional and shifts if HCPA adds/removes a file from the list;
	Finding 2's trap)."""
	response = requests.get(_HILLSBOROUGH_URL, headers=_headers(), timeout=_REQUEST_TIMEOUT)
	response.raise_for_status()
	soup = BeautifulSoup(response.text, "lxml")
	table = soup.find("table", id=re.compile(r"grdFiles"))
	if table is None:
		raise SourceStructureError("Hillsborough downloads page: could not find the file grid table")

	for row in table.find_all("tr"):
		link = row.find("a", href=re.compile(r"__doPostBack"))
		if link is None or _HILLSBOROUGH_FILENAME not in link.get_text():
			continue
		match = re.search(r"__doPostBack\('([^']+)'", str(link["href"]))
		if not match:
			raise SourceStructureError(f"Hillsborough: found the {_HILLSBOROUGH_FILENAME} row but no postback target")
		event_target = match.group(1)
		cells = row.find_all("td")
		if len(cells) < 3:
			raise SourceStructureError("Hillsborough: file row has fewer columns than expected (name/size/date)")
		date_text = cells[2].get_text(strip=True)
		return SourceVersion(
			dataset_name="PARCEL_SPREADSHEET",
			published_at=_parse_hillsborough_datetime(date_text),
			source_filename=_HILLSBOROUGH_FILENAME,
			extra={"event_target": event_target},
		)
	raise SourceStructureError(f"Hillsborough downloads page: no row found for {_HILLSBOROUGH_FILENAME!r}")


def open_hillsborough_download(version: SourceVersion) -> requests.Response:
	"""POSTs the ASP.NET postback to trigger the download and returns the
	streaming `requests.Response` — the caller reads `response.raw`
	directly into dbase.read_dbf_header/iter_dbf_records (never buffered
	into memory whole; the file is 500+ MB) and MUST close the response
	when done (`with open_hillsborough_download(v) as response: ...` or a
	try/finally)."""
	session = requests.Session()
	page = session.get(_HILLSBOROUGH_URL, headers=_headers(), timeout=_REQUEST_TIMEOUT)
	page.raise_for_status()
	soup = BeautifulSoup(page.text, "lxml")

	def _hidden(name: str) -> str:
		field_el = soup.find("input", attrs={"name": name})
		if field_el is None:
			raise SourceStructureError(f"Hillsborough: missing hidden field {name!r} — page structure changed")
		return str(field_el.get("value", ""))

	response = session.post(
		_HILLSBOROUGH_URL,
		headers=_headers(),
		data={
			"__EVENTTARGET": version.extra["event_target"],
			"__EVENTARGUMENT": "",
			"__VIEWSTATE": _hidden("__VIEWSTATE"),
			"__VIEWSTATEGENERATOR": _hidden("__VIEWSTATEGENERATOR"),
			"__EVENTVALIDATION": _hidden("__EVENTVALIDATION"),
		},
		stream=True,
		timeout=_REQUEST_TIMEOUT,
		allow_redirects=False,
	)
	response.raise_for_status()
	content_disposition = response.headers.get("Content-Disposition", "")
	if _HILLSBOROUGH_FILENAME not in content_disposition:
		response.close()
		raise SourceStructureError(
			f"Hillsborough: download response's Content-Disposition {content_disposition!r} "
			f"doesn't mention {_HILLSBOROUGH_FILENAME!r} — refusing to parse an unexpected response as the roll file"
		)
	return response


class _CappedStream:
	"""Wraps a raw response stream, enforcing a total-bytes-read cap —
	defense in depth for the Hillsborough download, found missing in
	review: `download_pinellas` enforces `assessor_max_download_bytes` but
	`open_hillsborough_download` (a raw `requests.Response`, streamed
	directly by the caller) had no equivalent cap at all. Exposes only
	`.read(n)`, all `dbase.read_exact`/`iter_dbf_records` need."""

	def __init__(self, raw, max_bytes: int):
		self._raw = raw
		self._max_bytes = max_bytes
		self._read_so_far = 0

	def read(self, n: int = -1) -> bytes:
		chunk = self._raw.read(n)
		self._read_so_far += len(chunk)
		if self._read_so_far > self._max_bytes:
			raise DownloadSizeExceededError(
				f"Hillsborough download exceeded the {self._max_bytes}-byte cap — aborted"
			)
		return chunk


def capped_hillsborough_stream(response: requests.Response) -> _CappedStream:
	"""The stream callers should actually read from — never `response.raw`
	directly (see `_CappedStream`)."""
	settings = get_settings()
	return _CappedStream(response.raw, settings.assessor_max_download_bytes)


_PINELLAS_DATE_RE = re.compile(r"([A-Za-z]{3}) (\d{2}), (\d{2}:\d{2}) (AM|PM)")
_MONTH_ABBREV = {
	"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
	"Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _parse_pinellas_datetime(text_value: str, *, now: Optional[datetime] = None) -> datetime:
	"""Pinellas dates carry no year (e.g. "Sep 04, 03:01 AM") — Finding 4.
	Assume the current year; if that lands more than ~30 days in the
	future (the December-viewed-in-January rollover), assume the prior
	year instead. `now` is injectable for testing the rollover without
	waiting for December."""
	match = _PINELLAS_DATE_RE.search(text_value.strip())
	if not match:
		raise SourceStructureError(f"Pinellas: unrecognized date format {text_value!r}")
	month_abbrev, day, time_part, meridiem = match.groups()
	month = _MONTH_ABBREV.get(month_abbrev)
	if month is None:
		raise SourceStructureError(f"Pinellas: unrecognized month abbreviation {month_abbrev!r}")
	now = now or datetime.now(timezone.utc)
	candidate = datetime.strptime(f"{now.year}-{month:02d}-{day} {time_part} {meridiem}", "%Y-%m-%d %I:%M %p")
	candidate = candidate.replace(tzinfo=timezone.utc)
	if candidate > now + timedelta(days=30):
		candidate = candidate.replace(year=candidate.year - 1)
	return candidate


def check_pinellas(dataset_name: str) -> SourceVersion:
	"""dataset_name: 'RP_PROPERTY_INFO' | 'RP_EXEMPTIONS'. Always reads the
	'csv' row's date — Finding 2: the same dataset's other formats (xlsx,
	json, xml) can be stale relative to csv, and csv is the only format
	this sync ever downloads."""
	response = requests.get(_PINELLAS_URL, headers=_headers(), timeout=_REQUEST_TIMEOUT)
	response.raise_for_status()
	soup = BeautifulSoup(response.text, "lxml")
	link = soup.find("i", attrs={"fname": dataset_name, "ftype": "csv"})
	if link is None:
		raise SourceStructureError(f"Pinellas: no CSV download link found for dataset {dataset_name!r}")
	date_span = link.find_next_sibling("span", class_="sp-updated-date")
	if date_span is None:
		raise SourceStructureError(f"Pinellas: found the {dataset_name!r} CSV link but no adjacent date")
	return SourceVersion(
		dataset_name=dataset_name,
		published_at=_parse_pinellas_datetime(date_span.get_text()),
		source_filename=f"{dataset_name}.csv",
	)


def download_pinellas(dataset_name: str, dest_dir: Path) -> Path:
	"""POSTs the token-free form download and streams the ZIP to
	`dest_dir` — zipfile needs a seekable file, so disk is the right call
	here (both files are small: ~93 MB / ~6.6 MB compressed). Returns the
	path to the downloaded ZIP; caller is responsible for deleting it."""
	settings = get_settings()
	dest_dir.mkdir(parents=True, exist_ok=True)
	dest_path = dest_dir / f"{dataset_name}.zip"
	response = requests.post(
		_PINELLAS_DOWNLOAD_URL,
		headers=_headers(),
		data={"hdn_tbl_name": dataset_name, "hdn_ftype": "csv"},
		stream=True,
		timeout=_REQUEST_TIMEOUT,
		allow_redirects=False,
	)
	response.raise_for_status()
	max_bytes = settings.assessor_max_download_bytes
	written = 0
	with open(dest_path, "wb") as fh:
		for chunk in response.iter_content(chunk_size=1024 * 1024):
			written += len(chunk)
			if written > max_bytes:
				response.close()
				dest_path.unlink(missing_ok=True)
				raise DownloadSizeExceededError(
					f"Pinellas {dataset_name}: download exceeded the {max_bytes}-byte cap — aborted"
				)
			fh.write(chunk)
	response.close()
	return dest_path
