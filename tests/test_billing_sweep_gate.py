"""Pure test: run_miss_credit_sweep() fails CLOSED (does nothing) unless
BILLING_MISS_CREDIT_SWEEP_ENABLED is explicitly set (PR #37 review finding
— nothing writes inbound_messages.acked_at yet, so every unclassified
message older than 60 seconds would otherwise look identical to a genuine
miss). No DB — get_system_db_context is never reached when the sweep is
disabled, which this test asserts directly.

test_miss_credit_sweep_runs_when_explicitly_enabled also mocks
src.tasks.billing_sweep._halt_if_unhealthy (S-1's Vera health gate, added
after this file) to return False (healthy) — this test is about the
enable/disable flag specifically, not about the health gate, which has its
own dedicated coverage in tests/test_vera_health_gate.py. Without this
mock, the real evaluate_settlement_health() would try to open a real DB
session (a different get_system_db_context reference than the one this
test patches — see health_gate.py's module docstring for why) and fail
closed to a HALT in this DB-less unit test, breaking this test's own,
unrelated assertion."""
from config.settings import get_settings
from src.tasks.billing_sweep import run_miss_credit_sweep


def test_miss_credit_sweep_disabled_by_default_does_nothing(monkeypatch):
	get_settings.cache_clear()
	monkeypatch.delenv("BILLING_MISS_CREDIT_SWEEP_ENABLED", raising=False)

	called = []
	monkeypatch.setattr(
		"src.tasks.billing_sweep.get_system_db_context",
		lambda: (_ for _ in ()).throw(AssertionError("must not open a DB session while disabled")),
	)

	result = run_miss_credit_sweep()
	assert result == 0
	assert called == []
	get_settings.cache_clear()


def test_miss_credit_sweep_runs_when_explicitly_enabled(monkeypatch):
	get_settings.cache_clear()
	monkeypatch.setenv("BILLING_MISS_CREDIT_SWEEP_ENABLED", "true")
	monkeypatch.setattr("src.tasks.billing_sweep._halt_if_unhealthy", lambda sweep_name: False)

	opened = []

	class _FakeSession:
		def execute(self, *a, **kw):
			return type("R", (), {"all": lambda self=None: []})()

	class _FakeCtx:
		def __enter__(self):
			opened.append(True)
			return _FakeSession()

		def __exit__(self, *a):
			return False

	monkeypatch.setattr("src.tasks.billing_sweep.get_system_db_context", lambda: _FakeCtx())

	result = run_miss_credit_sweep()
	assert result == 0
	assert opened == [True]
	get_settings.cache_clear()
