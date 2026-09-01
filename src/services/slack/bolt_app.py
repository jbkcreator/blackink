"""Slack Bolt app + Socket Mode bootstrap.

Week 0 runs Socket Mode, not HTTP webhooks — no public HTTPS endpoint yet
(DNS/TLS for blackink-master isn't set up). Socket Mode has no incoming
HTTP request at all: this process opens an outbound WebSocket to Slack
(authenticated by the App-Level Token, SLACK_APP_TOKEN) and Slack pushes
interaction/command envelopes down it. There is nothing for
src.services.slack.auth.verify_slack_signature() to check in this mode —
trust comes from possessing the app-level token to open the socket, not
from an HMAC over a request body — so that function is currently unused
while Socket Mode is active, not deleted: switching back to HTTP webhooks
later needs it again.

THIS FILE IS BOOTSTRAP ONLY — the AsyncApp instance and the Socket Mode
connection lifecycle. It does not register any @app.action / @app.command
/ @app.view listeners; those are the actual interaction handlers (the
Dev 3 plan's still-unbuilt slack_router equivalent) and belong in a
separate module that imports get_bolt_app() and decorates it.

Migration path back to HTTP webhooks (once DNS/TLS is ready): the AsyncApp
core and every listener registered on it are IDENTICAL between Socket Mode
and HTTP mode — only the receiver differs (AsyncSocketModeHandler here vs.
slack_bolt.adapter.fastapi.async_handler.AsyncSlackRequestHandler mounted
into src/api/main.py's FastAPI app). Swapping the receiver in this one
file is the whole migration; no listener code changes.
"""

from __future__ import annotations

import logging
from typing import Optional

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp

from config.settings import get_settings

logger = logging.getLogger(__name__)

_app: Optional[AsyncApp] = None
_handler: Optional[AsyncSocketModeHandler] = None


class SlackNotConfiguredError(RuntimeError):
	"""Raised when Slack bootstrap is attempted without the required
	credentials set — fails loudly and specifically (naming exactly which
	setting is missing) rather than surfacing as an opaque slack_bolt
	internal error several frames down."""


def get_bolt_app() -> AsyncApp:
	"""Lazy singleton — mirrors src.core.database.Database and
	src.core.redis_client's lazy-client pattern already used in this
	codebase. Building the AsyncApp does not itself open any network
	connection (that only happens in run_socket_mode below), so this is
	safe to call at import time without credentials present — only
	actually STARTING socket mode requires them."""
	global _app
	if _app is None:
		settings = get_settings()
		if not settings.slack_bot_token:
			raise SlackNotConfiguredError("SLACK_BOT_TOKEN is not set")
		if not settings.slack_signing_secret:
			raise SlackNotConfiguredError("SLACK_SIGNING_SECRET is not set")
		_app = AsyncApp(
			token=settings.slack_bot_token.get_secret_value(),
			signing_secret=settings.slack_signing_secret.get_secret_value(),
		)
	return _app


async def start_socket_mode() -> None:
	"""Opens the Socket Mode connection and blocks forever holding it open
	(AsyncSocketModeHandler.start_async's own behavior — connect, then
	sleep(inf)). This is a genuine run-loop, not a request handler: the
	caller MUST launch it as a background asyncio Task (see
	src/api/main.py's startup lifespan), never awaited directly, or
	startup hangs forever on this call.
	"""
	global _handler
	settings = get_settings()
	if not settings.slack_app_token:
		raise SlackNotConfiguredError("SLACK_APP_TOKEN is not set (required for Socket Mode)")

	app = get_bolt_app()
	_handler = AsyncSocketModeHandler(app, settings.slack_app_token.get_secret_value())
	logger.info("[slack.bolt_app] starting Socket Mode connection")
	await _handler.start_async()


async def run_socket_mode_task() -> None:
	"""Wrapper around start_socket_mode() for use with
	asyncio.create_task() from a FastAPI lifespan (see src/api/main.py).

	Catching SlackNotConfiguredError around create_task() itself does NOT
	work — the coroutine body (and any exception it raises) only executes
	once the event loop actually runs the task, not synchronously at
	create_task() call time, so that exception would otherwise surface
	as an unhandled "Task exception was never retrieved" warning instead
	of a clean log line. Caught and logged here instead, so a missing
	Slack config degrades (the API still starts; Slack cards can't be
	posted or clicked) rather than crashing the whole process.

	asyncio.CancelledError is deliberately NOT caught — that's how the
	lifespan's own task.cancel() on shutdown is supposed to end this
	loop; swallowing it here would break clean shutdown.
	"""
	try:
		await start_socket_mode()
	except SlackNotConfiguredError as exc:
		logger.warning("[slack.bolt_app] Socket Mode not started: %s", exc)
	except Exception:
		logger.error("[slack.bolt_app] Socket Mode connection failed", exc_info=True)


async def stop_socket_mode() -> None:
	"""Graceful shutdown counterpart — call from the same lifespan that
	launched start_socket_mode's background task, alongside cancelling
	that task (cancelling alone stops the infinite sleep but does not
	close the underlying Socket Mode client connection)."""
	global _handler
	if _handler is not None:
		logger.info("[slack.bolt_app] closing Socket Mode connection")
		await _handler.close_async()
		_handler = None
