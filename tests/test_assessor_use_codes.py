"""Unit tests for the per-county use-code allowlist in
src/services/assessor/mapping.py — see
docs/plans/2026-09-11-automated-county-assessor-data-sync.md §2.4/§3.
"""
from src.services.assessor.mapping import classify_property_use


def test_cross_county_divergence_proves_the_lookup_is_county_scoped():
	"""The exact reason this must be a per-county dict, not a shared one:
	Hillsborough's '0100' means Single Family; Pinellas has no '0100' at
	all (it uses '0110' for the same concept). If the lookup ever collapsed
	into one global dict keyed by code alone, this would break silently."""
	assert classify_property_use("hillsborough_fl", "0100") == "STANDARD_RESIDENTIAL"
	assert classify_property_use("pinellas_fl", "0100") is None
	assert classify_property_use("pinellas_fl", "0110") == "STANDARD_RESIDENTIAL"
	assert classify_property_use("hillsborough_fl", "0110") is None


def test_opposite_eligibility_same_code_string_different_county():
	"""0111 is 'permit pending' (not a standing rental) in Hillsborough, but
	'Single Family Community Land Trust' (a real residential use, though
	excluded by a separate business decision) in Pinellas — same code
	string, genuinely different meaning per county."""
	assert classify_property_use("hillsborough_fl", "0111") is None


def test_pinellas_ground_lease_codes_excluded_by_explicit_policy_not_by_omission():
	# These ARE on the Pinellas allowlist as STANDARD_RESIDENTIAL internally,
	# but excluded via the separate business-decision denylist — verify the
	# public function still returns None (the denylist is applied).
	for code in ("0111", "0112", "0134", "0135"):
		assert classify_property_use("pinellas_fl", code) is None


def test_hillsborough_task_doc_examples():
	# The task doc's own three §5A examples.
	assert classify_property_use("hillsborough_fl", "8800") is None  # UNITED STATES / federal
	assert classify_property_use("hillsborough_fl", "8600") is None  # HILLSBOROUGH COUNTY
	assert classify_property_use("hillsborough_fl", "0000") is None  # vacant


def test_llc_owner_on_residential_code_is_never_excluded_for_being_an_llc():
	from src.services.assessor.mapping import looks_like_government_owner

	assert classify_property_use("hillsborough_fl", "0802") == "STANDARD_RESIDENTIAL"
	assert looks_like_government_owner("ABC RENTALS LLC") is False


def test_hillsborough_permit_pending_and_vacant_subcodes_excluded():
	for code in ("0111", "0112", "0192", "0193", "0396", "0397", "0398", "0399"):
		assert classify_property_use("hillsborough_fl", code) is None, code


def test_hillsborough_care_facilities_excluded():
	for code in ("0600", "0610", "0620", "0630", "0640", "0650", "0660", "0670", "0680"):
		assert classify_property_use("hillsborough_fl", code) is None, code


def test_hillsborough_mobile_home_park_included_migrant_and_rv_excluded():
	for code in ("2810", "2811", "2812", "2813", "2814"):
		assert classify_property_use("hillsborough_fl", code) == "MOBILE_HOME_PARK", code
	assert classify_property_use("hillsborough_fl", "2815") is None  # migrant housing
	assert classify_property_use("hillsborough_fl", "2820") is None  # RV park


def test_hillsborough_mixed_use_included():
	assert classify_property_use("hillsborough_fl", "1201") == "MIXED_USE_RESIDENTIAL"
	assert classify_property_use("hillsborough_fl", "1203") == "MIXED_USE_RESIDENTIAL"


def test_hillsborough_small_multifamily_core_target():
	assert classify_property_use("hillsborough_fl", "0802") == "STANDARD_RESIDENTIAL"  # "3-9 units rentals"


def test_pinellas_mobile_home_park_included_rv_excluded():
	for code in ("2814", "2816", "2817"):
		assert classify_property_use("pinellas_fl", code) == "MOBILE_HOME_PARK", code
	assert classify_property_use("pinellas_fl", "2815") is None  # Campground - RV park


def test_pinellas_mixed_use_included():
	assert classify_property_use("pinellas_fl", "1227") == "MIXED_USE_RESIDENTIAL"


def test_pinellas_small_multifamily_core_target():
	assert classify_property_use("pinellas_fl", "0820") == "STANDARD_RESIDENTIAL"  # Duplex-Triplex-Fourplex
	assert classify_property_use("pinellas_fl", "0822") == "STANDARD_RESIDENTIAL"  # Apartments 5-9 units
	assert classify_property_use("pinellas_fl", "0830") == "STANDARD_RESIDENTIAL"  # SF with Accessory Dwelling


def test_pinellas_vacant_common_area_timeshare_and_care_facilities_excluded():
	for code in ("0000", "0030", "0435", "0442", "0443", "0752", "7456", "7837", "9999"):
		assert classify_property_use("pinellas_fl", code) is None, code
	for code in ("0904", "0944", "0954", "0964", "0974"):
		assert classify_property_use("pinellas_fl", code) is None, code


def test_unrecognized_or_missing_code_fails_closed():
	assert classify_property_use("hillsborough_fl", "9999999") is None
	assert classify_property_use("hillsborough_fl", None) is None
	assert classify_property_use("hillsborough_fl", "") is None
	assert classify_property_use("pinellas_fl", "ZZZZ") is None


def test_unknown_county_raises_rather_than_silently_returning_none():
	import pytest

	with pytest.raises(ValueError, match="no use-code allowlist"):
		classify_property_use("orange_fl", "0100")


if __name__ == "__main__":
	import pytest

	raise SystemExit(pytest.main([__file__, "-v"]))
