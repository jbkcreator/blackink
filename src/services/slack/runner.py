"""Standalone Slack Socket Mode runner — no FastAPI required.

Starts the Bolt app and holds the Socket Mode WebSocket open so Slack can
deliver button clicks, modal submits, and action callbacks to this process.

Run:
    PYTHONPATH=. python -m src.services.slack.runner
"""
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

import src.services.slack.listeners  # noqa: F401 — registers all @app.action handlers

from src.services.slack.bolt_app import run_socket_mode_task


async def main() -> None:
    await run_socket_mode_task()


if __name__ == "__main__":
    asyncio.run(main())
