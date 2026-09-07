"""Evidence Packet PDF renderer (Subtask 1.2.2).

compile_packet(data) -> bytes, no I/O — mirrors
src/services/owner_visibility/pdf_report.py's compile_pdf() contract
exactly (caller writes bytes to disk/object storage). Reuses that file's
FPDF-subclass idioms (header/footer/draw_section_rule, brand palette) so
the two documents read as one brand, but is a genuinely different 4-section
layout — the OVS report's 2-page layout is not reusable for this content.
"""
from __future__ import annotations

from fpdf import FPDF

from src.services.settlement.evidence import EvidencePacketData, EvidenceSection

_NAVY = (15, 23, 42)
_GOLD = (234, 179, 8)
_SLATE = (71, 85, 105)
_RED = (220, 38, 38)
_WHITE = (255, 255, 255)


class _EvidenceDoc(FPDF):
	def header(self) -> None:
		self.set_fill_color(*_NAVY)
		self.rect(0, 0, self.w, 18, style="F")
		self.set_xy(12, 4)
		self.set_font("Helvetica", "B", 13)
		self.set_text_color(*_WHITE)
		self.cell(60, 10, "BLACKINK - EVIDENCE PACKET", ln=False)
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
			"This packet reflects records held in Blackink's system of record at the time of "
			"charge. | getblackink.com",
			align="C",
		)

	def draw_section_rule(self, y: float, label: str) -> float:
		self.set_draw_color(*_NAVY)
		self.set_line_width(0.4)
		self.line(12, y, self.w - 12, y)
		self.set_xy(12, y + 1)
		self.set_font("Helvetica", "B", 10)
		self.set_text_color(*_SLATE)
		self.cell(0, 5, label.upper())
		return y + 8


def _render_section(doc: _EvidenceDoc, section: EvidenceSection, y: float) -> float:
	y = doc.draw_section_rule(y, f"Section {section.number}: {section.title}")
	doc.set_font("Helvetica", "", 9)
	for f in section.fields:
		doc.set_xy(12, y)
		doc.set_text_color(*_SLATE)
		doc.cell(70, 5, f.label + ":")
		doc.set_text_color(15, 23, 42)
		value = f.value if f.value is not None else "NOT RECORDED"
		doc.cell(0, 5, value)
		y += 5
		doc.set_xy(82, y - 5)
		doc.set_font("Helvetica", "I", 6)
		doc.set_text_color(*_SLATE)
		doc.cell(0, 4, f"source: {f.source_table}")
		doc.set_font("Helvetica", "", 9)
		y += 4
	for gap in section.gaps:
		doc.set_xy(12, y)
		doc.set_font("Helvetica", "B", 8)
		doc.set_text_color(*_RED)
		doc.multi_cell(0, 5, f"DATA GAP: {gap}" if not gap.startswith(("DATA GAP", "SOURCE", "PMS")) else gap)
		y = doc.get_y() + 2
	return y + 4


def compile_packet(data: EvidencePacketData) -> bytes:
	doc = _EvidenceDoc(format="Letter")
	doc.set_auto_page_break(auto=True, margin=15)
	doc.add_page()
	doc.set_xy(12, 22)
	doc.set_font("Helvetica", "B", 12)
	doc.set_text_color(*_NAVY)
	doc.cell(
		0, 8,
		f"Settlement Transaction #{data.transaction_id} - Installment {data.installment}"
		f" ({data.company_name or 'unknown company'})",
	)
	y = 34
	for section in data.sections:
		y = _render_section(doc, section, y)

	out = doc.output()
	return bytes(out)
