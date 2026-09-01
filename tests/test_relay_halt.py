"""Unit tests for Relay halt/resume mechanics.

Covers:
- HMAC token generation and verification (resume_auth)
- Redis key naming convention (halt_service)
- is_halted() scope cascade and synced-flag fast path
- is_halted() Postgres fallback in pre-sync window
- issue_halt() dual-persistence write behavior
- resume_halt() token verification and Redis cleanup
- sync_halts_from_db() Redis restore from Postgres

All tests use fakeredis + a FakeSession stand-in — no live DB or Redis
required. Same pattern as tests/test_compliance_gate.py.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from src.agents.relay.halt_state import (
    HaltRecord,
    SCOPE_GLOBAL,
    SCOPE_CLIENT,
    SCOPE_CAMPAIGN,
    VALID_SCOPES,
)
from src.agents.relay import resume_auth
from src.agents.relay.halt_service import (
    _redis_key,
    _REDIS_SYNCED_KEY,
    is_halted,
    issue_halt,
    resume_halt,
    get_active_halts,
)
from src.agents.relay.sync import sync_halts_from_db


# ── Helpers ───────────────────────────────────────────────────────────────────

_SECRET = "test-relay-secret-at-least-32-bytes!"


def _fake_settings():
    m = MagicMock()
    m.relay_resume_secret.get_secret_value.return_value = _SECRET
    return m


@contextmanager
def _fake_system_db(rows=None, update_row=None, raise_error=False):
    """Context-manager yielding a minimal session stub."""

    class _FakeResult:
        def __init__(self, data):
            self._data = data

        def first(self):
            return self._data[0] if self._data else None

        def fetchall(self):
            return self._data

    class _FakeSession:
        def __init__(self):
            self._call_count = 0

        def execute(self, _stmt, _params=None):
            self._call_count += 1
            if raise_error:
                raise RuntimeError("simulated DB error")
            # resume_halt UPDATE returns update_row; everything else returns rows
            if update_row is not None and self._call_count > 1:
                return _FakeResult([update_row] if update_row else [])
            return _FakeResult(rows or [])

    yield _FakeSession()


@contextmanager
def _patched_redis(r):
    with patch("src.agents.relay.halt_service.get_redis_client", return_value=r), \
         patch("src.agents.relay.sync.get_redis_client", return_value=r):
        yield r


# ── resume_auth ───────────────────────────────────────────────────────────────

class TestResumeAuth:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("src.agents.relay.resume_auth.get_settings", return_value=_fake_settings()):
            yield

    def test_generate_returns_64_hex_chars(self):
        token = resume_auth.generate_resume_token(42)
        assert len(token) == 64
        int(token, 16)  # raises ValueError if not valid hex

    def test_verify_valid_token_passes(self):
        token = resume_auth.generate_resume_token(42)
        assert resume_auth.verify_resume_token(42, token) is True

    def test_verify_wrong_halt_id_fails(self):
        token = resume_auth.generate_resume_token(42)
        assert resume_auth.verify_resume_token(99, token) is False

    def test_verify_tampered_token_fails(self):
        token = resume_auth.generate_resume_token(42)
        tampered = token[:-4] + "dead"
        assert resume_auth.verify_resume_token(42, tampered) is False

    def test_different_halt_ids_produce_different_tokens(self):
        assert resume_auth.generate_resume_token(1) != resume_auth.generate_resume_token(2)

    def test_verify_returns_false_when_secret_missing(self):
        bad_settings = MagicMock()
        bad_settings.relay_resume_secret = None
        with patch("src.agents.relay.resume_auth.get_settings", return_value=bad_settings):
            assert resume_auth.verify_resume_token(1, "anytoken") is False


# ── Redis key naming ──────────────────────────────────────────────────────────

class TestRedisKeyNaming:
    def test_global_key(self):
        assert _redis_key("GLOBAL") == "relay:halt:global"

    def test_client_key(self):
        assert _redis_key("CLIENT", "acme_pm") == "relay:halt:client:acme_pm"

    def test_campaign_key(self):
        assert _redis_key("CAMPAIGN", "camp_123") == "relay:halt:campaign:camp_123"

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError):
            _redis_key("UNKNOWN")


# ── is_halted — Redis fast path ───────────────────────────────────────────────

class TestIsHalted:
    def setup_method(self):
        self.r = fakeredis.FakeRedis(decode_responses=True)

    def test_not_halted_when_redis_empty_and_synced(self):
        self.r.set(_REDIS_SYNCED_KEY, "1")
        with _patched_redis(self.r):
            assert is_halted() is False

    def test_halted_on_global_key(self):
        self.r.set("relay:halt:global", "1")
        self.r.set(_REDIS_SYNCED_KEY, "1")
        with _patched_redis(self.r):
            assert is_halted() is True

    def test_client_halt_does_not_block_other_clients(self):
        self.r.set("relay:halt:client:acme_pm", "2")
        self.r.set(_REDIS_SYNCED_KEY, "1")
        with _patched_redis(self.r):
            assert is_halted(client_id="other_client") is False

    def test_client_halt_blocks_that_client(self):
        self.r.set("relay:halt:client:acme_pm", "2")
        self.r.set(_REDIS_SYNCED_KEY, "1")
        with _patched_redis(self.r):
            assert is_halted(client_id="acme_pm") is True

    def test_global_halt_blocks_any_client(self):
        self.r.set("relay:halt:global", "1")
        self.r.set(_REDIS_SYNCED_KEY, "1")
        with _patched_redis(self.r):
            assert is_halted(client_id="acme_pm") is True

    def test_campaign_halt_blocks_that_campaign(self):
        self.r.set("relay:halt:campaign:camp_99", "3")
        self.r.set(_REDIS_SYNCED_KEY, "1")
        with _patched_redis(self.r):
            assert is_halted(campaign_id="camp_99") is True

    def test_campaign_halt_does_not_block_other_campaigns(self):
        self.r.set("relay:halt:campaign:camp_99", "3")
        self.r.set(_REDIS_SYNCED_KEY, "1")
        with _patched_redis(self.r):
            assert is_halted(campaign_id="camp_other") is False

    def test_pre_sync_window_falls_back_to_postgres_not_halted(self):
        """When relay:synced is absent (pre-sync), must query Postgres."""
        with _patched_redis(self.r):
            with patch(
                "src.agents.relay.halt_service._db_is_halted",
                return_value=False,
            ) as mock_db:
                result = is_halted()
        mock_db.assert_called_once()
        assert result is False

    def test_pre_sync_window_falls_back_to_postgres_halted(self):
        with _patched_redis(self.r):
            with patch(
                "src.agents.relay.halt_service._db_is_halted",
                return_value=True,
            ) as mock_db:
                result = is_halted()
        mock_db.assert_called_once()
        assert result is True

    def test_redis_error_falls_back_to_postgres(self):
        broken_redis = MagicMock()
        broken_redis.exists.side_effect = RuntimeError("Redis down")
        with patch("src.agents.relay.halt_service.get_redis_client", return_value=broken_redis):
            with patch(
                "src.agents.relay.halt_service._db_is_halted",
                return_value=False,
            ) as mock_db:
                result = is_halted()
        mock_db.assert_called_once()
        assert result is False


# ── issue_halt ────────────────────────────────────────────────────────────────

class TestIssueHalt:
    def setup_method(self):
        self.r = fakeredis.FakeRedis(decode_responses=True)

    def test_issue_global_halt_sets_redis_key(self):
        db_row = SimpleNamespace(**{"__getitem__": lambda s, i: [7][i]})
        # Simulate row returning halt_id=7
        fake_row = MagicMock()
        fake_row.__getitem__ = lambda s, i: [7][i]

        class _FakeResult:
            def first(self_inner):
                return fake_row

        class _FakeSession:
            def execute(self_inner, *a, **kw):
                return _FakeResult()

        @contextmanager
        def _fake_ctx():
            yield _FakeSession()

        with patch("src.agents.relay.halt_service.get_system_db_context", _fake_ctx), \
             _patched_redis(self.r):
            halt_id = issue_halt(
                "GLOBAL", reason="Emergency stop", issued_by="admin"
            )

        assert halt_id == 7
        assert self.r.exists("relay:halt:global")

    def test_issue_halt_invalid_scope_raises(self):
        with pytest.raises(ValueError, match="Invalid scope"):
            issue_halt("INVALID", reason="x", issued_by="admin")

    def test_issue_client_halt_without_scope_id_raises(self):
        with pytest.raises(ValueError, match="scope_id is required"):
            issue_halt("CLIENT", reason="x", issued_by="admin")


# ── resume_halt ───────────────────────────────────────────────────────────────

class TestResumeHalt:
    def setup_method(self):
        self.r = fakeredis.FakeRedis(decode_responses=True)

    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("src.agents.relay.resume_auth.get_settings", return_value=_fake_settings()):
            yield

    def test_resume_with_valid_token_clears_redis(self):
        halt_id = 42
        self.r.set("relay:halt:global", str(halt_id))
        token = resume_auth.generate_resume_token(halt_id)

        _update_row = SimpleNamespace(scope="GLOBAL", scope_id=None)

        class _Res:
            def first(self_inner):
                return _update_row

        class _Sess:
            def execute(self_inner, *a, **kw):
                return _Res()

        @contextmanager
        def _fake_ctx():
            yield _Sess()

        with patch("src.agents.relay.halt_service.get_system_db_context", _fake_ctx), \
             _patched_redis(self.r):
            result = resume_halt(halt_id, token=token, resumed_by="admin")

        assert result is True
        assert not self.r.exists("relay:halt:global")

    def test_resume_with_invalid_token_rejected(self):
        with _patched_redis(self.r):
            result = resume_halt(42, token="badbadtoken", resumed_by="attacker")
        assert result is False
        # Redis should be untouched
        assert not self.r.exists("relay:halt:global")

    def test_resume_already_inactive_returns_false(self):
        token = resume_auth.generate_resume_token(99)

        @contextmanager
        def _fake_ctx():
            with _fake_system_db(update_row=None) as s:
                yield s

        with patch("src.agents.relay.halt_service.get_system_db_context", _fake_ctx), \
             _patched_redis(self.r):
            result = resume_halt(99, token=token, resumed_by="admin")

        assert result is False


# ── sync_halts_from_db ────────────────────────────────────────────────────────

class TestSyncHaltsFromDb:
    def setup_method(self):
        self.r = fakeredis.FakeRedis(decode_responses=True)

    def test_sync_restores_active_halts_to_redis(self):
        rows = [
            SimpleNamespace(id=1, scope="GLOBAL", scope_id=None),
            SimpleNamespace(id=2, scope="CLIENT", scope_id="acme_pm"),
        ]

        @contextmanager
        def _fake_ctx():
            with _fake_system_db(rows=rows) as s:
                yield s

        with patch("src.agents.relay.sync.get_system_db_context", _fake_ctx), \
             _patched_redis(self.r):
            count = sync_halts_from_db()

        assert count == 2
        assert self.r.get("relay:halt:global") == "1"
        assert self.r.get("relay:halt:client:acme_pm") == "2"
        assert self.r.exists(_REDIS_SYNCED_KEY)

    def test_sync_sets_synced_flag_even_with_zero_halts(self):
        @contextmanager
        def _fake_ctx():
            with _fake_system_db(rows=[]) as s:
                yield s

        with patch("src.agents.relay.sync.get_system_db_context", _fake_ctx), \
             _patched_redis(self.r):
            count = sync_halts_from_db()

        assert count == 0
        assert self.r.exists(_REDIS_SYNCED_KEY)

    def test_sync_returns_zero_on_db_error(self):
        @contextmanager
        def _fake_ctx():
            with _fake_system_db(raise_error=True) as s:
                yield s

        with patch("src.agents.relay.sync.get_system_db_context", _fake_ctx), \
             _patched_redis(self.r):
            count = sync_halts_from_db()

        assert count == 0
        assert not self.r.exists(_REDIS_SYNCED_KEY)

    def test_sync_after_redis_restart_replaces_old_state(self):
        """Stale Redis state from before restart gets overwritten."""
        self.r.set("relay:halt:global", "999")  # stale/wrong halt_id

        rows = [SimpleNamespace(id=5, scope="GLOBAL", scope_id=None)]

        @contextmanager
        def _fake_ctx():
            with _fake_system_db(rows=rows) as s:
                yield s

        with patch("src.agents.relay.sync.get_system_db_context", _fake_ctx), \
             _patched_redis(self.r):
            sync_halts_from_db()

        assert self.r.get("relay:halt:global") == "5"
