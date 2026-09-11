"""Unit tests for src/services/assessor_roll_loader.py (S-24)."""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from src.services.assessor_roll_loader import (
    UnrecognizedColumnsError,
    import_county_roll,
    parse_roll_file,
)


# ---------------------------------------------------------------------------
# parse_roll_file
# ---------------------------------------------------------------------------

def test_parses_canonical_headers():
    csv_bytes = (
        b"parcel_address,owner_name,parcel_id\n"
        b"123 Main St,John Smith,ABC123\n"
    )
    rows = parse_roll_file(csv_bytes)
    assert len(rows) == 1
    assert rows[0].parcel_address_raw == "123 Main St"
    assert rows[0].owner_name_on_roll == "John Smith"
    assert rows[0].assessor_parcel_id == "ABC123"
    assert rows[0].parcel_address_normalized == "123 MAIN ST"


def test_accepts_alias_headers_case_insensitively():
    csv_bytes = (
        b"SITUS_ADDRESS,OWN_NAME\n"
        b"456 Oak Ave,Jane Doe\n"
    )
    rows = parse_roll_file(csv_bytes)
    assert len(rows) == 1
    assert rows[0].parcel_address_raw == "456 Oak Ave"
    assert rows[0].owner_name_on_roll == "Jane Doe"
    assert rows[0].assessor_parcel_id is None  # no recognized parcel-id column


def test_parses_real_hillsborough_hcpa_column_schema():
    """FOLIO/OWNER/SITE_ADDR is HCPA's actual bulk parcel export schema
    (confirmed against jbkcreator/Forced-action-'s column_mapper.py, which
    has previously downloaded and loaded this exact county's real data) —
    not a guessed alias."""
    csv_bytes = (
        b"FOLIO,OWNER,SITE_ADDR,SITE_CITY,SITE_ZIP,TYPE\n"
        b"0123456789,SMITH JOHN,123 MAIN ST,TAMPA,33601,01\n"
    )
    rows = parse_roll_file(csv_bytes)
    assert len(rows) == 1
    assert rows[0].assessor_parcel_id == "0123456789"
    assert rows[0].owner_name_on_roll == "SMITH JOHN"
    assert rows[0].parcel_address_raw == "123 MAIN ST"


def test_missing_required_column_raises_unrecognized_columns_error():
    csv_bytes = b"some_other_column,owner_name\nvalue,Jane Doe\n"
    with pytest.raises(UnrecognizedColumnsError, match="address"):
        parse_roll_file(csv_bytes)


def test_no_header_row_raises():
    with pytest.raises(UnrecognizedColumnsError):
        parse_roll_file(b"")


def test_row_missing_address_or_owner_is_skipped_not_crashed():
    csv_bytes = (
        b"parcel_address,owner_name\n"
        b"123 Main St,John Smith\n"
        b",Missing Address Owner\n"
        b"456 Oak Ave,\n"
    )
    rows = parse_roll_file(csv_bytes)
    assert len(rows) == 1
    assert rows[0].parcel_address_raw == "123 Main St"


def test_tab_delimited_file_is_sniffed():
    csv_bytes = b"parcel_address\towner_name\n123 Main St\tJohn Smith\n"
    rows = parse_roll_file(csv_bytes)
    assert len(rows) == 1


def test_raw_payload_preserves_original_row():
    csv_bytes = b"parcel_address,owner_name,extra_col\n123 Main St,John Smith,extra_value\n"
    rows = parse_roll_file(csv_bytes)
    assert rows[0].raw_payload["extra_col"] == "extra_value"


# ---------------------------------------------------------------------------
# import_county_roll
# ---------------------------------------------------------------------------

def _mock_session(last_sha256=None):
    session = MagicMock()
    session.execute.return_value.first.return_value = (last_sha256,) if last_sha256 else None
    return session


def test_missing_file_bytes_returns_missing_status():
    session = _mock_session()
    result = import_county_roll(session, "hillsborough_fl", None)
    assert result.status == "MISSING"
    session.execute.assert_not_called()


def test_unchanged_hash_returns_unchanged_without_reimporting():
    csv_bytes = b"parcel_address,owner_name\n123 Main St,John Smith\n"
    existing_hash = hashlib.sha256(csv_bytes).hexdigest()
    session = _mock_session(last_sha256=existing_hash)

    result = import_county_roll(session, "hillsborough_fl", csv_bytes)

    assert result.status == "UNCHANGED"
    # Only the lookup SELECT should have run -- no DELETE/INSERT.
    calls = [str(c[0][0]) for c in session.execute.call_args_list]
    assert not any("DELETE" in c or "INSERT INTO raw_assessor_parcels" in c for c in calls)


def test_changed_hash_triggers_delete_then_insert():
    csv_bytes = b"parcel_address,owner_name\n123 Main St,John Smith\n"
    session = _mock_session(last_sha256="different_hash_entirely")

    result = import_county_roll(session, "hillsborough_fl", csv_bytes)

    assert result.status == "SUCCESS"
    assert result.row_count == 1
    calls = [str(c[0][0]) for c in session.execute.call_args_list]
    delete_idx = next(i for i, c in enumerate(calls) if "DELETE FROM raw_assessor_parcels" in c)
    insert_idx = next(i for i, c in enumerate(calls) if "INSERT INTO raw_assessor_parcels" in c)
    assert delete_idx < insert_idx


def test_first_ever_import_with_no_prior_hash_succeeds():
    csv_bytes = b"parcel_address,owner_name\n123 Main St,John Smith\n"
    session = _mock_session(last_sha256=None)
    result = import_county_roll(session, "hillsborough_fl", csv_bytes)
    assert result.status == "SUCCESS"


def test_parse_failure_returns_failed_and_does_not_touch_existing_rows():
    bad_bytes = b"wrong_column_name\nsome_value\n"
    session = _mock_session(last_sha256="prior_good_hash")

    result = import_county_roll(session, "hillsborough_fl", bad_bytes)

    assert result.status == "FAILED"
    assert result.error is not None
    calls = [str(c[0][0]) for c in session.execute.call_args_list]
    assert not any("DELETE" in c for c in calls)


def test_file_with_zero_usable_rows_returns_failed():
    csv_bytes = b"parcel_address,owner_name\n,\n"  # header ok, only a blank row
    session = _mock_session(last_sha256="prior_hash")
    result = import_county_roll(session, "hillsborough_fl", csv_bytes)
    assert result.status == "FAILED"
