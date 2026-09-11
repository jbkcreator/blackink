"""Guards that every scheduled worker the deployed app depends on is
actually registered by _start_background_workers().

Regression for the show-rate reminder cascade (Subtask 3.2.2) shipping with
its only sender (show_rate_reminder_sender.run_sweep) unregistered — the
Docker image runs Uvicorn alone, so an unregistered sweep never runs and
every 24h/30min booking_reminder_jobs row stays PENDING forever.
"""
from unittest.mock import patch

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


def test_vera_health_sweep_is_registered():
	"""S-1 regression guard, same class as the show-rate reminder one above:
	settlement_sweep.py / billing_sweep.py's gate reads vera_health_runs, but
	nothing writes to it unless this worker actually runs in the deployed
	process — an unregistered health sweep means every gated sweep halts
	forever on NO_HEALTH_RUN, silently freezing settlement and billing."""
	assert "vera_health_sweep.run_sweep" in _registered_worker_names()


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


def test_only_respond_worker_is_skipped_when_llm_misconfigured():
	"""Corrected behavior (was: the guard raising aborted the ENTIRE
	function, so /healthz and all 18 unrelated sweep threads — billing,
	settlement, booking sync — never started either; this is the exact
	PR #4 bug already fixed once for a missing Slack credential, see
	tests/test_api_startup.py's docstring). ANTHROPIC_API_KEY only gates
	the respond worker: every other worker thread must still start, and
	_start_background_workers() must not raise — the failure is logged
	loudly (CRITICAL) instead, mirroring src/services/slack/bolt_app.py's
	own catch-log-and-degrade pattern."""
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
		main._start_background_workers()  # must NOT raise

	assert "respond_worker" not in names
	assert "show_rate_reminder_sender.run_sweep" in names, (
		f"unrelated workers must still start, but only these did: {names}"
	)
	assert len(names) > 10, "sanity check: the other ~18 sweep threads should have started"
