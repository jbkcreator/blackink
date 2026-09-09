"""Campaign Response-Time Audit PDF (Ghost Shopper proof asset).

Produces a 2-page branded PDF from one call:

    pdf_bytes = compile_campaign_pdf(data)

Page 1 -- Response Time Audit: detected latency vs. industry benchmark.
Page 2 -- Revenue Impact Model: annual revenue at risk from slow response.

No I/O -- caller uploads bytes to storage (see storage.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from fpdf import FPDF

# Shared brand palette (matches OVS PDF)
_NAVY  = (15,  23,  42)
_GOLD  = (234, 179,  8)
_SLATE = (71,  85, 105)
_LIGHT = (241, 245, 249)
_WHITE = (255, 255, 255)
_RED   = (220,  38,  38)
_GREEN = (22,  163,  74)
_AMBER = (217, 119,   6)

_BENCHMARK_SEC = 300   # 5-minute industry best practice


@dataclass
class CampaignAuditData:
    company_name:         str
    county_name:          str
    audit_date:           str           # e.g. "September 2026"
    latency_sec:          int | None    # None = no reply within 24 h
    loss_est:             int           # pre-computed dollar figure
    monthly_leads:        int   = 5
    avg_monthly_fee:      float = 200.0
    owner_tenure_months:  int   = 30
    model_assumptions:    str   = field(default=(
        "8% management fee on $100/door/month gross rent, "
        "30-month average owner tenure, "
        "5 new owner inquiries per month."
    ))


def _status(latency_sec: int | None) -> tuple[str, tuple[int, int, int]]:
    """Returns (label, RGB colour) for the response-time status badge."""
    if latency_sec is None:
        return "NO REPLY (24 h+)", _RED
    if latency_sec <= _BENCHMARK_SEC:
        return "EXCELLENT", _GREEN
    if latency_sec <= 3_600:
        return "ACCEPTABLE", _AMBER
    if latency_sec <= 14_400:
        return "SLOW", _AMBER
    return "CRITICAL", _RED


def _fmt_latency(latency_sec: int | None) -> str:
    if latency_sec is None:
        return "No Reply"
    if latency_sec < 60:
        return f"{latency_sec} sec"
    if latency_sec < 3_600:
        m = latency_sec // 60
        s = latency_sec % 60
        return f"{m} min {s} sec" if s else f"{m} min"
    h = latency_sec // 3_600
    m = (latency_sec % 3_600) // 60
    return f"{h} hr {m} min" if m else f"{h} hr"


def _calc_model(data: CampaignAuditData) -> tuple[float, float]:
    """Returns (lost_fraction, annual_lost_revenue)."""
    gap = data.latency_sec if data.latency_sec is not None else 86_400
    lost_fraction = 1.0 - math.exp(-0.0005 * gap)
    tenure_years  = data.owner_tenure_months / 12.0
    annual_lost   = (
        data.monthly_leads * 12
        * lost_fraction
        * data.avg_monthly_fee
        * tenure_years
    )
    return lost_fraction, annual_lost


class _CampaignDoc(FPDF):
    def header(self) -> None:
        self.set_fill_color(*_NAVY)
        self.rect(0, 0, self.w, 18, style="F")
        self.set_xy(12, 4)
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(*_WHITE)
        self.cell(60, 10, "BLACKINK", ln=False)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_GOLD)
        self.set_xy(self.w - 52, 6)
        self.cell(40, 6, "getblackink.com", align="R")

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*_SLATE)
        self.cell(
            0, 5,
            "Audit performed using publicly observable response signals. "
            "All estimates are illustrative benchmarks, not a guarantee of results. "
            "| getblackink.com",
            align="C",
        )

    def section_rule(self, y: float, label: str) -> float:
        self.set_draw_color(*_NAVY)
        self.set_line_width(0.4)
        self.line(12, y, self.w - 12, y)
        self.set_xy(12, y + 1)
        self.set_font("Helvetica", "B", 7)
        self.set_text_color(*_SLATE)
        self.cell(0, 4, label.upper())
        return y + 7


def _render_page1(doc: _CampaignDoc, data: CampaignAuditData) -> None:
    doc.add_page()

    # Sub-header
    doc.set_xy(12, 22)
    doc.set_font("Helvetica", "B", 9)
    doc.set_text_color(*_GOLD)
    doc.cell(0, 5, "RESPONSE TIME AUDIT REPORT")

    doc.set_xy(12, 28)
    doc.set_font("Helvetica", "B", 16)
    doc.set_text_color(*_NAVY)
    doc.cell(0, 8, data.company_name[:55])

    doc.set_xy(12, 36)
    doc.set_font("Helvetica", "", 9)
    doc.set_text_color(*_SLATE)
    doc.cell(0, 5, f"{data.county_name}  |  Audit: {data.audit_date}")

    # ── Two-column metric panel ──────────────────────────────────────────
    y_panel  = 45
    panel_h  = 46
    half_w   = (doc.w - 28) / 2
    gap      = 4

    status_label, status_color = _status(data.latency_sec)

    # Left panel -- detected response time
    doc.set_fill_color(*_LIGHT)
    doc.rect(12, y_panel, half_w, panel_h, style="F")

    doc.set_xy(12, y_panel + 4)
    doc.set_font("Helvetica", "B", 7)
    doc.set_text_color(*_SLATE)
    doc.cell(half_w, 4, "DETECTED RESPONSE TIME", align="C")

    doc.set_xy(12, y_panel + 10)
    doc.set_font("Helvetica", "B", 22)
    doc.set_text_color(*_NAVY)
    doc.cell(half_w, 12, _fmt_latency(data.latency_sec), align="C")

    doc.set_xy(12, y_panel + 24)
    doc.set_font("Helvetica", "B", 10)
    doc.set_text_color(*status_color)
    doc.cell(half_w, 6, f">> {status_label}", align="C")

    # Right panel -- benchmark
    rx = 12 + half_w + gap
    doc.set_fill_color(*_NAVY)
    doc.rect(rx, y_panel, half_w - gap, panel_h, style="F")

    doc.set_xy(rx, y_panel + 4)
    doc.set_font("Helvetica", "B", 7)
    doc.set_text_color(*_GOLD)
    doc.cell(half_w - gap, 4, "INDUSTRY BEST PRACTICE", align="C")

    doc.set_xy(rx, y_panel + 10)
    doc.set_font("Helvetica", "B", 22)
    doc.set_text_color(*_WHITE)
    doc.cell(half_w - gap, 12, "< 5 minutes", align="C")

    doc.set_xy(rx, y_panel + 24)
    doc.set_font("Helvetica", "", 8)
    doc.set_text_color(*_LIGHT)
    doc.cell(half_w - gap, 5, "Top-performing PM firms", align="C")
    doc.set_xy(rx, y_panel + 29)
    doc.cell(half_w - gap, 5, "respond within 5 minutes, 24/7", align="C")

    # ── Body sections ────────────────────────────────────────────────────
    y = y_panel + panel_h + 8

    y = doc.section_rule(y, "What This Means for Your Business")
    doc.set_xy(12, y)
    doc.set_font("Helvetica", "", 9)
    doc.set_text_color(*_NAVY)
    doc.multi_cell(
        doc.w - 24, 5,
        "Property owners evaluating PM firms consistently choose the fastest "
        "responder. Research shows that over 70% of owner inquiries go to the "
        "first firm that replies. Every hour of delay is a lead your competitors "
        "are winning while your team is unavailable.",
    )
    y = doc.get_y() + 6

    y = doc.section_rule(y, "How This Audit Was Conducted")
    doc.set_xy(12, y)
    doc.set_font("Helvetica", "", 9)
    doc.set_text_color(*_SLATE)
    doc.multi_cell(
        doc.w - 24, 5,
        f"Blackink's audit system submitted a realistic owner inquiry through "
        f"{data.company_name[:40]}'s public contact channel and measured the "
        f"elapsed time until a staff member replied. This reflects the real "
        f"experience a prospective owner would have contacting your firm today.",
    )
    y = doc.get_y() + 6

    y = doc.section_rule(y, "Audit Result Summary")
    col_w = (doc.w - 24) / 2
    rows = [
        ("Response detected",   _fmt_latency(data.latency_sec)),
        ("Status",              status_label),
        ("Industry benchmark",  "< 5 minutes"),
        ("Gap vs. benchmark",   _gap_vs_benchmark(data.latency_sec)),
    ]
    for label, value in rows:
        doc.set_fill_color(*_LIGHT)
        doc.rect(12, y, doc.w - 24, 6, style="F")
        doc.set_xy(14, y + 1)
        doc.set_font("Helvetica", "", 8)
        doc.set_text_color(*_SLATE)
        doc.cell(col_w - 2, 4, label)
        doc.set_font("Helvetica", "B", 8)
        doc.set_text_color(*_NAVY)
        doc.cell(col_w - 2, 4, value, align="R")
        y += 7


def _gap_vs_benchmark(latency_sec: int | None) -> str:
    if latency_sec is None:
        return "No reply received"
    if latency_sec <= _BENCHMARK_SEC:
        return "Within benchmark"
    delta = latency_sec - _BENCHMARK_SEC
    return f"{_fmt_latency(delta)} over benchmark"


def _render_page2(doc: _CampaignDoc, data: CampaignAuditData) -> None:
    doc.add_page()
    lost_fraction, annual_lost = _calc_model(data)
    tenure_years = data.owner_tenure_months / 12.0
    gap_sec = data.latency_sec if data.latency_sec is not None else 86_400

    doc.set_xy(12, 22)
    doc.set_font("Helvetica", "B", 9)
    doc.set_text_color(*_GOLD)
    doc.cell(0, 5, "ESTIMATED ANNUAL REVENUE AT RISK")

    doc.set_xy(12, 28)
    doc.set_font("Helvetica", "B", 16)
    doc.set_text_color(*_NAVY)
    doc.cell(0, 7, data.company_name[:55])

    doc.set_xy(12, 35)
    doc.set_font("Helvetica", "", 9)
    doc.set_text_color(*_SLATE)
    doc.cell(0, 5, f"{data.county_name}  |  {data.audit_date}")

    # Big loss figure
    y = 44
    doc.set_fill_color(*_LIGHT)
    doc.rect(12, y, doc.w - 24, 24, style="F")
    doc.set_xy(12, y + 3)
    doc.set_font("Helvetica", "", 9)
    doc.set_text_color(*_SLATE)
    doc.cell(doc.w - 24, 5, "Estimated Annual Revenue at Risk", align="C")
    doc.set_xy(12, y + 8)
    doc.set_font("Helvetica", "B", 28)
    doc.set_text_color(*_RED)
    doc.cell(doc.w - 24, 13, f"${annual_lost:,.0f}", align="C")
    doc.set_xy(12, y + 19)
    doc.set_font("Helvetica", "I", 8)
    doc.set_text_color(*_SLATE)
    doc.cell(doc.w - 24, 4, "Estimate only -- see assumptions below", align="C")

    y = 72
    y = doc.section_rule(y, "Formula")
    doc.set_xy(12, y)
    doc.set_font("Helvetica", "", 8)
    doc.set_text_color(*_NAVY)
    doc.multi_cell(
        doc.w - 24, 5,
        "Lost Revenue  =  Monthly Leads  ×  (1 - e^(-0.0005 × Response Delay))"
        "  ×  (Avg Monthly Fee × 12)  ×  Owner Tenure (years)",
    )
    y = doc.get_y() + 4

    y = doc.section_rule(y, "Model Inputs")
    col_w = (doc.w - 24) / 2
    rows: list[tuple[str, str]] = [
        ("Monthly Owner Inquiries",   str(data.monthly_leads)),
        ("Detected Response Delay",   _fmt_latency(data.latency_sec)),
        ("Leads Lost to Delay",       f"{lost_fraction * 100:.1f}%"),
        ("Avg Monthly Mgmt Fee",      f"${data.avg_monthly_fee:,.0f} / owner"),
        ("Average Owner Tenure",      f"{data.owner_tenure_months} months ({tenure_years:.1f} yrs)"),
    ]
    for label, value in rows:
        doc.set_fill_color(*_LIGHT)
        doc.rect(12, y, doc.w - 24, 6, style="F")
        doc.set_xy(14, y + 1)
        doc.set_font("Helvetica", "", 8)
        doc.set_text_color(*_SLATE)
        doc.cell(col_w - 2, 4, label)
        doc.set_font("Helvetica", "B", 8)
        doc.set_text_color(*_NAVY)
        doc.cell(col_w - 2, 4, value, align="R")
        y += 7

    y += 4
    y = doc.section_rule(y, "Calculation")
    monthly_lost = data.monthly_leads * lost_fraction
    calc_lines = [
        f"Monthly leads lost  =  {data.monthly_leads}  ×  {lost_fraction:.4f}  =  {monthly_lost:.2f} leads/month",
        f"Annual leads lost   =  {monthly_lost:.2f}  ×  12  =  {monthly_lost * 12:.1f} leads/year",
        f"Revenue per lead    =  ${data.avg_monthly_fee:,.0f}  ×  {tenure_years:.1f} yrs  =  ${data.avg_monthly_fee * tenure_years:,.0f} lifetime",
        f"Annual lost         =  {monthly_lost * 12:.1f}  ×  ${data.avg_monthly_fee * tenure_years:,.0f}  =  ${annual_lost:,.0f}",
    ]
    doc.set_font("Courier", "", 8)
    doc.set_text_color(*_NAVY)
    for line in calc_lines:
        doc.set_xy(14, y)
        doc.cell(0, 5, line)
        y += 5

    y += 4
    y = doc.section_rule(y, "How Blackink Closes This Gap")
    doc.set_xy(12, y)
    doc.set_font("Helvetica", "", 9)
    doc.set_text_color(*_NAVY)
    doc.multi_cell(
        doc.w - 24, 5,
        "Blackink clients respond to owner inquiries in under 5 minutes, "
        "24 hours a day -- including nights, weekends, and holidays. "
        "Our AI-driven engagement layer captures leads your team would otherwise miss "
        "and routes serious prospects directly to your closers.",
    )
    y = doc.get_y() + 4

    y = doc.section_rule(y, "Assumptions & Disclaimer")
    doc.set_xy(12, y)
    doc.set_font("Helvetica", "I", 7.5)
    doc.set_text_color(*_SLATE)
    doc.multi_cell(
        doc.w - 24, 4,
        data.model_assumptions
        + " All revenue figures are illustrative estimates based on publicly available "
        "industry benchmarks. They are not a guarantee, projection, or representation "
        "of actual past or future performance. Blackink makes no warranty as to accuracy.",
    )


def compile_campaign_pdf(data: CampaignAuditData) -> bytes:
    """Compile and return a 2-page campaign audit report as PDF bytes."""
    doc = _CampaignDoc(orientation="P", unit="mm", format="Letter")
    doc.set_auto_page_break(auto=True, margin=14)
    doc.set_margins(left=12, top=20, right=12)
    _render_page1(doc, data)
    _render_page2(doc, data)
    return bytes(doc.output())
