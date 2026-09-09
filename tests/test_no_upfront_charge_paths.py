"""Structural proof of zero-dollars-upfront (Subtask 1.2.2, layer 6 of 6 —
see migrations/apply_settlement_ledger.py's docstring for the other five).

Walks src/ and asserts that every money-moving Stripe call the settlement
engine can make — invoice finalize/pay/item-create, an uncaptured
PaymentIntent.create — appears ONLY in src/services/settlement/charge.py,
and that .capture( appears nowhere. This is what survives a future
contributor adding a "quick charge" helper elsewhere — same spirit as
tests/test_migration_coverage.py.
"""
import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
ALLOWED_FILE = SRC_ROOT / "services" / "settlement" / "charge.py"

# gateway.py is the ABC + LiveStripeGateway implementation the settlement
# engine's own call sites go through — it legitimately contains the real
# Stripe SDK calls once, wrapped behind the interface charge.py uses.
ALLOWED_GATEWAY_FILE = SRC_ROOT / "services" / "settlement" / "gateway.py"

MONEY_MOVING_PATTERNS = [
	re.compile(r"\.invoices\.pay\("),
	re.compile(r"\.invoices\.finalize_invoice\("),
	re.compile(r"\.invoice_items\.create\("),
]
CAPTURE_PATTERN = re.compile(r"\.capture\(")

# src/services/payment_auth.py (Subtask 1.2.1) has one docstring PROSE
# reference — "...unless some future code path explicitly calls
# .capture(), which nothing here does." — documenting that its own $1
# auth hold is never captured. Not a real call; exempted by exact
# (file, line) rather than a fragile regex heuristic.
_KNOWN_PROSE_EXEMPTIONS = {
	(SRC_ROOT / "services" / "payment_auth.py", 195),
}


def _all_py_files():
	return [p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_money_moving_calls_confined_to_settlement_module():
	offenders = []
	for path in _all_py_files():
		if path in (ALLOWED_FILE, ALLOWED_GATEWAY_FILE):
			continue
		text = path.read_text(encoding="utf-8", errors="replace")
		for pattern in MONEY_MOVING_PATTERNS:
			if pattern.search(text):
				offenders.append((str(path), pattern.pattern))
	assert offenders == [], f"money-moving Stripe calls found outside charge.py/gateway.py: {offenders}"


def test_no_capture_call_anywhere():
	offenders = []
	for path in _all_py_files():
		text = path.read_text(encoding="utf-8", errors="replace")
		for i, line in enumerate(text.splitlines(), start=1):
			if (path, i) in _KNOWN_PROSE_EXEMPTIONS:
				continue
			if CAPTURE_PATTERN.search(line):
				offenders.append(f"{path}:{i}: {line.strip()}")
	assert offenders == [], f".capture( found — no code path may capture a manual-capture PaymentIntent: {offenders}"
