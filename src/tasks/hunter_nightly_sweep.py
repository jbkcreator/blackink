"""Hunter nightly sweep entry point.

Usage:
    python -m src.tasks.hunter_nightly_sweep

Runs one full entity-resolution pass over all unresolved llc_portfolio_owner
companies. Exits 0 on success, 1 on fatal error.
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)


def main() -> int:
    from src.agents.hunter.worker import run_sweep

    try:
        resolved = run_sweep()
        logger.info("hunter_nightly_sweep: done, resolved=%d", resolved)
        return 0
    except Exception:
        logger.exception("hunter_nightly_sweep: fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
