"""Unit tests for src/services/assessor/mapping.py's field mapping, state
normalization, exemption classification, and homestead derivation. See
docs/plans/2026-09-11-automated-county-assessor-data-sync.md §2.4/§3.
"""
from src.services.assessor.mapping import (
	HOMESTEAD,
	NO_HOMESTEAD,
	NOT_APPLICABLE,
	UNKNOWN,
	classify_pinellas_exemption,
	derive_hillsborough_homestead,
	derive_pinellas_exemption_update,
	map_hillsborough_row,
	map_pinellas_property_info_row,
	normalize_state,
)


# ── State normalization ──────────────────────────────────────────────────


def test_normalize_state_full_name_and_abbrev():
	assert normalize_state("FLORIDA") == "FL"
	assert normalize_state("florida") == "FL"
	assert normalize_state("FL") == "FL"
	assert normalize_state("New York") == "NY"


def test_normalize_state_unrecognized_returns_none_never_a_guess():
	assert normalize_state("NOT A STATE") is None
	assert normalize_state("") is None
	assert normalize_state(None) is None


# ── Hillsborough field mapping (the three task-doc §5A example rows) ────


def test_map_hillsborough_row_federal_owner_excluded():
	row = {
		"FOLIO": "0000010000", "DOR_C": "8800", "OWNER": "UNITED STATES",
		"SITE_ADDR": "0 EGMONT KEY", "SITE_CITY": "", "STATE": "FLORIDA",
		"tUNITS": "0.00",
	}
	mapped = map_hillsborough_row(row)
	assert mapped["assessor_parcel_id"] == "0000010000"
	assert mapped["property_class"] is None
	assert mapped["owner_mailing_state"] == "FL"


def test_map_hillsborough_row_county_owner_excluded():
	row = {"FOLIO": "0000050000", "DOR_C": "8600", "OWNER": "HILLSBOROUGH COUNTY", "STATE": "FL"}
	assert map_hillsborough_row(row)["property_class"] is None


def test_map_hillsborough_row_vacant_excluded():
	row = {"FOLIO": "0000080000", "DOR_C": "0000", "OWNER": "PATRICIA ANNE SEVIGNY TRUSTEE"}
	assert map_hillsborough_row(row)["property_class"] is None


def test_map_hillsborough_row_llc_on_residential_kept():
	row = {"FOLIO": "1234567890", "DOR_C": "0802", "OWNER": "ABC RENTALS LLC", "STATE": "FL", "tUNITS": "6"}
	mapped = map_hillsborough_row(row)
	assert mapped["property_class"] == "STANDARD_RESIDENTIAL"
	assert mapped["unit_count"] == 6


def test_map_hillsborough_row_government_owner_safeguard_overrides_residential_code():
	"""Secondary safeguard: even if a use code were somehow residential,
	a clearly government-named owner still gets excluded."""
	row = {"FOLIO": "999", "DOR_C": "0100", "OWNER": "STATE OF FLORIDA"}
	assert map_hillsborough_row(row)["property_class"] is None


def test_map_hillsborough_property_use_code_is_2digit_prefix_of_dor_c():
	row = {"FOLIO": "1", "DOR_C": "0802", "OWNER": "X"}
	assert map_hillsborough_row(row)["property_use_code"] == "08"


def test_map_hillsborough_row_owns_homestead_and_exemption_excluded():
	"""No separate exemptions file exists for Hillsborough — its own
	mapper must set homestead_status/exemption_excluded outright, never
	leave them at the Pinellas-only 'not yet evaluated' NULL state."""
	row = {"FOLIO": "1", "DOR_C": "0100", "OWNER": "X", "BASE": "2015"}
	mapped = map_hillsborough_row(row)
	assert mapped["homestead_status"] == HOMESTEAD
	assert mapped["homestead_raw_value"] == "2015"
	assert mapped["exemption_excluded"] is False


def test_map_hillsborough_row_homestead_not_applicable_for_ineligible_use():
	row = {"FOLIO": "1", "DOR_C": "8600", "OWNER": "HILLSBOROUGH COUNTY", "BASE": "0"}
	assert map_hillsborough_row(row)["homestead_status"] == NOT_APPLICABLE


def test_map_pinellas_property_info_row_never_sets_homestead_columns():
	"""RP_EXEMPTIONS owns these for Pinellas — the property-info mapper
	must not even offer a key for them (column-ownership rule)."""
	row = {"STRAP": "1", "PROPERTY_USE": "0110", "OWNER1": "X"}
	mapped = map_pinellas_property_info_row(row)
	assert "homestead_status" not in mapped
	assert "exemption_excluded" not in mapped


