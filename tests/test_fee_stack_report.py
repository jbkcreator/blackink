"""Unit tests for the ADD-8-Lite Fee-Stack One-Pager PDF generator (Subtask 2.2.3)."""
import pytest

pytest.importorskip("fpdf", reason="fpdf2 not installed")

from src.agents.ink.subagents.fee_stack.fee_stack_report import (
    FeeStackData,
    _calc_categories,
    compile_fee_stack_pdf,
)


class TestCalcCategories:
    def test_returns_five_categories(self):
        cats = _calc_categories(100)
        assert len(cats) == 5

    def test_all_non_zero_uplift(self):
        cats = _calc_categories(50)
        for name, amount, note in cats:
            assert amount > 0, f"Category '{name}' has zero uplift"

    def test_scales_with_door_count(self):
        cats_100 = {name: amt for name, amt, _ in _calc_categories(100)}
        cats_200 = {name: amt for name, amt, _ in _calc_categories(200)}
        for name in cats_100:
            assert cats_200[name] > cats_100[name], f"Category '{name}' did not scale up"

    def test_lease_renewal_formula(self):
        cats = {name: amt for name, amt, _ in _calc_categories(100)}
        assert cats["Lease Renewal Fees"] == 100 * 150

    def test_resident_benefits_formula(self):
        cats = {name: amt for name, amt, _ in _calc_categories(100)}
        assert cats["Resident Benefits Package"] == 100 * 180


class TestCompileFeeStackPdf:
    def _make_data(self, door_count=80) -> FeeStackData:
        return FeeStackData(
            company_name="Gulfshore Property Management",
            county_name="Collier County",
            door_count=door_count,
            audit_date="September 2026",
        )

    def test_returns_bytes(self):
        result = compile_fee_stack_pdf(self._make_data())
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_pdf_magic_bytes(self):
        result = compile_fee_stack_pdf(self._make_data())
        assert result[:4] == b"%PDF", "Output is not a PDF"

    def test_small_door_count(self):
        """Even 1 door must not crash."""
        result = compile_fee_stack_pdf(self._make_data(door_count=1))
        assert result[:4] == b"%PDF"

    def test_large_door_count(self):
        result = compile_fee_stack_pdf(self._make_data(door_count=5000))
        assert result[:4] == b"%PDF"

    def test_long_company_name_truncated(self):
        """Very long company names must not overflow the layout."""
        data = FeeStackData(
            company_name="A" * 120,
            county_name="Hillsborough County",
            door_count=100,
            audit_date="September 2026",
        )
        result = compile_fee_stack_pdf(data)
        assert result[:4] == b"%PDF"
