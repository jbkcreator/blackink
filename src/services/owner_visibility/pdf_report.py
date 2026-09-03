"""Owner Visibility Score PDF Report Compiler (Subtask 2.1.3).

Produces a 2-page branded executive PDF from a single function call:

    pdf_bytes = compile_pdf(data)

Page 1 - Score, county rank, data coverage %, 3 weakest signals with peers.
Page 2 - Revenue impact model with labelled inputs and formula.

No I/O - caller is responsible for writing bytes to disk or object storage.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from fpdf import FPDF

# Brand palette (RGB)
_NAVY  = (15,  23,  42)
_GOLD  = (234, 179,  8)
_SLATE = (71,  85, 105)
_LIGHT = (241, 245, 249)
_WHITE = (255, 255, 255)
_RED   = (220,  38,  38)
_GREEN = (22,  163,  74)

_SIGNAL_LABELS: dict[str, str] = {
    "website_owner_page":       "Owner / About Page",
    "website_contact_info":     "Contact Information",
    "website_tech_health":      "Website Performance",
    "website_after_hours":      "After-Hours Availability",
    "dbpr_active_licence":      "FL Broker Licence (DBPR)",
    "google_rating":            "Google Rating",
    "google_review_volume":     "Review Volume",
    "google_review_recency":    "Review Recency",
    "google_biz_completeness":  "Business Profile Completeness",
    "google_response_rate":     "Owner Response Rate",
}

_SIGNAL_DESCRIPTIONS: dict[str, str] = {
    "website_owner_page":       "Your website should prominently feature your team, credentials, and ownership story.",
    "website_contact_info":     "Phone, email, and office address must be easy to find on every page.",
    "website_tech_health":      "Fast, mobile-ready, HTTPS sites rank higher and convert more prospective owners.",
    "website_after_hours":      "Live chat or answering service signals 24/7 responsiveness - a top owner concern.",
    "dbpr_active_licence":      "A current DBPR active broker licence establishes legal credibility in Florida.",
    "google_rating":            "A Google rating of 4.5+ signals reliable, trust-worthy service.",
    "google_review_volume":     "More reviews indicate an established firm owners feel confident recommending.",
    "google_review_recency":    "Recent reviews show you are actively earning owner trust today.",
    "google_biz_completeness":  "Complete Google Business profiles (hours, photos, description) rank higher in search.",
    "google_response_rate":     "Responding to reviews demonstrates professionalism to researching owners.",
}


@dataclass(frozen=True)
class WeakSignal:
    signal_name: str
    points_awarded: int
    points_possible: int
    peer_names: list[str]


@dataclass
class OVSReportData:
    """All data required to render one OVS report. Caller assembles from DB rows."""
    company_name: str
    county_name: str
    month_label: str
    score_total: int
    data_coverage_pct: int
    county_rank: int | None
    county_percentile: int | None
    county_firm_count: int
    weakest_signals: list[WeakSignal]
    # Revenue model inputs
    monthly_leads: int = 5
    avg_response_gap_sec: int = 14400
    avg_monthly_fee_per_owner: float = 200.0
    owner_tenure_months: int = 30
    model_assumptions: str = (
        "8% management fee on $100/door/month gross rent, "
        "30-month average owner tenure, "
        "5 new owner inquiries per month, "
        "4-hour county average response gap."
    )


class _OVSDoc(FPDF):
    """FPDF subclass with shared layout helpers."""

    def header(self) -> None:
        self._draw_header_bar()

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*_SLATE)
        self.cell(
            0, 5,
            "This report uses publicly observable data only. All estimates are based on "
            "industry benchmarks and are not a guarantee of results. | getblackink.com",
            align="C",
        )

    def _draw_header_bar(self) -> None:
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

    def draw_section_rule(self, y: float, label: str) -> float:
        self.set_draw_color(*_NAVY)
        self.set_line_width(0.4)
        self.line(12, y, self.w - 12, y)
        self.set_xy(12, y + 1)
        self.set_font("Helvetica", "B", 7)
        self.set_text_color(*_SLATE)
        self.cell(0, 4, label.upper())
        return y + 7

    def score_circle(self, cx: float, cy: float, r: float, score: int) -> None:
        self.set_fill_color(*_NAVY)
        self.ellipse(cx - r, cy - r, r * 2, r * 2, style="F")
        self.set_font("Helvetica", "B", 34)
        self.set_text_color(*_WHITE)
        self.set_xy(cx - r, cy - 8)
        self.cell(r * 2, 16, str(score), align="C")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*_GOLD)
        self.set_xy(cx - r, cy + 9)
        self.cell(r * 2, 6, "out of 100", align="C")


def _calc_revenue_model(data: OVSReportData) -> tuple[float, float]:
    """Returns (lost_fraction, annual_lost_revenue)."""
    lost_fraction = 1.0 - math.exp(-0.0005 * data.avg_response_gap_sec)
    owner_tenure_years = data.owner_tenure_months / 12.0
    annual_lost = (
        data.monthly_leads
        * 12
        * lost_fraction
        * data.avg_monthly_fee_per_owner
        * owner_tenure_years
    )
    return lost_fraction, annual_lost


def _render_page1(doc: _OVSDoc, data: OVSReportData) -> None:
    doc.add_page()

    doc.set_xy(12, 22)
    doc.set_font("Helvetica", "B", 9)
    doc.set_text_color(*_GOLD)
    doc.cell(0, 5, "OWNER VISIBILITY SCORE REPORT")

    doc.set_xy(12, 28)
    doc.set_font("Helvetica", "B", 16)
    doc.set_text_color(*_NAVY)
    doc.cell(0, 8, data.company_name[:60])

    doc.set_xy(12, 36)
    doc.set_font("Helvetica", "", 9)
    doc.set_text_color(*_SLATE)
    doc.cell(0, 5, f"{data.county_name}  |  {data.month_label}")

    y_panel = 44
    panel_h  = 52
    left_w   = 68
    right_w  = doc.w - 24 - left_w

    doc.set_fill_color(*_LIGHT)
    doc.rect(12, y_panel, left_w, panel_h, style="F")
    doc.score_circle(cx=12 + left_w / 2, cy=y_panel + panel_h / 2 - 4, r=20, score=data.score_total)
    doc.set_xy(12, y_panel + panel_h - 10)
    doc.set_font("Helvetica", "", 8)
    doc.set_text_color(*_SLATE)
    doc.cell(left_w, 6, f"Data Coverage: {data.data_coverage_pct}%", align="C")

    rx = 12 + left_w + 4
    doc.set_fill_color(*_WHITE)
    doc.set_draw_color(*_LIGHT)
    doc.rect(rx, y_panel, right_w, panel_h, style="FD")

    if data.county_rank is not None:
        doc.set_xy(rx + 4, y_panel + 6)
        doc.set_font("Helvetica", "B", 28)
        doc.set_text_color(*_NAVY)
        doc.cell(right_w - 8, 14, f"#{data.county_rank}", ln=True)

        doc.set_xy(rx + 4, y_panel + 20)
        doc.set_font("Helvetica", "", 9)
        doc.set_text_color(*_SLATE)
        doc.cell(right_w - 8, 5, f"of {data.county_firm_count} firms in {data.county_name}")

        doc.set_xy(rx + 4, y_panel + 27)
        doc.set_font("Helvetica", "B", 11)
        doc.set_text_color(*_GREEN)
        pct = data.county_percentile if data.county_percentile is not None else 0
        doc.cell(right_w - 8, 6, f"{pct}th Percentile")

        doc.set_xy(rx + 4, y_panel + 36)
        doc.set_font("Helvetica", "", 8)
        doc.set_text_color(*_SLATE)
        doc.multi_cell(
            right_w - 8, 4,
            "Top 25 firms per county are shared with prospects "
            "as part of the Blackink market-insight program.",
        )
    else:
        doc.set_xy(rx + 4, y_panel + 14)
        doc.set_font("Helvetica", "I", 9)
        doc.set_text_color(*_SLATE)
        doc.multi_cell(right_w - 8, 5, "Insufficient data to rank this firm this month.")

    y = y_panel + panel_h + 6
    y = doc.draw_section_rule(y, "3 Areas to Improve Your Visibility Score")

    for i, sig in enumerate(data.weakest_signals[:3], 1):
        label     = _SIGNAL_LABELS.get(sig.signal_name, sig.signal_name)
        desc      = _SIGNAL_DESCRIPTIONS.get(sig.signal_name, "")
        pts_label = f"{sig.points_awarded}/{sig.points_possible} pts"

        doc.set_xy(12, y)
        doc.set_fill_color(*_LIGHT)
        doc.rect(12, y, doc.w - 24, 7, style="F")
        doc.set_xy(14, y + 1)
        doc.set_font("Helvetica", "B", 9)
        doc.set_text_color(*_NAVY)
        doc.cell(doc.w - 28 - 24, 5, f"{i}.  {label}")
        doc.set_font("Helvetica", "", 8)
        doc.set_text_color(*_RED)
        doc.cell(24, 5, pts_label, align="R")
        y += 8

        doc.set_xy(16, y)
        doc.set_font("Helvetica", "", 8)
        doc.set_text_color(*_SLATE)
        doc.multi_cell(doc.w - 28, 4, desc)
        y = doc.get_y() + 2

        if sig.peer_names:
            doc.set_xy(16, y)
            doc.set_font("Helvetica", "I", 8)
            doc.set_text_color(*_GREEN)
            peer_str = "Higher-ranked peers: " + ",  ".join(sig.peer_names)
            doc.multi_cell(doc.w - 28, 4, peer_str)
            y = doc.get_y()

        y += 4
        if y > doc.h - 20:
            break


def _render_page2(doc: _OVSDoc, data: OVSReportData) -> None:
    doc.add_page()
    lost_fraction, annual_lost = _calc_revenue_model(data)
    owner_tenure_years = data.owner_tenure_months / 12.0

    doc.set_xy(12, 22)
    doc.set_font("Helvetica", "B", 9)
    doc.set_text_color(*_GOLD)
    doc.cell(0, 5, "REVENUE IMPACT MODEL  -  ESTIMATED ANNUAL REVENUE AT RISK")

    doc.set_xy(12, 28)
    doc.set_font("Helvetica", "B", 14)
    doc.set_text_color(*_NAVY)
    doc.cell(0, 7, data.company_name[:60])

    doc.set_xy(12, 35)
    doc.set_font("Helvetica", "", 9)
    doc.set_text_color(*_SLATE)
    doc.cell(0, 5, f"{data.county_name}  |  {data.month_label}")

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
    doc.cell(doc.w - 24, 4, "Estimate only - see assumptions below", align="C")

    y = 72
    y = doc.draw_section_rule(y, "Formula")
    doc.set_xy(12, y)
    doc.set_font("Helvetica", "", 8)
    doc.set_text_color(*_NAVY)
    doc.multi_cell(
        doc.w - 24, 5,
        "Lost Revenue = Monthly Leads x (1 - e^(-0.0005 x Avg Response Gap))"
        " x (Avg Monthly Fee x 12) x Owner Tenure (years)",
    )
    y = doc.get_y() + 4

    y = doc.draw_section_rule(y, "Model Inputs  (all figures are estimates)")
    col_w = (doc.w - 24) / 2

    rows: list[tuple[str, str]] = [
        ("Monthly Owner Inquiries",    str(data.monthly_leads)),
        ("Avg County Response Gap",    _fmt_gap(data.avg_response_gap_sec)),
        ("Fraction of Leads Lost",     f"{lost_fraction * 100:.1f}%"),
        ("Avg Monthly Management Fee", f"${data.avg_monthly_fee_per_owner:,.0f} / owner"),
        ("Average Owner Tenure",       f"{data.owner_tenure_months} months ({owner_tenure_years:.1f} yrs)"),
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
    y = doc.draw_section_rule(y, "Calculation")
    calc_lines = [
        f"Monthly leads lost  =  {data.monthly_leads}  x  {lost_fraction:.4f}  =  {data.monthly_leads * lost_fraction:.2f} leads/month",
        f"Annual leads lost   =  {data.monthly_leads * lost_fraction:.2f}  x  12  =  {data.monthly_leads * lost_fraction * 12:.1f} leads/year",
        f"Revenue per lead    =  ${data.avg_monthly_fee_per_owner:,.0f}  x  {owner_tenure_years:.1f} yrs  =  ${data.avg_monthly_fee_per_owner * owner_tenure_years:,.0f} lifetime value",
        f"Annual revenue lost =  {data.monthly_leads * lost_fraction * 12:.1f}  x  ${data.avg_monthly_fee_per_owner * owner_tenure_years:,.0f}  =  ${annual_lost:,.0f}",
    ]
    doc.set_font("Courier", "", 8)
    doc.set_text_color(*_NAVY)
    for line in calc_lines:
        doc.set_xy(14, y)
        doc.cell(0, 5, line)
        y += 5

    y += 4
    y = doc.draw_section_rule(y, "Assumptions & Disclaimer")
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


def _fmt_gap(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} sec"
    if seconds < 3600:
        return f"{seconds // 60} min"
    hours = seconds / 3600
    return f"{hours:.1f} hrs"


def compile_pdf(data: OVSReportData) -> bytes:
    """Compile and return a 2-page OVS report as PDF bytes."""
    doc = _OVSDoc(orientation="P", unit="mm", format="Letter")
    doc.set_auto_page_break(auto=True, margin=14)
    doc.set_margins(left=12, top=20, right=12)

    _render_page1(doc, data)
    _render_page2(doc, data)

    return bytes(doc.output())
