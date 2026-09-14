"""Unit tests for src/services/address_normalize.py's address-parsing and
match-key helpers — the writer (assessor mapping.py) and reader
(winback_ingest.py) both build their match key through
build_address_match_key, so a divergence here would silently break
Win-Back's assessor lookup for every real address. See mapping.py's own
tests (test_assessor_mapping.py) for the county-specific wiring.
"""
from src.services.address_normalize import (
	build_address_match_key,
	normalize_address,
	split_city_state_zip,
	split_street_city_state_zip,
)


def test_split_street_city_state_zip_with_zip():
	assert split_street_city_state_zip("123 Main St, Tampa, FL 33607") == ("123 Main St", "Tampa", "FL", "33607")


def test_split_street_city_state_zip_without_zip():
	assert split_street_city_state_zip("123 Main St, Tampa, FL") == ("123 Main St", "Tampa", "FL", None)


def test_split_street_city_state_zip_truncates_zip_plus_4():
	assert split_street_city_state_zip("123 Main St, Tampa, FL 33607-1234") == ("123 Main St", "Tampa", "FL", "33607")


def test_split_street_city_state_zip_no_commas_returns_none():
	"""A bare street with no city is left unparsed, never a guess — this
	is the case that used to silently match on street alone."""
	assert split_street_city_state_zip("123 Main St") is None


def test_split_street_city_state_zip_blank_returns_none():
	assert split_street_city_state_zip("") is None
	assert split_street_city_state_zip(None) is None


def test_split_city_state_zip_with_zip():
	assert split_city_state_zip("Palm Harbor, FL 34683") == ("Palm Harbor", "FL", "34683")


def test_split_city_state_zip_unrecognized_format_returns_none():
	assert split_city_state_zip("not a real format") is None


def test_build_address_match_key_concatenates_street_city_state_zip():
	assert build_address_match_key("10 Main St", "Tampa", "33602") == normalize_address("10 Main St Tampa FL 33602")


def test_build_address_match_key_state_defaults_to_fl_when_omitted():
	assert build_address_match_key("10 Main St", "Tampa", "33602") == build_address_match_key("10 Main St", "Tampa", "33602", "FL")


def test_build_address_match_key_accepts_an_explicit_state():
	assert build_address_match_key("10 Main St", "Tampa", "33602", "GA") == normalize_address("10 Main St Tampa GA 33602")


def test_build_address_match_key_same_street_different_city_differs():
	"""The whole point of this key: two parcels sharing a street name in
	different cities must never produce the same key."""
	key_a = build_address_match_key("10 Main St", "Tampa", "33602")
	key_b = build_address_match_key("10 Main St", "Brandon", "33511")
	assert key_a != key_b


def test_build_address_match_key_truncates_zip_plus_4_to_match_a_plain_5digit_zip():
	"""A zip+4 on one side (e.g. the county source file) must still match a
	plain 5-digit zip on the other (a client's CSV) — truncating both to
	their first 5 digits is what makes that possible."""
	assert build_address_match_key("10 Main St", "Tampa", "33602-9999") == build_address_match_key("10 Main St", "Tampa", "33602")


def test_build_address_match_key_handles_missing_city_or_zip_without_raising():
	assert build_address_match_key("10 Main St", None, None) == normalize_address("10 Main St FL")
	assert build_address_match_key("10 Main St", "", "") == normalize_address("10 Main St FL")


if __name__ == "__main__":
	import pytest

	raise SystemExit(pytest.main([__file__, "-v"]))
