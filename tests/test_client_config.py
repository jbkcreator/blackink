"""Deny-by-default resolution — the inverse of Forced Action's
venture_config.py fallback-to-shared-settings behavior. Pure unit tests
using a FakeSession, no live database required. Each test uses a unique
client_id to avoid the in-process cache leaking state between tests.
"""

from types import SimpleNamespace

from src.services.client_config import get_client_config, invalidate_cache


class FakeSession:
	def __init__(self, row=None, raise_error=False):
		self._row = row
		self._raise_error = raise_error

	def execute(self, *args, **kwargs):
		if self._raise_error:
			raise RuntimeError("simulated DB error")
		return SimpleNamespace(fetchone=lambda: self._row)


def _active_row(client_id="c1", suspended_at=None):
	return SimpleNamespace(
		client_id=client_id,
		display_name="Test Client",
		is_active=True,
		plan_tier="standard",
		daily_send_ceiling=100,
		contract_start_date=None,
		contract_end_date=None,
		suspended_at=suspended_at,
	)


def test_denies_on_db_error():
	invalidate_cache()
	config = get_client_config(FakeSession(raise_error=True), "err_client")
	assert config.is_active is False


def test_denies_on_missing_row():
	invalidate_cache()
	config = get_client_config(FakeSession(row=None), "missing_client")
	assert config.is_active is False


def test_denies_on_inactive_row():
	row = _active_row(client_id="inactive_client")
	row.is_active = False
	invalidate_cache()
	config = get_client_config(FakeSession(row=row), "inactive_client")
	assert config.is_active is False


def test_denies_on_suspended_row():
	from datetime import datetime, timezone

	row = _active_row(client_id="suspended_client", suspended_at=datetime.now(timezone.utc))
	invalidate_cache()
	config = get_client_config(FakeSession(row=row), "suspended_client")
	assert config.is_active is False


def test_allows_active_unsuspended_row():
	row = _active_row(client_id="good_client")
	invalidate_cache()
	config = get_client_config(FakeSession(row=row), "good_client")
	assert config.is_active is True
	assert config.daily_send_ceiling == 100


def test_denies_empty_client_id_without_touching_db():
	config = get_client_config(FakeSession(raise_error=True), "")
	assert config.is_active is False


def test_cache_invalidation_forces_fresh_lookup():
	invalidate_cache()
	client_id = "cache_test_client"
	row = _active_row(client_id=client_id)
	first = get_client_config(FakeSession(row=row), client_id)
	assert first.is_active is True

	invalidate_cache(client_id)
	# Second lookup with a session that would deny — proves the cache was
	# actually cleared, not just coincidentally re-fetched.
	second = get_client_config(FakeSession(row=None), client_id)
	assert second.is_active is False
