"""Fee-Stack One-Pager Generator (ADD-8-Lite, Subtask 2.2.1).

Produces a 1-page branded PDF mapping uncollected fee revenue for a target PM firm.

    pdf_bytes = compile_fee_stack_pdf(data)

No I/O - caller is responsible for writing bytes to disk or object storage.
All strings passed to fpdf2 cells must be ASCII-safe (latin-1 only).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from fpdf import FPDF

# Brand palette (RGB) - matches pdf_report.py
_NAVY  = (15,  23,  42)
_GOLD  = (234, 179,  8)
_SLATE = (71,  85, 105)
_LIGHT = (241, 245, 249)
_WHITE = (255, 255, 255)
_RED   = (220,  38,  38)
_GREEN = (22,  163,  74)

# Page layout constants (US Letter, portrait)
_PW   = 216.0   # page width mm
_PH   = 279.0   # page height mm
_LM   = 16.0    # left margin
_RM   = 16.0    # right margin
_CW   = _PW - _LM - _RM   # content width


@dataclass(frozen=True)
class FeeCategory:
    key: str
    label: str
    description: str
    assumption_text: str  # shown as "Industry benchmark: ..." line
    annual_uplift: float  # computed dollars


@dataclass
class FeeStackData:
    company_name: str
    county_name: str
    door_count: int
    month_label: str = ""

    # Per-unit assumptions (overridable)
    lease_renewal_fee: float = 200.0        # $ per renewal
    renewal_rate_pct: float = 0.80          # fraction of doors that turn annually
    maintenance_markup_pct: float = 0.10    # markup fraction on vendor invoices
    maintenance_spend_per_door: float = 500.0  # $ in vendor invoices per door/year
    tenant_setup_fee: float = 150.0         # $ per new lease
    pet_rent_monthly: float = 35.0          # $ per pet per month
    pet_occupancy_pct: float = 0.30         # fraction of units with pets
    rbp_monthly_per_door: float = 15.0      # $ resident benefits package per door/month


def _build_categories(data: FeeStackData) -> list[FeeCategory]:
    d = data.door_count
    renewals = d * data.renewal_rate_pct

    lease_renewal = renewals * data.lease_renewal_fee
    maintenance   = d * data.maintenance_spend_per_door * data.maintenance_markup_pct
    tenant_setup  = renewals * data.tenant_setup_fee
    pet_rent      = d * data.pet_occupancy_pct * data.pet_rent_monthly * 12
    rbp           = d * data.rbp_monthly_per_door * 12

    return [
        FeeCategory(
            key="lease_renewal",
            label="Lease Renewal Fees",
            description=(
                "A flat fee charged to renewing tenants at lease turn. "
                "Most operators collect $150-$250 per renewal; many collect nothing."
            ),
            assumption_text=(
                f"${data.lease_renewal_fee:.0f}/renewal x "
                f"{data.renewal_rate_pct*100:.0f}% annual turnover x "
                f"{d} doors"
            ),
            annual_uplift=lease_renewal,
        ),
        FeeCategory(
            key="maintenance_markup",
            label="Maintenance Coordination Markup",
            description=(
                "A standard 10% coordination fee on third-party vendor invoices "
                "covers PM overhead. Firms that pass invoices through at cost leave "
                "this margin on the table."
            ),
            assumption_text=(
                f"${data.maintenance_spend_per_door:.0f}/door/yr vendor spend x "
                f"{data.maintenance_markup_pct*100:.0f}% markup x "
                f"{d} doors"
            ),
            annual_uplift=maintenance,
        ),
        FeeCategory(
            key="tenant_setup",
            label="Tenant Setup / Admin Fees",
            description=(
                "A one-time admin fee per new lease covers screening, document "
                "preparation, and move-in coordination. Often waived or underpriced."
            ),
            assumption_text=(
                f"${data.tenant_setup_fee:.0f}/new lease x "
                f"{data.renewal_rate_pct*100:.0f}% turn rate x "
                f"{d} doors"
            ),
            annual_uplift=tenant_setup,
        ),
        FeeCategory(
            key="pet_rent",
            label="Pet Rent Revenue Share",
            description=(
                "Pet rent ($25-$50/pet/month) is collected from tenants but "
                "rarely passed to the PM. Retaining a share is standard practice "
                "in institutional PM contracts."
            ),
            assumption_text=(
                f"${data.pet_rent_monthly:.0f}/pet/month x "
                f"{data.pet_occupancy_pct*100:.0f}% occupancy x "
                f"{d} doors x 12 months"
            ),
            annual_uplift=pet_rent,
        ),
        FeeCategory(
            key="rbp",
            label="Resident Benefits Package",
            description=(
                "A bundled $10-$25/door/month program covering renters insurance "
                "enrollment, HVAC filter delivery, and credit-building reporting. "
                "Margin is retained by the PM after vendor cost."
            ),
            assumption_text=(
                f"${data.rbp_monthly_per_door:.0f}/door/month x "
                f"{d} doors x 12 months"
            ),
            annual_uplift=rbp,
        ),
    ]


def _fmt_dollars(amount: float) -> str:
    """Format as $X,XXX with no decimals."""
    return f"${amount:,.0f}"


class _FeeDoc(FPDF):
    def __init__(self) -> None:
        super().__init__(unit="mm", format="Letter")
        self.set_margins(_LM, 10, _RM)
        self.set_auto_page_break(auto=False)

    def header(self) -> None:
        pass  # manual header drawn in compile function

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*_SLATE)
        self.cell(0, 4, "getblackink.com  |  Fee estimates use industry benchmarks. Not a financial audit.", align="C")

    def _set_fill(self, rgb: tuple[int, int, int]) -> None:
        self.set_fill_color(*rgb)

    def _set_text(self, rgb: tuple[int, int, int]) -> None:
        self.set_text_color(*rgb)


def compile_fee_stack_pdf(data: FeeStackData) -> bytes:
    """Return a 1-page branded Fee-Stack One-Pager as raw PDF bytes."""
    categories = _build_categories(data)
    total_uplift = sum(c.annual_uplift for c in categories)

    doc = _FeeDoc()
    doc.add_page()

    # --- Header bar ---
    doc.set_fill_color(*_NAVY)
    doc.rect(0, 0, _PW, 28, style="F")

    doc.set_y(5)
    doc.set_x(_LM)
    doc.set_font("Helvetica", "B", 15)
    doc.set_text_color(*_WHITE)
    name = data.company_name[:55] + ("..." if len(data.company_name) > 55 else "")
    doc.cell(_CW * 0.65, 7, name, ln=0)

    doc.set_font("Helvetica", "", 8)
    doc.set_text_color(*_GOLD)
    tag = "BLACKINK  |  FEE OPPORTUNITY ANALYSIS"
    doc.cell(_CW * 0.35, 7, tag, align="R", ln=1)

    doc.set_x(_LM)
    doc.set_font("Helvetica", "B", 10)
    doc.set_text_color(*_GOLD)
    doc.cell(_CW, 5, "Fee-Stack Revenue Opportunity Report", ln=1)

    doc.set_x(_LM)
    doc.set_font("Helvetica", "", 8)
    doc.set_text_color(*_WHITE)
    sub = data.county_name
    if data.month_label:
        sub += "  |  " + data.month_label
    sub += f"  |  {data.door_count} doors under management"
    doc.cell(_CW, 4, sub, ln=1)

    # --- Intro text ---
    doc.set_y(33)
    doc.set_x(_LM)
    doc.set_font("Helvetica", "", 8.5)
    doc.set_text_color(*_SLATE)
    intro = (
        "The five revenue lines below are standard in institutional property management contracts "
        "but are routinely left uncollected or underpriced. Based on your estimated portfolio of "
        f"{data.door_count} doors, the combined annual opportunity is shown at right."
    )
    doc.multi_cell(_CW, 4.5, intro)

    # --- Total uplift banner ---
    doc.ln(2)
    doc.set_fill_color(*_LIGHT)
    doc.rect(_LM, doc.get_y(), _CW, 14, style="F")
    doc.set_fill_color(*_GREEN)
    doc.rect(_LM, doc.get_y(), 3, 14, style="F")

    y_banner = doc.get_y()
    doc.set_xy(_LM + 6, y_banner + 2)
    doc.set_font("Helvetica", "", 8)
    doc.set_text_color(*_SLATE)
    doc.cell(80, 5, "Estimated Additional Annual Revenue", ln=0)

    doc.set_xy(_LM + 6, y_banner + 7)
    doc.set_font("Helvetica", "B", 8)
    doc.set_text_color(*_SLATE)
    doc.cell(80, 5, "if all five categories are activated", ln=0)

    doc.set_xy(_LM + 90, y_banner + 1)
    doc.set_font("Helvetica", "B", 20)
    doc.set_text_color(*_GREEN)
    doc.cell(_CW - 96, 12, _fmt_dollars(total_uplift), align="R", ln=1)

    # --- Category rows ---
    doc.ln(4)
    col_label_w = _CW * 0.44
    col_desc_w  = _CW * 0.33
    col_amt_w   = _CW * 0.23

    # Column headers
    doc.set_x(_LM)
    doc.set_font("Helvetica", "B", 7)
    doc.set_text_color(*_SLATE)
    doc.set_fill_color(*_NAVY)
    doc.set_text_color(*_WHITE)
    doc.cell(col_label_w, 5, "  Fee Category", fill=True, ln=0)
    doc.cell(col_desc_w,  5, "What It Is", fill=True, ln=0)
    doc.cell(col_amt_w,   5, "Annual Uplift", fill=True, align="R", ln=1)

    row_h_label = 5.0
    row_h_desc  = 4.0

    for i, cat in enumerate(categories):
        bg = _LIGHT if i % 2 == 0 else _WHITE
        y_row = doc.get_y()

        # Estimate row height needed (multi_cell for description)
        doc.set_font("Helvetica", "", 7.5)
        lines_desc = max(1, math.ceil(len(cat.description) / 42))
        lines_assump = 1
        row_h = max(12.0, (lines_desc + lines_assump) * row_h_desc + 3)

        doc.set_fill_color(*bg)
        doc.rect(_LM, y_row, _CW, row_h, style="F")

        # Accent bar
        doc.set_fill_color(*_GOLD)
        doc.rect(_LM, y_row, 2, row_h, style="F")

        # Label column
        doc.set_xy(_LM + 4, y_row + 2)
        doc.set_font("Helvetica", "B", 8.5)
        doc.set_text_color(*_NAVY)
        doc.cell(col_label_w - 4, row_h_label, cat.label, ln=0)

        doc.set_xy(_LM + 4, y_row + row_h_label + 2)
        doc.set_font("Helvetica", "I", 6.5)
        doc.set_text_color(*_SLATE)
        doc.cell(col_label_w - 4, 4, "Assumption: " + cat.assumption_text[:65], ln=0)

        # Description column
        doc.set_xy(_LM + col_label_w + 2, y_row + 2)
        doc.set_font("Helvetica", "", 7)
        doc.set_text_color(*_SLATE)
        doc.multi_cell(col_desc_w - 2, row_h_desc, cat.description)

        # Amount column
        doc.set_xy(_LM + col_label_w + col_desc_w, y_row + (row_h / 2) - 4)
        doc.set_font("Helvetica", "B", 11)
        doc.set_text_color(*_GREEN)
        doc.cell(col_amt_w, 8, _fmt_dollars(cat.annual_uplift), align="R", ln=0)

        doc.set_y(y_row + row_h + 1)

    # --- Disclaimer ---
    doc.ln(4)
    doc.set_x(_LM)
    doc.set_font("Helvetica", "I", 6.5)
    doc.set_text_color(*_SLATE)
    disclaimer = (
        "All estimates use industry benchmark assumptions and are illustrative only. "
        "Actual uplift will vary by market, lease terms, and current contract language. "
        "Blackink does not guarantee any specific financial outcome."
    )
    doc.multi_cell(_CW, 3.5, disclaimer)

    return bytes(doc.output())
