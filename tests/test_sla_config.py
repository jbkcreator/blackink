"""Per-client SLA-window resolution — override-or-platform-default.

Pure unit tests with a FakeSession, no live database. Verifies a client's
own clients.sla_* columns win when set, and NULL columns (or an unknown
client) fall back to the config/settings.py platform defaults.
"""
from types import SimpleNamespace

from config.settings import get_settings
from src.services.sla_config import resolve_sla_windows


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def mappings(self):
		return self

	def first(self):
		return self._row


class FakeSession:
	def __init__(self, row=None):
		self._row = row

	def execute(self, *args, **kwargs):
		return _FakeResult(self._row)


def test_falls_back_to_platform_defaults_when_all_null():
	settings = get_settings()
	row = {
		"sla_hot_lead_minutes": None,
		"sla_standard_minutes": None,
		"sla_tier2_minutes": None,
		"sla_tier3_minutes": None,
		"speed_to_lead_sla_minutes": None,
	}
	w = resolve_sla_windows(FakeSession(row), "acme")
	assert w.hot_lead_minutes == settings.respond_sla_hot_lead_minutes
	assert w.standard_minutes == settings.respond_sla_standard_minutes
	assert w.tier2_minutes == settings.respond_sla_tier2_minutes
	assert w.tier3_minutes == settings.respond_sla_tier3_minutes
	assert w.speed_to_lead_minutes == settings.speed_to_lead_sla_minutes


def test_uses_per_client_override_when_set():
	row = {
		"sla_hot_lead_minutes": 5,
		"sla_standard_minutes": 20,
		"sla_tier2_minutes": 25,
		"sla_tier3_minutes": 90,
		"speed_to_lead_sla_minutes": 10,
	}
	w = resolve_sla_windows(FakeSession(row), "acme")
	assert (w.hot_lead_minutes, w.standard_minutes, w.tier2_minutes,
		w.tier3_minutes, w.speed_to_lead_minutes) == (5, 20, 25, 90, 10)


def test_mixed_override_and_default():
	settings = get_settings()
	row = {
		"sla_hot_lead_minutes": 5,
		"sla_standard_minutes": None,   # falls back
		"sla_tier2_minutes": None,
		"sla_tier3_minutes": 120,
		"speed_to_lead_sla_minutes": None,
	}
	w = resolve_sla_windows(FakeSession(row), "acme")
	assert w.hot_lead_minutes == 5
	assert w.standard_minutes == settings.respond_sla_standard_minutes
	assert w.tier3_minutes == 120
	assert w.speed_to_lead_minutes == settings.speed_to_lead_sla_minutes


def test_unknown_client_resolves_to_defaults():
	settings = get_settings()
	# No row (client not found) — still returns concrete platform defaults,
	# never raises, since an SLA still needs a due time.
	w = resolve_sla_windows(FakeSession(None), None)
	assert w.hot_lead_minutes == settings.respond_sla_hot_lead_minutes
	assert w.speed_to_lead_minutes == settings.speed_to_lead_sla_minutes
