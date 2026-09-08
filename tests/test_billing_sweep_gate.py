"""Pure test: run_miss_credit_sweep() fails CLOSED (does nothing) unless
BILLING_MISS_CREDIT_SWEEP_ENABLED is explicitly set (PR #37 review finding
— nothing writes inbound_messages.acked_at yet, so every unclassified
message older than 60 seconds would otherwise look identical to a genuine
miss). No DB — get_system_db_context is never reached when the sweep is
disabled, which this test asserts directly."""
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
