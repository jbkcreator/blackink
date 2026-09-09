"""ADD-8-Lite Fee-Stack One-Pager PDF generator.

Produces a 1-page branded PDF mapping uncollected fee lines and ancillary
revenue opportunities for a property management firm.

Five fee categories, each estimated from door count:
  1. Lease Renewal Fees          — $150/door/yr
  2. Maintenance Markups         — $200/door/yr (10% on ~$2k avg maintenance)
  3. Tenant Setup / Move-In Fees — 60% annual turnover × $100/unit
  4. Pet Rent Share              — 35% pet-owning units × $300/yr uncaptured
  5. Resident Benefits Package   — $180/door/yr ($15/unit/month unbilled)

Shares the fpdf2 renderer from pdf_report.py — no independent renderer.
No I/O — caller uploads bytes to storage.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fpdf import FPDF

# Brand palette — mirrors pdf_report.py
_NAVY  = (15,  23,  42)
_GOLD  = (234, 179,  8)
_SLATE = (71,  85, 105)
_LIGHT = (241, 245, 249)
_WHITE = (255, 255, 255)
_RED   = (220,  38,  38)
_GREEN = (22,  163,  74)

_TURNOVER_RATE  = 0.60   # 60% annual tenant turnover
_PET_RATE       = 0.35   # 35% of units have pets


@dataclass
class FeeStackData:
    company_name:  str
    county_name:   str
    door_count:    int          # basis for all calculations
    audit_date:    str          # e.g. "September 2026"
    latency_sec:   Optional[int] = None   # included for context line, not formula


def _calc_categories(door_count: int) -> list[tuple[str, int, str]]:
    """Return list of (category_name, annual_uplift_dollars, note)."""
    d = max(door_count, 1)
    return [
        (
            "Lease Renewal Fees",
            round(d * 150),
            "$150/door/yr - renewal admin fee typically uncollected",
        ),
        (
            "Maintenance Markups",
            round(d * 200),
            "$200/door/yr - 10% coordination markup on ~$2k avg annual maintenance",
        ),
        (
            "Tenant Setup / Move-In Fees",
            round(d * _TURNOVER_RATE * 100),
            "60% annual turnover x $100/unit admin fee",
        ),
        (
            "Pet Rent Share",
            round(d * _PET_RATE * 300),
            "35% pet units x $25/mo x 12 mo uncaptured",
        ),
        (
            "Resident Benefits Package",
            round(d * 180),
            "$15/unit/month package not currently billed",
        ),
    ]


def compile_fee_stack_pdf(data: FeeStackData) -> bytes:
    """Return 1-page branded fee-stack PDF bytes."""
    categories = _calc_categories(data.door_count)
    total_uplift = sum(amt for _, amt, _ in categories)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    W = pdf.w - 30   # usable width

    # ── Header bar ────────────────────────────────────────────────────────────
    pdf.set_fill_color(*_NAVY)
    pdf.rect(0, 0, pdf.w, 28, "F")

    pdf.set_xy(15, 6)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*_WHITE)
    pdf.cell(W, 8, "FEE-STACK OPPORTUNITY ANALYSIS", ln=True)

    pdf.set_x(15)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_GOLD)
    company_label = (data.company_name[:55] + "...") if len(data.company_name) > 55 else data.company_name
    pdf.cell(W, 5, f"{company_label}  |  {data.county_name}  |  {data.audit_date}", ln=True)

    # ── Sub-header ─────────────────────────────────────────────────────────────
    pdf.set_xy(15, 32)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_SLATE)
    pdf.multi_cell(
        W, 5,
        f"Based on {data.door_count:,} managed doors, Blackink identified "
        f"5 fee categories where revenue is being left on the table. "
        f"Estimated total annual uplift: ${total_uplift:,}.",
        align="L",
    )

    # ── Table header ──────────────────────────────────────────────────────────
    pdf.set_xy(15, 48)
    pdf.set_fill_color(*_NAVY)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("Helvetica", "B", 9)
    col_w = [80, 38, W - 118]
    pdf.cell(col_w[0], 7, "Fee Category",        border=0, fill=True, align="L")
    pdf.cell(col_w[1], 7, "Est. Annual Uplift",  border=0, fill=True, align="R")
    pdf.cell(col_w[2], 7, "Basis",               border=0, fill=True, align="L", ln=True)

    # ── Table rows ────────────────────────────────────────────────────────────
    alt = False
    for i, (name, amount, note) in enumerate(categories):
        y = pdf.get_y()
        pdf.set_fill_color(*((_LIGHT) if alt else _WHITE))
        pdf.set_text_color(*_NAVY)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(col_w[0], 8, name, border=0, fill=True, align="L")
        pdf.set_text_color(*_GREEN)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(col_w[1], 8, f"${amount:,}", border=0, fill=True, align="R")
        pdf.set_text_color(*_SLATE)
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(col_w[2], 8, note, border=0, fill=True, align="L", ln=True)
        alt = not alt

    # ── Total row ─────────────────────────────────────────────────────────────
    pdf.set_fill_color(*_NAVY)
    pdf.set_text_color(*_WHITE)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(col_w[0], 9, "TOTAL ANNUAL UPLIFT", border=0, fill=True, align="L")
    pdf.set_text_color(*_GOLD)
    pdf.cell(col_w[1], 9, f"${total_uplift:,}", border=0, fill=True, align="R")
    pdf.set_text_color(*_WHITE)
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(col_w[2], 9, "Estimated, door-count basis", border=0, fill=True, align="L", ln=True)

    # ── Disclaimer ────────────────────────────────────────────────────────────
    pdf.ln(8)
    pdf.set_x(15)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(*_SLATE)
    pdf.multi_cell(
        W, 4,
        "Estimates are illustrative and based on industry benchmarks. "
        "Actual uplift depends on current billing practices, lease mix, "
        "and market conditions. Blackink's platform tracks and captures "
        "each fee category automatically once configured.",
        align="L",
    )

    # ── Footer ────────────────────────────────────────────────────────────────
    pdf.set_xy(15, 275)
    pdf.set_fill_color(*_NAVY)
    pdf.rect(0, 272, pdf.w, 25, "F")
    pdf.set_xy(15, 276)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_GOLD)
    pdf.cell(W // 2, 5, "BLACKINK - PROPERTY MANAGEMENT GROWTH PLATFORM", align="L")
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(*_WHITE)
    pdf.cell(W // 2, 5, "getblackink.com", align="R", ln=True)
    pdf.set_x(15)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(150, 163, 180)
    pdf.cell(W, 4, "Confidential - prepared exclusively for the recipient named above.", align="L")

    return bytes(pdf.output())
