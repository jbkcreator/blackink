"""
Add custom_hook_text to winback_rows (S-14 — Master Data Contract §D).

audit_loss_dollars_est already exists (apply_winback_touch_sequence.py:68)
but was never populated. custom_hook_text is the pre-rendered hook sentence
derived from audit_loss_dollars_est + county name, stored so callers (Slack
cards, sequence content) need only read one column rather than re-computing.

Both are populated by src/services/winback_loss_est.py's waterfall:
  ghost_shopper_replies.loss_est → OVS score-derived → fixed $1,200 fallback.

Idempotent: ADD COLUMN IF NOT EXISTS.
Run after apply_winback_touch_sequence.py, before apply_rls_policies.py.

    PYTHONPATH=. python migrations/apply_winback_loss_est.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    "ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS custom_hook_text TEXT",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'winback_rows' "
                "  AND column_name IN ('audit_loss_dollars_est', 'custom_hook_text') "
                "ORDER BY column_name"
            )
        ).fetchall()
    print("apply_winback_loss_est: done —", [c.column_name for c in cols])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
