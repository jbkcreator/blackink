"""Unit tests for src/services/assessor/dbase.py — synthetic in-memory .dbf
round-trip. No network, no DB. See
docs/plans/2026-09-11-automated-county-assessor-data-sync.md §3.
"""
import io
import struct
from datetime import date

import pytest

from src.services.assessor.dbase import (
	DbfFormatError,
	iter_dbf_records,
	read_dbf_header,
	read_dbf_header_from_stream,
	read_exact,
)

# Synthetic schema: NAME C(10), AGE N(3,0), AMT N(10,2), DOB D(8)
_FIELDS = [
	("NAME", "C", 10, 0),
	("AGE", "N", 3, 0),
	("AMT", "N", 10, 2),
	("DOB", "D", 8, 0),
]


def _field_descriptor(name: str, field_type: str, length: int, decimal_count: int) -> bytes:
	return (
		name.encode("ascii").ljust(11, b"\x00")
		+ field_type.encode("ascii")
		+ b"\x00" * 4
		+ bytes([length])
		+ bytes([decimal_count])
		+ b"\x00" * 14
	)


def _build_dbf(fields, records: list[dict]) -> bytes:
	"""records: list of dicts {field_name: value_or_None}, plus optional
	'_deleted': True to mark a record deleted."""
	header_length = 32 + 32 * len(fields) + 1
	record_length = 1 + sum(f[2] for f in fields)
	fixed_header = struct.pack(
		"<BBBBIHH", 0x03, 126, 9, 4, len(records), header_length, record_length
	) + b"\x00" * 20
	field_descriptors = b"".join(_field_descriptor(*f) for f in fields)
	header = fixed_header + field_descriptors + b"\x0d"

	body = b""
	for rec in records:
		flag = b"\x2a" if rec.get("_deleted") else b"\x20"
		row = flag
		for name, ftype, length, _decimals in fields:
			value = rec.get(name)
			if value is None:
				cell = b" " * length
			elif ftype == "C":
				cell = str(value).encode("cp1252").ljust(length)[:length]
			elif ftype == "D":
				cell = str(value).encode("ascii").rjust(length, b"0")[:length]
			elif ftype == "N":
				cell = str(value).encode("ascii").rjust(length)[:length]
			else:  # pragma: no cover
				raise AssertionError(ftype)
			row += cell
		assert len(row) == record_length
		body += row
	return header + body


def test_round_trip_basic_types():
	records = [
		{"NAME": "ACME LLC", "AGE": 5, "AMT": "123.45", "DOB": "20250101"},
		{"NAME": "SMITH", "AGE": 0, "AMT": "0.00", "DOB": "20260904"},
	]
	raw = _build_dbf(_FIELDS, records)
	header = read_dbf_header(raw)
	assert header.n_records == 2
	assert header.record_length == 1 + 10 + 3 + 10 + 8
	assert [f.name for f in header.fields] == ["NAME", "AGE", "AMT", "DOB"]

	stream = io.BytesIO(raw[header.header_length :])
	rows = list(iter_dbf_records(stream, header))
	assert len(rows) == 2
	assert rows[0]["NAME"] == "ACME LLC"
	assert rows[0]["AGE"] == 5
	assert rows[0]["AMT"] == pytest.approx(123.45)
	assert rows[0]["DOB"] == date(2025, 1, 1)
	assert rows[1]["DOB"] == date(2026, 9, 4)


def test_deleted_record_is_skipped():
	records = [
		{"NAME": "KEEP", "AGE": 1, "AMT": "1.00", "DOB": "20250101"},
		{"NAME": "GONE", "AGE": 2, "AMT": "2.00", "DOB": "20250101", "_deleted": True},
	]
	raw = _build_dbf(_FIELDS, records)
	header = read_dbf_header(raw)
	stream = io.BytesIO(raw[header.header_length :])
	rows = list(iter_dbf_records(stream, header))
	assert len(rows) == 1
	assert rows[0]["NAME"] == "KEEP"


def test_blank_numeric_and_char_and_date_become_none():
	records = [{"NAME": None, "AGE": None, "AMT": None, "DOB": None}]
	raw = _build_dbf(_FIELDS, records)
	header = read_dbf_header(raw)
	stream = io.BytesIO(raw[header.header_length :])
	rows = list(iter_dbf_records(stream, header))
	assert rows[0] == {"NAME": None, "AGE": None, "AMT": None, "DOB": None}


def test_header_last_update_parsed():
	raw = _build_dbf(_FIELDS, [])
	header = read_dbf_header(raw)
	assert header.last_update == date(2026, 9, 4)


def test_unsupported_field_type_raises():
	fields = [("MEMO", "M", 10, 0)]
	raw = _build_dbf(fields, [])
	with pytest.raises(DbfFormatError, match="unsupported type"):
		read_dbf_header(raw)


def test_wrong_version_byte_raises():
	raw = bytearray(_build_dbf(_FIELDS, []))
	raw[0] = 0x05  # not dBase III
	with pytest.raises(DbfFormatError, match="unsupported dBase version"):
		read_dbf_header(bytes(raw))


def test_read_dbf_header_requires_full_header_bytes():
	raw = _build_dbf(_FIELDS, [])
	with pytest.raises(DbfFormatError, match="need"):
		read_dbf_header(raw[:40])  # far short of header_length


def test_read_exact_handles_short_reads_from_a_socket_like_stream():
	"""Simulate a stream that only ever returns a few bytes per .read()
	call, as a real HTTP/socket stream may — read_exact must loop rather
	than assume one call satisfies the request."""

	class DrippingStream:
		def __init__(self, data: bytes):
			self._data = data
			self._pos = 0

		def read(self, n: int) -> bytes:
			chunk = self._data[self._pos : self._pos + min(3, n)]
			self._pos += len(chunk)
			return chunk

	stream = DrippingStream(b"0123456789")
	assert read_exact(stream, 10) == b"0123456789"
	assert read_exact(stream, 5) == b""  # EOF — nothing left


def test_read_dbf_header_from_stream_buffers_exactly_what_it_needs():
	records = [{"NAME": "A", "AGE": 1, "AMT": "1.00", "DOB": "20250101"}]
	raw = _build_dbf(_FIELDS, records)
	stream = io.BytesIO(raw)
	header = read_dbf_header_from_stream(stream)
	assert header.n_records == 1
	# stream must now be positioned right at the first record.
	rows = list(iter_dbf_records(stream, header))
	assert rows[0]["NAME"] == "A"


def test_truncated_records_stop_iteration_without_raising():
	records = [{"NAME": "A", "AGE": 1, "AMT": "1.00", "DOB": "20250101"}]
	raw = _build_dbf(_FIELDS, records)
	header = read_dbf_header(raw)
	# Simulate a transfer cut off mid-record.
	truncated_body = raw[header.header_length :][: header.record_length - 5]
	stream = io.BytesIO(truncated_body)
	rows = list(iter_dbf_records(stream, header))
	assert rows == []


if __name__ == "__main__":
	raise SystemExit(pytest.main([__file__, "-v"]))
