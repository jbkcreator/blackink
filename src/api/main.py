"""FastAPI entrypoint. Mirrors Forced Action's main.py convention: mounts
every router, no business logic lives here — except two things with
nowhere else to live under this repo's current single-process deploy
model (no orchestration layer, no separate container per background
concern yet):

- Runs the Slack Socket Mode connection (src.services.slack.bolt_app) as
  a background asyncio task inside this same process's lifespan — there is
  exactly one deployable process (blackink-master). Revisit if the
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
from src.api.inbound_email_router import router as inbound_email_router
from src.api.auth_router import router as auth_router
from src.api.sandbox_router import router as sandbox_router
from src.api.metrics_router import router as metrics_router
from src.api.meetings_router import router as meetings_router
from src.api.ovs_router import router as ovs_router
from src.api.booking_webhook_router import router as booking_webhook_router
from src.api.inbound_router import router as inbound_router
from src.api.calendar_oauth_router import router as calendar_oauth_router
from src.api.public_landing_router import router as public_landing_router
from src.api.inbound_lead_router import router as inbound_lead_router
from src.api.mailgun_inbound_router import router as mailgun_inbound_router
from src.api.winback_router import router as winback_router
from src.api.unsubscribe_router import router as unsubscribe_router
from src.services.events import flush_pending
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
_SHOW_RATE_REMINDER_SWEEP_INTERVAL_SECONDS = 60
# Comfortably inside the "5 minutes" no-show recovery-email budget and
# the "10 minutes" no-show-prompt window.
_NO_SHOW_PROMPT_SWEEP_INTERVAL_SECONDS = 60
_NO_SHOW_RECOVERY_SWEEP_INTERVAL_SECONDS = 60
_SELF_SERVE_AUDIT_SWEEP_INTERVAL_SECONDS = 30
_MEETING_OUTCOME_PROMPT_SWEEP_INTERVAL_SECONDS = 60
# Task 4.2.1 — 30-second tick keeps SLA response latency well under 30 min
_SPEED_TO_LEAD_SWEEP_INTERVAL_SECONDS = 30
_RESPOND_SLA_SWEEP_INTERVAL_SECONDS = 60


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
	from src.tasks.show_rate_reminder_sender import run_sweep as show_rate_reminder_sweep
	from src.tasks.no_show_prompt_sender import run_sweep as no_show_prompt_sweep
	from src.tasks.no_show_recovery_sender import run_sweep as no_show_recovery_sweep
	from src.tasks.self_serve_audit_worker import run_sweep as self_serve_audit_sweep
	from src.tasks.meeting_outcome_prompt_sender import run_sweep as meeting_outcome_prompt_sweep
	from src.tasks.speed_to_lead_sweep import run_sweep as speed_to_lead_sweep
	from src.tasks.respond_sla_sweep import run_sweep as respond_sla_sweep
	from src.agents.respond.worker import Worker as RespondWorker

	workers = [
		("calendar_sync_worker.drain_queue", _QUEUE_DRAIN_INTERVAL_SECONDS, drain_queue),
		("calendar_sync_worker.sweep_all_active_connections", _SAFETY_SWEEP_INTERVAL_SECONDS, sweep_all_active_connections),
		("booking_confirmation_sender.run_sweep", _CONFIRMATION_SWEEP_INTERVAL_SECONDS, run_sweep),
		("calendar_subscription_renewal.run_renewal_sweep", _SUBSCRIPTION_RENEWAL_INTERVAL_SECONDS, run_renewal_sweep),
		# The show-rate reminder cascade's only sender. Without this the
		# 24h/30min booking_reminder_jobs stay PENDING forever.
		("show_rate_reminder_sender.run_sweep", _SHOW_RATE_REMINDER_SWEEP_INTERVAL_SECONDS, show_rate_reminder_sweep),
		("no_show_prompt_sender.run_sweep", _NO_SHOW_PROMPT_SWEEP_INTERVAL_SECONDS, no_show_prompt_sweep),
		("no_show_recovery_sender.run_sweep", _NO_SHOW_RECOVERY_SWEEP_INTERVAL_SECONDS, no_show_recovery_sweep),
		("self_serve_audit_worker.run_sweep", _SELF_SERVE_AUDIT_SWEEP_INTERVAL_SECONDS, self_serve_audit_sweep),
		("meeting_outcome_prompt_sender.run_sweep", _MEETING_OUTCOME_PROMPT_SWEEP_INTERVAL_SECONDS, meeting_outcome_prompt_sweep),
		# Task 4.2.1 — SLA sweep dispatches deferred Speed-to-Lead auto-responses.
		("speed_to_lead_sweep.run_sweep", _SPEED_TO_LEAD_SWEEP_INTERVAL_SECONDS, speed_to_lead_sweep),
		("respond_sla_sweep.run_sweep", _RESPOND_SLA_SWEEP_INTERVAL_SECONDS, respond_sla_sweep),
	]
	for name, interval, fn in workers:
		thread = threading.Thread(target=_loop, args=(name, interval, fn), name=name, daemon=True)
		thread.start()
		logger.info("Started background worker %s (interval=%ds)", name, interval)

	# The respond worker has its own internal loop and error handling — run it
	# directly rather than wrapping in _loop(). Signal handlers are not
	# installed: only the main thread can handle signals in Python, and Cloud
	# Run SIGTERM terminates the container regardless.
	respond_worker = RespondWorker()
	respond_thread = threading.Thread(
		target=respond_worker.run_forever,
		name="respond_worker",
		daemon=True,
	)
	respond_thread.start()
	logger.info("Started background worker respond_worker")


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
	_start_background_workers()
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
app.include_router(inbound_email_router)
app.include_router(auth_router)
app.include_router(sandbox_router)
app.include_router(metrics_router)
app.include_router(meetings_router)
app.include_router(ovs_router)
app.include_router(calendar_oauth_router)
app.include_router(booking_webhook_router)
app.include_router(inbound_router)
app.include_router(public_landing_router)
app.include_router(inbound_lead_router)
app.include_router(mailgun_inbound_router)
app.include_router(winback_router)
app.include_router(unsubscribe_router)


@app.get("/healthz")
def healthz():
	return {"status": "ok"}
