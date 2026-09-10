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

	with patch.object(main.threading, "Thread", _StubThread):
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
