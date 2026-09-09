"""Adds settlement_transactions.inst{1,2}_reopen_count (PR #37 second review,
re-review findings 1/3 — closing the "reopened retry replays the stale
payment method and idempotency key" gap).

reopen_failed_permanent_installment() (src/services/settlement/ledger.py)
resets `inst{N}_attempts` back to 0 to give a reopened installment a fresh
3-attempt budget. Without a SEPARATE counter, the very next pay attempt
rebuilds the pay-attempt idempotency key `settlement-pay-ach|...|a1` —
byte-identical to the ORIGINAL first attempt's key. Inside Stripe's ~24h
idempotency-key retention window, Stripe replays the ORIGINAL cached
decline instead of making a real charge attempt against whatever payment
method the client actually fixed. `reopen_count` is folded into that key
(`|r{reopen_count}a{attempts}`) and is only ever incremented, never reset,
so a second/third reopen still produces a fresh key too.

Idempotent: ADD COLUMN IF NOT EXISTS throughout.

    PYTHONPATH=. python migrations/apply_settlement_reopen_count.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    "ALTER TABLE settlement_transactions ADD COLUMN IF NOT EXISTS inst1_reopen_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE settlement_transactions ADD COLUMN IF NOT EXISTS inst2_reopen_count INTEGER NOT NULL DEFAULT 0",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'settlement_transactions' AND column_name LIKE '%reopen_count'"
            )
        ).fetchall()
    print(f"apply_settlement_reopen_count: done — columns present={[c[0] for c in cols]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
