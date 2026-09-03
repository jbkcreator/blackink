"""FastAPI entrypoint. Mirrors Forced Action's main.py convention: mounts
every router, no business logic lives here — except two things with
nowhere else to live under this repo's current single-process deploy
model (no orchestration layer, no separate container per background
concern yet):

- Runs the Slack Socket Mode connection (src.services.slack.bolt_app) as
  a background asyncio task inside this same process's lifespan — Week 0
  has exactly one deployable process (blackink-master). Revisit if the
  API needs to restart independently of the Slack connection, or the API
  scales horizontally (each instance would otherwise open a redundant
  socket).
- Starts the booking-engine background workers
  (calendar_sync_worker.py / booking_confirmation_sender.py /
  calendar_subscription_renewal.py) as background threads — under Cloud
  Run's request-driven model (see CLAUDE.md's Cloud Run section), a
  min-instances=1, CPU-always-allocated deployment is what keeps these
  actually running, same one-process-does-everything posture as the
  Slack task above.
"""

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.agents.relay.sync import sync_halts_from_db
from src.api.akrash_ingest_router import router as akrash_router
from src.api.booking_webhook_router import router as booking_webhook_router
from src.api.calendar_oauth_router import router as calendar_oauth_router
from src.services.slack import listeners  # noqa: F401 — import registers the Bolt @app.* listeners
from src.services.slack.bolt_app import run_socket_mode_task, stop_socket_mode

# Python's root logger defaults to WARNING — without this, every
# logger.info() in the background workers (including each successful
# sweep tick) is silently dropped, which looks identical to "the worker
# isn't running" from the console. uvicorn configures its own loggers
# independently of this, so this is still needed for app-level logging.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_QUEUE_DRAIN_INTERVAL_SECONDS = 10
_SAFETY_SWEEP_INTERVAL_SECONDS = 300
_CONFIRMATION_SWEEP_INTERVAL_SECONDS = 30
_SUBSCRIPTION_RENEWAL_INTERVAL_SECONDS = 3600


def _loop(name: str, interval_seconds: int, fn) -> None:
	while True:
		try:
			fn()
		except Exception:
			logger.exception("%s: worker tick failed", name)
		time.sleep(interval_seconds)


def _start_background_workers() -> None:
	from src.tasks.booking_confirmation_sender import run_sweep
	from src.tasks.calendar_subscription_renewal import run_renewal_sweep
	from src.tasks.calendar_sync_worker import drain_queue, sweep_all_active_connections

	workers = [
		("calendar_sync_worker.drain_queue", _QUEUE_DRAIN_INTERVAL_SECONDS, drain_queue),
		("calendar_sync_worker.sweep_all_active_connections", _SAFETY_SWEEP_INTERVAL_SECONDS, sweep_all_active_connections),
		("booking_confirmation_sender.run_sweep", _CONFIRMATION_SWEEP_INTERVAL_SECONDS, run_sweep),
		("calendar_subscription_renewal.run_renewal_sweep", _SUBSCRIPTION_RENEWAL_INTERVAL_SECONDS, run_renewal_sweep),
	]
	for name, interval, fn in workers:
		thread = threading.Thread(target=_loop, args=(name, interval, fn), name=name, daemon=True)
		thread.start()
		logger.info("Started background worker %s (interval=%ds)", name, interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
	# Restore Redis halt state from Postgres before accepting any traffic —
	# src.agents.relay.sync's own docstring: "without this, a Redis flush
	# would silently clear all active halts until an admin noticed."
	sync_halts_from_db()
	slack_task = asyncio.create_task(run_socket_mode_task())
	_start_background_workers()
	yield
	slack_task.cancel()
	try:
		await slack_task
	except asyncio.CancelledError:
		pass
	await stop_socket_mode()


app = FastAPI(title="Blackink API", lifespan=lifespan)

app.include_router(akrash_router)
app.include_router(calendar_oauth_router)
app.include_router(booking_webhook_router)


@app.get("/healthz")
def healthz():
	return {"status": "ok"}