# ── Pinellas field mapping ──────────────────────────────────────────────


def test_map_pinellas_row_basic():
	row = {
		"STRAP": "143001153090004010",
		"PROPERTY_USE": "0110",
		"OWNER1": "JOHN SMITH",
		"SITE_ADDRESS": "123 MAIN ST",
		"MAILING_STATE": "GEORGIA",
		"TOTAL_LIVING_UNITS": "1",
		"STATUS": "",
	}
	mapped = map_pinellas_property_info_row(row)
	assert mapped["assessor_parcel_id"] == "143001153090004010"
	assert mapped["strap"] == "143001153090004010"
	assert mapped["property_class"] == "STANDARD_RESIDENTIAL"
	assert mapped["owner_mailing_state"] == "GA"
	assert mapped["unit_count"] == 1


def test_map_pinellas_row_delete_in_progress_forces_ineligible_regardless_of_use_code():
	row = {"STRAP": "1", "PROPERTY_USE": "0110", "OWNER1": "X", "STATUS": "Delete in Progress"}
	assert map_pinellas_property_info_row(row)["property_class"] is None


def test_map_pinellas_row_new_parcel_in_progress_forces_ineligible():
	row = {"STRAP": "1", "PROPERTY_USE": "0110", "OWNER1": "X", "STATUS": "New parcel in progress"}
	assert map_pinellas_property_info_row(row)["property_class"] is None


def test_map_pinellas_row_blank_status_does_not_affect_eligibility():
	row = {"STRAP": "1", "PROPERTY_USE": "0110", "OWNER1": "X", "STATUS": None}
	assert map_pinellas_property_info_row(row)["property_class"] == "STANDARD_RESIDENTIAL"


def test_map_pinellas_row_government_owner_safeguard():
	row = {"STRAP": "1", "PROPERTY_USE": "0110", "OWNER1": "PINELLAS COUNTY"}
	assert map_pinellas_property_info_row(row)["property_class"] is None


def test_map_pinellas_property_use_code_is_2digit_land_use_cd():
	row = {"STRAP": "1", "PROPERTY_USE": "0110", "LAND_USE_CD": "01", "OWNER1": "X"}
	assert map_pinellas_property_info_row(row)["property_use_code"] == "01"


# ── Pinellas exemption classification ───────────────────────────────────


def test_classify_pinellas_exemption_blank_means_no_exemption_not_unknown():
	assert classify_pinellas_exemption(None) is False
	assert classify_pinellas_exemption("") is False
	assert classify_pinellas_exemption("   ") is False


def test_classify_pinellas_exemption_government_and_agricultural_excluded():
	assert classify_pinellas_exemption("Government") is True
	assert classify_pinellas_exemption("Agricultural") is True
	assert classify_pinellas_exemption("government owned") is True


def test_classify_pinellas_exemption_unrecognized_text_not_excluded():
	assert classify_pinellas_exemption("Widget Exemption") is False


# ── Pinellas exemptions join (real sample data from the client, 2026-09-11) ──


def _hx_row(strap: str, hx_yr: int, hx_yn: str, hx_use: str = "0%", hx_status: str = "", exemption: str = ""):
	return {
		"STRAP": strap, "HX_YR": hx_yr, "HX_YN": hx_yn, "HX_USE": hx_use,
		"HX_STATUS": hx_status, "PROPERTY_EXEMPTION": exemption,
	}


def test_exemptions_pinning_never_uses_the_forecast_year_as_current():
	"""Real sample data: 2025=Yes, 2026=Yes, 2027=Yes with HX_STATUS
	'Assuming no ownership changes before Jan. 1, 2027.' — a forecast, not
	an observed fact. ROLL_YEAR=2026 must select the 2026 row as current,
	never silently drift to 2027's projection."""
	prev_row = _hx_row("STRAP1", 2025, "Yes", "100%")
	curr_row = _hx_row("STRAP1", 2026, "Yes", "100%")
	next_row = _hx_row("STRAP1", 2027, "Yes", "100%", hx_status="Assuming no ownership changes before Jan. 1, 2027.")
	update = derive_pinellas_exemption_update(curr_row, prev_row, next_row)
	assert update["homestead_status"] == HOMESTEAD
	assert update["homestead_previous"] == HOMESTEAD
	assert update["homestead_next_year"] == HOMESTEAD
	assert update["homestead_roll_year"] == 2026


