"""PR #4 review finding 2: a missing Slack credential took the whole API
down, not just Slack.

src/api/main.py imports src.services.slack.listeners to register the Bolt
handlers, and that module bound `app = get_bolt_app()` at MODULE scope.
get_bolt_app() raises SlackNotConfiguredError without SLACK_BOT_TOKEN /
SLACK_SIGNING_SECRET — during FastAPI app CONSTRUCTION, before the
lifespan's run_socket_mode_task() error handling could log and degrade. An
optional integration outage became a total API outage, /healthz included.

These tests run the import in a SUBPROCESS with the Slack env cleared and
the working directory moved off the repo root. Both are required to
reproduce honestly: config.settings reads a .env file relative to CWD (so
a developer's local .env would otherwise mask the failure), and
config.settings.get_settings is an @lru_cache singleton constructed at
module import, so it cannot be re-pointed once this process has imported
it. A subprocess is the only way to get a genuinely cold import.
"""

import subprocess
import sys
import tempfile
import textwrap

REPO_ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[1])

_SLACK_ENV_KEYS = ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_APP_TOKEN")


def _run_without_slack(body: str) -> subprocess.CompletedProcess:
	script = textwrap.dedent(
		f"""
		import os, sys
		sys.path.insert(0, {REPO_ROOT!r})
		for key in {_SLACK_ENV_KEYS!r}:
			os.environ.pop(key, None)
		"""
	) + textwrap.dedent(body)
	with tempfile.TemporaryDirectory() as cwd:  # away from the repo's .env
		return subprocess.run(
			[sys.executable, "-c", script],
			cwd=cwd,
			capture_output=True,
			text=True,
			timeout=120,
		)


def test_api_imports_without_slack_credentials():
	result = _run_without_slack(
		"""
		import src.api.main
		print("IMPORT_OK")
		"""
	)
	assert "SlackNotConfiguredError" not in result.stderr, result.stderr
	assert "IMPORT_OK" in result.stdout, result.stderr


def test_healthz_still_serves_without_slack_credentials():
	"""The actual production impact: the API must still answer."""
	result = _run_without_slack(
		"""
		from fastapi.testclient import TestClient
		from src.api.main import app

		# TestClient(app) as a context manager runs the lifespan, which is
		# where run_socket_mode_task() is supposed to degrade gracefully.
		with TestClient(app) as client:
			response = client.get("/healthz")
			print("STATUS", response.status_code, response.json())
		"""
	)
	assert "STATUS 200 {'status': 'ok'}" in result.stdout, result.stderr


def test_listener_module_imports_and_still_defines_its_handlers():
	"""Degrading must mean 'no listeners REGISTERED with Slack', not
	'handlers undefined' — every handler is a plain function the rest of
	the codebase calls directly (work_orders.__main__ imports
	post_work_order_card, and the unit tests call the handlers)."""
	result = _run_without_slack(
		"""
		from src.services.slack import listeners
		from src.services.slack.bolt_app import _UnconfiguredApp

		assert isinstance(listeners.app, _UnconfiguredApp), type(listeners.app)
		for name in ("handle_terminal_action", "handle_snooze", "handle_revise_open",
		             "handle_revise_submit", "post_work_order_card"):
			assert callable(getattr(listeners, name)), name
		print("HANDLERS_OK")
		"""
	)
	assert "HANDLERS_OK" in result.stdout, result.stderr
