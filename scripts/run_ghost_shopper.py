"""Manual test script for the Ghost Shopper subagent.

Runs Ghost Shopper directly — no Ink worker, no Redis, no IMAP.
Points the crawl at a real PM website and prints the result.

Usage:
    PYTHONPATH=. python scripts/run_ghost_shopper.py [website_url]

    # default URL hardcoded below if none passed
    PYTHONPATH=. python scripts/run_ghost_shopper.py https://some-pm-company.com

Requires:
    ANTHROPIC_API_KEY and DATABASE_URL in .env (or environment)
"""
import logging
import sys
import uuid

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

from dotenv import load_dotenv
load_dotenv()

from config.settings import get_settings
from langgraph.checkpoint.postgres import PostgresSaver

from src.agents.ink.subagents.ghost_shopper.graph import build_graph
from src.agents.ink.subagents.ghost_shopper.runner import run

# ── config ────────────────────────────────────────────────────────────────────

DEFAULT_URL  = "https://www.greystar.com"   # swap to the PM site you want to test
COMPANY_ID   = "test-company-01"
WORK_ORDER_ID = str(uuid.uuid4())           # fresh thread each run (no checkpoint reuse)

# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    website_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    settings    = get_settings()

    print(f"\n{'─'*60}")
    print(f"  Ghost Shopper test run")
    print(f"  URL:           {website_url}")
    print(f"  company_id:    {COMPANY_ID}")
    print(f"  work_order_id: {WORK_ORDER_ID}")
    print(f"{'─'*60}\n")

    with PostgresSaver.from_conn_string(settings.database_url) as checkpointer:
        checkpointer.setup()
        graph  = build_graph(checkpointer=checkpointer)
        result = run(
            graph=graph,
            company_id=COMPANY_ID,
            work_order_id=WORK_ORDER_ID,
            website_url=website_url,
        )

    print(f"\n{'─'*60}")
    print(f"  outcome:      {result.outcome}")
    print(f"  submitted_at: {result.submitted_at}")
    print(f"  error:        {result.error}")
    print(f"{'─'*60}\n")

    return 0 if result.outcome in ("SUBMITTED", "FORM_NOT_FOUND") else 1


if __name__ == "__main__":
    raise SystemExit(main())
