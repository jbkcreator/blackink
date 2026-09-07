"""Tests for the OVS PDF report compiler (no DB, HTTP, or file I/O)."""
import math
import zlib
import pytest

from src.services.owner_visibility.pdf_report import (
    OVSReportData,
    WeakSignal,
    compile_pdf,
    _calc_revenue_model,
    _fmt_gap,
)


def _sample_data(**overrides) -> OVSReportData:
    base = OVSReportData(
        company_name="Hoffman Realty",
        county_name="Hillsborough County, FL",
        month_label="September 2026",
        score_total=38,
        data_coverage_pct=60,
        county_rank=1,
        county_percentile=100,
        county_firm_count=5,
        weakest_signals=[
            WeakSignal("website_after_hours",  0, 8,  ["Rent Solutions"]),
            WeakSignal("website_contact_info", 0, 10, ["Rent Solutions", "Bay Management"]),
            WeakSignal("dbpr_active_licence",  0, 4,  []),
        ],
    )
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


class TestCompilePdf:
    def test_returns_bytes(self):
        pdf = compile_pdf(_sample_data())
        assert isinstance(pdf, bytes)

    def test_pdf_has_content(self):
        pdf = compile_pdf(_sample_data())
        assert len(pdf) > 2000

    def test_pdf_header_magic(self):
        pdf = compile_pdf(_sample_data())
        # All PDFs start with %PDF-
        assert pdf[:5] == b"%PDF-"

    def test_two_pages_generated(self):
        pdf = compile_pdf(_sample_data())
        # fpdf2 encodes page count as /Count N or as multiple /Page objects
        assert pdf.count(b"/Page") >= 2

    def test_company_name_appears_in_pdf(self):
        pdf = compile_pdf(_sample_data())
        # fpdf2 compresses content streams with zlib/FlateDecode; decompress to find text.
        decompressed = b""
        for segment in pdf.split(b"stream\n")[1:]:
            chunk = segment.split(b"\nendstream")[0]
            try:
                decompressed += zlib.decompress(chunk)
            except Exception:
                pass
        text = decompressed.decode("latin-1", errors="ignore")
        assert "Hoffman" in text

    def test_no_rank_firm_renders_without_error(self):
        data = _sample_data(county_rank=None, county_percentile=None)
        pdf = compile_pdf(data)
        assert len(pdf) > 2000

    def test_empty_weakest_signals_renders(self):
        data = _sample_data(weakest_signals=[])
        pdf = compile_pdf(data)
        assert len(pdf) > 2000

    def test_single_weak_signal_renders(self):
        data = _sample_data(
            weakest_signals=[WeakSignal("dbpr_active_licence", 0, 4, ["Peer A"])]
        )
        pdf = compile_pdf(data)
        assert len(pdf) > 2000

    def test_peers_with_no_matches_renders(self):
        data = _sample_data(
            weakest_signals=[WeakSignal("website_after_hours", 0, 8, [])]
        )
        pdf = compile_pdf(data)
        assert len(pdf) > 2000

    def test_long_company_name_renders(self):
        data = _sample_data(company_name="A" * 80)
        pdf = compile_pdf(data)
        assert len(pdf) > 2000

    def test_score_zero_renders(self):
        pdf = compile_pdf(_sample_data(score_total=0))
        assert len(pdf) > 2000

    def test_score_100_renders(self):
        pdf = compile_pdf(_sample_data(score_total=100))
        assert len(pdf) > 2000


class TestRevenueModel:
    def test_zero_gap_means_no_lost_leads(self):
        data = _sample_data(avg_response_gap_sec=0)
        lost_fraction, annual_lost = _calc_revenue_model(data)
        assert lost_fraction == pytest.approx(0.0, abs=1e-9)
        assert annual_lost == pytest.approx(0.0, abs=1e-6)

    def test_large_gap_approaches_100pct_loss(self):
        data = _sample_data(avg_response_gap_sec=100_000)
        lost_fraction, _ = _calc_revenue_model(data)
        assert lost_fraction > 0.999

    def test_4hr_gap_formula(self):
        data = _sample_data(
            avg_response_gap_sec=14400,
            monthly_leads=5,
            avg_monthly_fee_per_owner=200.0,
            owner_tenure_months=30,
        )
        lost_fraction, annual_lost = _calc_revenue_model(data)
        expected_fraction = 1 - math.exp(-0.0005 * 14400)
        expected_annual = 5 * 12 * expected_fraction * 200.0 * 2.5
        assert lost_fraction == pytest.approx(expected_fraction, rel=1e-6)
        assert annual_lost == pytest.approx(expected_annual, rel=1e-6)

    def test_revenue_scales_with_monthly_leads(self):
        base = _sample_data(monthly_leads=5)
        double = _sample_data(monthly_leads=10)
        _, rev_base = _calc_revenue_model(base)
        _, rev_double = _calc_revenue_model(double)
        assert rev_double == pytest.approx(rev_base * 2, rel=1e-6)

    def test_revenue_scales_with_fee(self):
        base = _sample_data(avg_monthly_fee_per_owner=200.0)
        high = _sample_data(avg_monthly_fee_per_owner=400.0)
        _, rev_base = _calc_revenue_model(base)
        _, rev_high = _calc_revenue_model(high)
        assert rev_high == pytest.approx(rev_base * 2, rel=1e-6)


class TestFmtGap:
    def test_seconds(self):
        assert _fmt_gap(45) == "45 sec"

    def test_minutes(self):
        assert _fmt_gap(120) == "2 min"

    def test_hours(self):
        assert _fmt_gap(14400) == "4.0 hrs"

    def test_fractional_hours(self):
        assert _fmt_gap(5400) == "1.5 hrs"
