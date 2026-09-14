"""Streaming dBase III (.dbf) reader — Hillsborough's `PARCEL_SPREADSHEET.xls`
is, despite its name and file extension, a dBase III file, not Excel (verified
against the real download byte-for-byte, 2026-09-11 — see
docs/plans/2026-09-11-automated-county-assessor-data-sync.md's Finding 1). The
county's own `.xls` naming and Excel's native ability to open `.dbf` files are
both real and both irrelevant to what the file actually contains: `xlrd`,
`openpyxl`, and `pandas.read_excel()` all reject it outright.

No installed dependency reads dBase; `dbfread` exists but requires a seekable
file object, which would force landing the 500+ MB file on disk to parse
something whose records are fixed-width and trivially streamable without it.
dBase III is a frozen, fully-specified format, so a small `struct`-based
reader is the smaller total system here.

Deliberately strict: only field types C (character), N (numeric), and D
(date) are supported — the only three that occur in the real Hillsborough
file. Any other type (most commonly M, a memo field requiring a companion
.dbt file that government exports often omit) raises DbfFormatError rather
than being silently skipped or guessed at, per the source task doc's "do not
silently continue when the file's structure changes" rule.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import date
from typing import BinaryIO, Iterator, Optional, Tuple, Union

# dBase III (no memo). A different version byte could mean a different
# field-descriptor layout or a memo-file dependency this reader doesn't
# support — reject outright rather than guess.
_VERSION_DBASE_III = 0x03

_DELETED_FLAG = 0x2A  # '*' — record is logically deleted, must be skipped
_TERMINATOR = 0x0D  # ends the field-descriptor array

# Confirmed against the real Hillsborough file's 47 fields (2026-09-11) —
# only these three types occur. See module docstring.
_SUPPORTED_FIELD_TYPES = frozenset("CND")


class DbfFormatError(ValueError):
	"""The file's structure doesn't match what this reader supports — an
	unexpected version byte, an unsupported field type, or a header shorter
	than it claims to be. Never guessed past; the caller (importer.py) is
	expected to treat this as a failed import, not a partial one."""


@dataclass(frozen=True)
class DbfField:
	name: str
	field_type: str  # 'C' | 'N' | 'D'
	length: int
	decimal_count: int


@dataclass(frozen=True)
class DbfHeader:
	version: int
	last_update: date
	n_records: int
	header_length: int
	record_length: int
	fields: Tuple[DbfField, ...]


def read_exact(stream: BinaryIO, n: int) -> bytes:
	"""Read exactly `n` bytes from `stream`, looping over short reads. A raw
	HTTP/socket stream (e.g. requests' `Response.raw`) may return fewer
	bytes than requested on a single `.read()` call, unlike a real file —
	callers that assumed otherwise would silently misparse the header or a
	record. Returns fewer than `n` bytes only at genuine EOF."""
	chunks = []
	remaining = n
	while remaining > 0:
		chunk = stream.read(remaining)
		if not chunk:
			break
		chunks.append(chunk)
		remaining -= len(chunk)
	return b"".join(chunks)


def _parse_fixed_header(buf: bytes) -> Tuple[int, date, int, int, int]:
	if len(buf) < 32:
		raise DbfFormatError(f"need at least 32 header bytes, got {len(buf)}")
	version, yy, mm, dd, n_records, header_length, record_length = struct.unpack("<BBBBIHH", buf[:12])
	if version != _VERSION_DBASE_III:
		raise DbfFormatError(
			f"unsupported dBase version byte 0x{version:02x} (only dBase III / 0x03 is supported)"
		)
	try:
		last_update = date(1900 + yy, mm, dd)
	except ValueError as exc:
		raise DbfFormatError(f"invalid header last-update date {1900 + yy}-{mm:02d}-{dd:02d}") from exc
	return version, last_update, n_records, header_length, record_length


def read_dbf_header(buf: bytes) -> DbfHeader:
	"""Parse a full dBase III header from `buf`. `buf` must already contain
	at least `header_length` bytes — read 32 bytes first to learn
	`header_length` from the raised DbfFormatError's message (or peek it
	via `_parse_fixed_header`), buffer that many bytes total, then call
	this."""
	version, last_update, n_records, header_length, record_length = _parse_fixed_header(buf)
	if len(buf) < header_length:
		raise DbfFormatError(f"need {header_length} header bytes (per the file's own header), got {len(buf)}")

	fields = []
	offset = 32
	while offset < header_length - 1:  # -1: leave room for the terminator byte
		descriptor = buf[offset : offset + 32]
		if not descriptor or descriptor[0] == _TERMINATOR:
			break
		name = descriptor[:11].split(b"\x00")[0].decode("ascii", errors="replace")
		field_type = chr(descriptor[11])
		length = descriptor[16]
		decimal_count = descriptor[17]
		if field_type not in _SUPPORTED_FIELD_TYPES:
			raise DbfFormatError(
				f"field {name!r} has unsupported type {field_type!r} — only C/N/D are "
				f"supported (a memo 'M' or other type changes how records must be "
				f"parsed, and this reader refuses to silently guess)"
			)
		fields.append(DbfField(name=name, field_type=field_type, length=length, decimal_count=decimal_count))
		offset += 32

	return DbfHeader(
		version=version,
		last_update=last_update,
		n_records=n_records,
		header_length=header_length,
		record_length=record_length,
		fields=tuple(fields),
	)


def read_dbf_header_from_stream(stream: BinaryIO) -> DbfHeader:
	"""Reads a dBase III header directly from a stream (e.g. an HTTP
	response's `.raw`), buffering exactly as many bytes as needed — 32 to
	learn `header_length`, then the remainder — never the whole 500+ MB
	body. `stream` is left positioned immediately after the header,
	ready for iter_dbf_records."""
	fixed = read_exact(stream, 32)
	_, _, _, header_length, _ = _parse_fixed_header(fixed)
	rest = read_exact(stream, header_length - 32)
	return read_dbf_header(fixed + rest)


def _decode_field(raw: bytes, field: DbfField) -> Optional[Union[str, int, float, date]]:
	text = raw.decode("cp1252", errors="replace").strip()
	if field.field_type == "C":
		return text or None
	if field.field_type == "D":
		# Fixed 8-char YYYYMMDD; blank or all-zero means unset.
		if not text or text == "0" * 8 or len(text) != 8:
			return None
		try:
			return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
		except ValueError:
			return None
	if field.field_type == "N":
		if not text:
			return None
		try:
			return float(text) if field.decimal_count else int(text)
		except ValueError:
			# A blank-padded numeric field (all spaces) or a stray non-numeric
			# value — treat as unset rather than raise; a single bad field on
			# one record must not abort the whole import (the importer's own
			# row-count floor is the real backstop against systemic corruption).
			return None
	raise DbfFormatError(f"unreachable: unsupported field type {field.field_type!r}")  # pragma: no cover


def iter_dbf_records(stream: BinaryIO, header: DbfHeader) -> Iterator[dict]:
	"""Yield one dict per active (non-deleted) record, reading `stream`
	sequentially at constant memory — never buffers more than one record at
	a time. `stream` must already be positioned immediately after
	`header.header_length` bytes."""
	for _ in range(header.n_records):
		record = read_exact(stream, header.record_length)
		if len(record) < header.record_length:
			# Genuine EOF (some writers omit the trailing padding around the
			# 0x1A EOF marker) or a truncated transfer — either way, nothing
			# more to parse. A truncated transfer is caught by the importer's
			# row-count floor, not here.
			break
		if record[0] == _DELETED_FLAG:
			continue
		row = {}
		offset = 1  # skip the deletion-flag byte
		for field in header.fields:
			row[field.name] = _decode_field(record[offset : offset + field.length], field)
			offset += field.length
		yield row
