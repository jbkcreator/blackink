"""Tests for the Fee-Stack One-Pager PDF generator (no DB, HTTP, or file I/O)."""
import zlib
import pytest

from src.services.owner_visibility.fee_stack_pdf import (
    FeeStackData,
    FeeCategory,
    compile_fee_stack_pdf,
    _build_categories,
    _fmt_dollars,
)


def _sample_data(**overrides) -> FeeStackData:
    base = FeeStackData(
        company_name="Hoffman Realty",
        county_name="Hillsborough County, FL",
        door_count=120,
        month_label="September 2026",
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


def _decompress_pdf_text(pdf: bytes) -> str:
    """Decompress all FlateDecode streams and return as latin-1 text."""
    text = b""
    for segment in pdf.split(b"stream\n")[1:]:
        chunk = segment.split(b"\nendstream")[0]
        try:
            text += zlib.decompress(chunk)
        except Exception:
            pass
    return text.decode("latin-1", errors="ignore")


class TestCompileFeePdf:
    def test_returns_bytes(self):
        assert isinstance(compile_fee_stack_pdf(_sample_data()), bytes)

    def test_pdf_header_magic(self):
        pdf = compile_fee_stack_pdf(_sample_data())
        assert pdf[:5] == b"%PDF-"

    def test_pdf_has_content(self):
        pdf = compile_fee_stack_pdf(_sample_data())
        assert len(pdf) > 2000

    def test_single_page(self):
        pdf = compile_fee_stack_pdf(_sample_data())
        # fpdf2 encodes /Count N in the page tree object
        assert b"/Count 1" in pdf

    def test_company_name_in_pdf(self):
        pdf = compile_fee_stack_pdf(_sample_data())
        text = _decompress_pdf_text(pdf)
        assert "Hoffman" in text

    def test_no_month_label_renders(self):
        data = _sample_data(month_label="")
        pdf = compile_fee_stack_pdf(data)
        assert len(pdf) > 2000

    def test_long_company_name_renders(self):
        data = _sample_data(company_name="A" * 80)
        assert len(compile_fee_stack_pdf(data)) > 2000

    def test_single_door_renders(self):
        assert len(compile_fee_stack_pdf(_sample_data(door_count=1))) > 2000

    def test_large_portfolio_renders(self):
        assert len(compile_fee_stack_pdf(_sample_data(door_count=5000))) > 2000

    def test_five_categories_present(self):
        categories = _build_categories(_sample_data())
        assert len(categories) == 5

    def test_category_keys(self):
        keys = {c.key for c in _build_categories(_sample_data())}
        assert keys == {"lease_renewal", "maintenance_markup", "tenant_setup", "pet_rent", "rbp"}

    def test_all_uplifts_positive(self):
        for cat in _build_categories(_sample_data()):
            assert cat.annual_uplift > 0, f"{cat.key} uplift should be positive"


class TestUpliftMath:
    def test_lease_renewal_math(self):
        data = _sample_data(
            door_count=100,
            lease_renewal_fee=200.0,
            renewal_rate_pct=0.80,
        )
        cats = {c.key: c for c in _build_categories(data)}
        assert cats["lease_renewal"].annual_uplift == pytest.approx(100 * 0.80 * 200.0)

    def test_maintenance_markup_math(self):
        data = _sample_data(
            door_count=100,
            maintenance_spend_per_door=500.0,
            maintenance_markup_pct=0.10,
        )
        cats = {c.key: c for c in _build_categories(data)}
        assert cats["maintenance_markup"].annual_uplift == pytest.approx(100 * 500.0 * 0.10)

    def test_tenant_setup_math(self):
        data = _sample_data(
            door_count=100,
            tenant_setup_fee=150.0,
            renewal_rate_pct=0.80,
        )
        cats = {c.key: c for c in _build_categories(data)}
        assert cats["tenant_setup"].annual_uplift == pytest.approx(100 * 0.80 * 150.0)

    def test_pet_rent_math(self):
        data = _sample_data(
            door_count=100,
            pet_rent_monthly=35.0,
            pet_occupancy_pct=0.30,
        )
        cats = {c.key: c for c in _build_categories(data)}
        assert cats["pet_rent"].annual_uplift == pytest.approx(100 * 0.30 * 35.0 * 12)

    def test_rbp_math(self):
        data = _sample_data(
            door_count=100,
            rbp_monthly_per_door=15.0,
        )
        cats = {c.key: c for c in _build_categories(data)}
        assert cats["rbp"].annual_uplift == pytest.approx(100 * 15.0 * 12)

    def test_total_scales_with_doors(self):
        cats_100 = _build_categories(_sample_data(door_count=100))
        cats_200 = _build_categories(_sample_data(door_count=200))
        total_100 = sum(c.annual_uplift for c in cats_100)
        total_200 = sum(c.annual_uplift for c in cats_200)
        assert total_200 == pytest.approx(total_100 * 2, rel=1e-6)


class TestFmtDollars:
    def test_small_amount(self):
        assert _fmt_dollars(500.0) == "$500"

    def test_thousands(self):
        assert _fmt_dollars(12000.0) == "$12,000"

    def test_large_amount(self):
        assert _fmt_dollars(1234567.89) == "$1,234,568"

    def test_zero(self):
        assert _fmt_dollars(0.0) == "$0"
