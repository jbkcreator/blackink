"""FastAPI entrypoint. Mirrors Forced Action's main.py convention: mounts
every router, no business logic lives here.

Runs the Slack Socket Mode connection (src.services.slack.bolt_app) as a
background task inside this same process's lifespan, rather than as a
separate service/container — Week 0 has exactly one deployable process
(blackink-master) and no orchestration layer yet, so one fewer container
to add, deploy, and separately monitor. Revisit if the API needs to
restart independently of the Slack connection, or the API scales
horizontally (each instance would otherwise open a redundant socket)."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.agents.relay.sync import sync_halts_from_db
from src.api.akrash_ingest_router import router as akrash_router
from src.services.events import flush_pending
from src.services.slack import listeners  # noqa: F401 — import registers the Bolt @app.* listeners
from src.services.slack.bolt_app import run_socket_mode_task, stop_socket_mode

logger = logging.getLogger(__name__)

# PR review finding: src.services.events._pending_buffer is process-local,
# and the only other caller of flush_pending() (src/tasks/daily_digest.py)
# runs in a SEPARATE cron process with its own empty buffer — it can never
# see or drain what THIS process buffered. This loop is the minimum fix:
# whatever this process buffers, this process eventually retries itself.
# It does NOT survive this process crashing or restarting (the buffer is
# in-memory) — that gap is the documented upgrade path on _pending_buffer's
# own ponytail comment (a durable outbox), deliberately out of scope here.
_FLUSH_INTERVAL_SECONDS = 300  # 5 minutes


async def _periodic_flush_loop(interval_seconds: float = _FLUSH_INTERVAL_SECONDS) -> None:
	while True:
		await asyncio.sleep(interval_seconds)
		try:
			flushed = flush_pending()
			if flushed:
				logger.info("[main] periodic flush drained %d buffered event(s)", flushed)
		except Exception:
			# A failed flush attempt must never kill this loop — the next
			# tick tries again, and the buffer's own bound is what protects
			# memory if the DB stays down longer than that.
			logger.error("[main] periodic flush_pending() failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
	# Restore Redis halt state from Postgres before accepting any traffic —
	# src.agents.relay.sync's own docstring: "without this, a Redis flush
	# would silently clear all active halts until an admin noticed."
	sync_halts_from_db()
	slack_task = asyncio.create_task(run_socket_mode_task())
	flush_task = asyncio.create_task(_periodic_flush_loop())
	yield
	slack_task.cancel()
	flush_task.cancel()
	try:
		await slack_task
	except asyncio.CancelledError:
		pass
	try:
		await flush_task
	except asyncio.CancelledError:
		pass
	await stop_socket_mode()


app = FastAPI(title="Blackink API", lifespan=lifespan)

app.include_router(akrash_router)


@app.get("/healthz")
def healthz():
	return {"status": "ok"}
