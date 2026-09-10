"""Guards that every scheduled worker the deployed app depends on is
actually registered by _start_background_workers().

Regression for the show-rate reminder cascade (Subtask 3.2.2) shipping with
its only sender (show_rate_reminder_sender.run_sweep) unregistered — the
Docker image runs Uvicorn alone, so an unregistered sweep never runs and
every 24h/30min booking_reminder_jobs row stays PENDING forever.
"""
from unittest.mock import patch

import pytest

from src.api import main


def _registered_worker_names():
	names = []

	class _StubThread:
		def __init__(self, *args, target=None, name=None, daemon=None, **kwargs):
			if name:
				names.append(name)

		def start(self):
			pass

	# Group D / D-10: _start_background_workers() now calls
	# _assert_llm_configured() before spawning the respond worker thread
	# (a real production credential check) — stub it here since this test's
	# purpose is "is every worker registered," not "is ANTHROPIC_API_KEY set
	# in the test environment."
	with patch.object(main.threading, "Thread", _StubThread), \
	     patch("src.agents.respond.worker._assert_llm_configured"):
		main._start_background_workers()
	return names


def test_show_rate_reminder_sender_is_registered():
	assert "show_rate_reminder_sender.run_sweep" in _registered_worker_names()


def test_core_booking_workers_are_registered():
	names = _registered_worker_names()
	for expected in (
		"calendar_sync_worker.drain_queue",
		"booking_confirmation_sender.run_sweep",
		"calendar_subscription_renewal.run_renewal_sweep",
		"show_rate_reminder_sender.run_sweep",
	):
		assert expected in names, f"{expected} not registered"


def test_respond_worker_llm_guard_runs_before_thread_starts():
	"""Group D / D-10 review finding: src/agents/respond/worker.py's own
	main()/_assert_llm_configured() is NEVER reached by this codebase's
	actual deployment — this single-process model instantiates RespondWorker()
	directly inside _start_background_workers(), not via `python -m
	src.agents.respond.worker`. Without a guard call HERE, a missing
	ANTHROPIC_API_KEY silently starts a worker that classifies every reply
	as NURTURE/ROUTED with zero alert. Assert the guard actually fires on
	the real production start path, and that it runs BEFORE the respond
	worker thread is spawned (a misconfiguration must abort the whole
	startup, not race a thread that's already running)."""
	names = []
	guard_called_before_thread_started = []

	class _StubThread:
		def __init__(self, *args, target=None, name=None, daemon=None, **kwargs):
			self._name = name
			if name:
				names.append(name)

		def start(self):
			if self._name == "respond_worker":
				guard_called_before_thread_started.append(mock_guard.called)

	with patch.object(main.threading, "Thread", _StubThread), \
	     patch("src.agents.respond.worker._assert_llm_configured") as mock_guard:
		main._start_background_workers()

	mock_guard.assert_called_once()
	assert guard_called_before_thread_started == [True]


def test_respond_worker_thread_never_starts_when_llm_misconfigured():
	"""The guard raising must abort _start_background_workers() entirely —
	including every OTHER worker registered after it in the function — not
	just skip the respond worker. A partially-started worker set on a
	misconfigured deployment is itself a silent-failure surface."""
	names = []

	class _StubThread:
		def __init__(self, *args, target=None, name=None, daemon=None, **kwargs):
			if name:
				names.append(name)

		def start(self):
			pass

	with patch.object(main.threading, "Thread", _StubThread), \
	     patch("src.agents.respond.worker._assert_llm_configured",
	           side_effect=RuntimeError("ANTHROPIC_API_KEY is not set")):
		with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
			main._start_background_workers()

	assert "respond_worker" not in names
