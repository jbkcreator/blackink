"""Unit tests for the pure date-parsing logic in
src/services/assessor/sources.py. No network — the HTML-scraping functions
(check_hillsborough/check_pinellas/open_hillsborough_download/
download_pinellas) were verified directly against the live sites during
implementation (2026-09-11) rather than against saved fixtures; see
docs/plans/2026-09-11-automated-county-assessor-data-sync.md's Findings
2-4 for what was confirmed and how.
"""
from datetime import datetime, timezone

import pytest

from src.services.assessor.sources import (
	DownloadSizeExceededError,
	SourceStructureError,
	_CappedStream,
	_parse_hillsborough_datetime,
	_parse_pinellas_datetime,
)


def test_parse_hillsborough_datetime():
	assert _parse_hillsborough_datetime("9/4/2026 7:10AM") == datetime(2026, 9, 4, 7, 10, tzinfo=timezone.utc)


def test_parse_pinellas_datetime_assumes_current_year():
	now = datetime(2026, 9, 11, tzinfo=timezone.utc)
	result = _parse_pinellas_datetime("Sep 04, 03:01 AM", now=now)
	assert result == datetime(2026, 9, 4, 3, 1, tzinfo=timezone.utc)


def test_parse_pinellas_datetime_december_january_rollover():
	"""Viewed in early January, a date that would land >30 days in the
	future must roll back to the prior year — the exact December/January
	wrinkle Finding 4 warns about."""
	now = datetime(2027, 1, 3, tzinfo=timezone.utc)
	result = _parse_pinellas_datetime("Dec 28, 11:45 PM", now=now)
	assert result.year == 2026


def test_parse_pinellas_datetime_near_future_within_window_stays_current_year():
	now = datetime(2026, 9, 11, tzinfo=timezone.utc)
	result = _parse_pinellas_datetime("Sep 20, 10:00 AM", now=now)
	assert result.year == 2026


def test_parse_pinellas_datetime_unrecognized_format_raises():
	with pytest.raises(SourceStructureError):
		_parse_pinellas_datetime("not a date")


class _FakeRaw:
	def __init__(self, data: bytes, chunk_size: int = 4):
		self._data = data
		self._pos = 0
		self._chunk_size = chunk_size

	def read(self, n: int = -1) -> bytes:
		size = self._chunk_size if n == -1 else min(n, self._chunk_size)
		chunk = self._data[self._pos : self._pos + size]
		self._pos += len(chunk)
		return chunk


def test_capped_stream_allows_reads_under_the_cap():
	stream = _CappedStream(_FakeRaw(b"0123456789", chunk_size=100), max_bytes=100)
	assert stream.read(10) == b"0123456789"


def test_capped_stream_raises_once_total_reads_exceed_the_cap():
	"""Regression test: open_hillsborough_download had no byte cap at all
	(unlike download_pinellas) — found in review."""
	stream = _CappedStream(_FakeRaw(b"0" * 100, chunk_size=10), max_bytes=25)
	with pytest.raises(DownloadSizeExceededError):
		for _ in range(10):
			chunk = stream.read(10)
			if not chunk:
				break


if __name__ == "__main__":
	raise SystemExit(pytest.main([__file__, "-v"]))
