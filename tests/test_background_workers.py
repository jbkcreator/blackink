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


def test_respond_worker_llm_guard_runs_before_any_thread_starts():
	"""PR #48 review finding: the guard used to run AFTER the `workers` loop
	had already started all 18 sweep threads (billing, settlement, etc.) —
	each of which calls its sweep function immediately on thread start, so a
	misconfigured ANTHROPIC_API_KEY still let those threads run at least one
	real tick (real Stripe calls included) before the RuntimeError ever
	propagated. Threads are daemons and are not killed by an exception on a
	different thread, so "the guard eventually raises" was not the same as
	"nothing ran". Assert the guard is called before the FIRST thread of any
	kind is even constructed, not merely before the respond worker's own
	thread specifically."""
	names = []
	guard_called_before_first_thread = []

	class _StubThread:
		def __init__(self, *args, target=None, name=None, daemon=None, **kwargs):
			if not names:  # this is the first thread constructed
				guard_called_before_first_thread.append(mock_guard.called)
			if name:
				names.append(name)

		def start(self):
			pass

	with patch.object(main.threading, "Thread", _StubThread), \
	     patch("src.agents.respond.worker._assert_llm_configured") as mock_guard:
		main._start_background_workers()

	mock_guard.assert_called_once()
	assert guard_called_before_first_thread == [True]
	assert len(names) > 1, "sanity check: multiple worker threads should have been registered"


def test_no_worker_thread_starts_when_llm_misconfigured():
	"""The guard raising must abort _start_background_workers() before ANY
	worker thread is constructed — not just before the respond worker's own
	thread. Before this fix, the guard ran after all 18 sweep threads
	(including billing_sweep.run_sit_invoice_sweep and every settlement_sweep
	sweep) had already started and fired at least one real tick, since
	daemon threads are not killed by an exception raised on a different
	thread. A partially-started worker set — real money-moving sweeps
	included — on a misconfigured deployment is itself a silent-failure
	surface this guard exists to prevent entirely, not just for the respond
	worker."""
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

	assert names == [], f"no worker thread should have started, but these did: {names}"