def test_exemptions_homestead_drop_detected_on_first_import():
	"""2025=Yes, 2026=No — a real drop, visible on day one because the
	prior year ships in the same file, without waiting for a second sync."""
	prev_row = _hx_row("STRAP2", 2025, "Yes")
	curr_row = _hx_row("STRAP2", 2026, "No")
	update = derive_pinellas_exemption_update(curr_row, prev_row, None)
	assert update["homestead_previous"] == HOMESTEAD
	assert update["homestead_status"] == NO_HOMESTEAD


def test_exemptions_pending_loss_next_year_signal():
	curr_row = _hx_row("STRAP3", 2026, "Yes")
	next_row = _hx_row("STRAP3", 2027, "No")
	update = derive_pinellas_exemption_update(curr_row, None, next_row)
	assert update["homestead_status"] == HOMESTEAD
	assert update["homestead_next_year"] == NO_HOMESTEAD


def test_exemptions_hx_use_percentage_string_parsed():
	curr_row = _hx_row("STRAP4", 2026, "Yes", hx_use="50%")
	update = derive_pinellas_exemption_update(curr_row, None, None)
	assert update["homestead_use_pct"] == 50


def test_exemptions_no_current_year_row_is_unknown_not_a_crash():
	update = derive_pinellas_exemption_update(None, None, None)
	assert update["homestead_status"] == UNKNOWN
	assert update["homestead_roll_year"] is None


def test_exemptions_property_exemption_drives_exclusion():
	curr_row = _hx_row("STRAP5", 2026, "No", exemption="Government")
	update = derive_pinellas_exemption_update(curr_row, None, None)
	assert update["exemption_excluded"] is True
	assert update["property_exemption_raw"] == "Government"


def test_exemptions_no_exemption_text_means_not_excluded():
	curr_row = _hx_row("STRAP6", 2026, "No", exemption="")
	update = derive_pinellas_exemption_update(curr_row, None, None)
	assert update["exemption_excluded"] is False


def test_exemptions_current_year_blank_never_falls_back_to_a_stale_prior_year_value():
	"""Regression test: a parcel that was government-owned last year but
	sold to a private owner this year (current year's field correctly
	blank) must NOT stay excluded because of the stale prior-year value —
	a real bug found in review that would have permanently suppressed a
	legitimate lead."""
	curr_row = _hx_row("STRAP7", 2026, "No", exemption="")
	prev_row = _hx_row("STRAP7", 2025, "No", exemption="Government")
	update = derive_pinellas_exemption_update(curr_row, prev_row, None)
	assert update["exemption_excluded"] is False
	assert update["property_exemption_raw"] == ""


def test_exemptions_current_year_blank_never_borrows_from_next_year_either():
	curr_row = _hx_row("STRAP8", 2026, "No", exemption="")
	next_row = _hx_row("STRAP8", 2027, "No", exemption="Government")
	update = derive_pinellas_exemption_update(curr_row, None, next_row)
	assert update["exemption_excluded"] is False


def test_exemptions_falls_back_to_previous_year_only_when_current_year_row_is_entirely_missing():
	"""If the current roll year's row is genuinely absent (not merely
	blank), falling back to an adjacent year is the best available signal
	— a real gap, distinct from "this year says no exemption"."""
	prev_row = _hx_row("STRAP9", 2025, "No", exemption="Government")
	update = derive_pinellas_exemption_update(None, prev_row, None)
	assert update["exemption_excluded"] is True


# ── Hillsborough homestead derivation (BASE column) ─────────────────────


def test_hillsborough_homestead_residential_with_base_positive():
	status, raw = derive_hillsborough_homestead("STANDARD_RESIDENTIAL", 2015)
	assert status == HOMESTEAD
	assert raw == "2015"


def test_hillsborough_homestead_residential_with_base_zero():
	status, _ = derive_hillsborough_homestead("STANDARD_RESIDENTIAL", 0)
	assert status == NO_HOMESTEAD


def test_hillsborough_homestead_non_residential_is_not_applicable_never_a_guess():
	"""BASE is only a homestead year for a residential parcel — for a
	mobile home park or mixed-use parcel it can mean a different kind of
	cap entirely, per the task doc §4 rule."""
	status, _ = derive_hillsborough_homestead("MOBILE_HOME_PARK", 2015)
	assert status == NOT_APPLICABLE
	status, _ = derive_hillsborough_homestead(None, 2015)
	assert status == NOT_APPLICABLE


def test_hillsborough_homestead_missing_base_is_unknown():
	status, _ = derive_hillsborough_homestead("STANDARD_RESIDENTIAL", None)
	assert status == UNKNOWN


if __name__ == "__main__":
	import pytest

	raise SystemExit(pytest.main([__file__, "-v"]))
