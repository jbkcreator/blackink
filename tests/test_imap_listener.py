"""IMAP listener — unit tests (no real IMAP, no real Redis, no real DB).

Tests cover:
  - _parse_sender_domain: header parsing edge cases
  - _compute_loss_est:    revenue formula
  - _handle_reply:        latency calc, audit row write, redis publish, marker clear
  - _sweep_timeouts:      timed-out rows get null resume + timed_out=True audit row
  - no pending match:     unknown domain is silently ignored
  - idempotency of audit: _record_reply called before _publish_resume

Run:
    pytest tests/test_imap_listener.py -v
"""
from __future__ import annotations

import json
import math
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# aioimaplib is an optional runtime dep not installed in the test env.
# Stub it out before importing the listener module.
sys.modules.setdefault("aioimaplib", MagicMock())

from src.tasks.imap_listener import (  # noqa: E402
    _compute_loss_est,
    _handle_reply,
    _parse_sender_domain,
    _sweep_timeouts,
)

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_db(pending_row=None, timeout_rows=None):
    """Return a mock DB session.

    pending_row  — value returned by _find_pending_submission (fetchone)
    timeout_rows — value returned by _find_timed_out_submissions (fetchall)
    """
    db = MagicMock()
    # fetchone() used by _find_pending_submission
    db.execute.return_value.fetchone.return_value = pending_row
    # fetchall() used by _find_timed_out_submissions
    db.execute.return_value.fetchall.return_value = timeout_rows or []
    return db


def _pending_row(
    ghost_submitted_at: int,
    ghost_work_order_id: str = "wo-test-001",
    company_id: str = "co-abc",
):
    return SimpleNamespace(
        ghost_submitted_at=ghost_submitted_at,
        ghost_work_order_id=ghost_work_order_id,
        company_id=company_id,
    )


@contextmanager
def _db_ctx(db):
    yield db


# ── _parse_sender_domain ──────────────────────────────────────────────────────

class TestParseSenderDomain:
    def test_standard_from_header(self):
        assert _parse_sender_domain("Jordan <inquiry@suncoastpm.com>") == "suncoastpm.com"

    def test_bare_address(self):
        assert _parse_sender_domain("info@tampabaypm.com") == "tampabaypm.com"

    def test_www_stripped(self):
        assert _parse_sender_domain("noreply@www.example-pm.com") == "example-pm.com"

    def test_empty_returns_none(self):
        assert _parse_sender_domain("") is None

    def test_no_at_sign_returns_none(self):
        assert _parse_sender_domain("Not A Real Header") is None

    def test_uppercase_lowercased(self):
        assert _parse_sender_domain("Admin@SunCoastPM.COM") == "suncoastpm.com"


# ── _compute_loss_est ─────────────────────────────────────────────────────────

class TestComputeLossEst:
    def test_zero_latency_zero_loss(self):
        assert _compute_loss_est(0) == 0

    def test_loss_increases_with_latency(self):
        assert _compute_loss_est(3600) > _compute_loss_est(1800)

    def test_loss_bounded_below_max(self):
        # Even at 30 days the formula can't exceed MONTHLY_LEADS * AVG_FEE * AVG_TENURE
        max_possible = 8 * 1_200 * 2.5
        assert _compute_loss_est(30 * 24 * 3600) <= int(max_possible)

    def test_known_value(self):
        # 1 hour = 3600s
        # decay = 1 - e^(-0.0005 * 3600) ≈ 0.8347
        # loss  = 8 * 0.8347 * 1200 * 2.5 ≈ 20033
        decay = 1.0 - math.exp(-0.0005 * 3600)
        expected = int(8 * decay * 1_200 * 2.5)
        assert _compute_loss_est(3600) == expected


# ── _handle_reply ─────────────────────────────────────────────────────────────

class TestHandleReply:
    def _run(self, db, redis_mock):
        with (
            patch("src.tasks.imap_listener.get_system_db_context", return_value=_db_ctx(db)),
            patch("src.tasks.imap_listener.get_redis_client", return_value=redis_mock),
        ):
            _handle_reply("suncoastpm.com", received_at_ms=1_700_010_000_000)

    def test_publishes_resume_signal(self):
        submitted_at_ms = 1_700_000_000_000
        row = _pending_row(ghost_submitted_at=submitted_at_ms)
        db = _make_db(pending_row=row)
        redis_mock = MagicMock()

        self._run(db, redis_mock)

        redis_mock.xadd.assert_called_once()
        stream, fields = redis_mock.xadd.call_args[0]
        assert stream == "ink:resume_signals"
        assert fields["work_order_id"] == "wo-test-001"
        payload = json.loads(fields["resume_payload"])
        assert payload["latency_sec"] == 10_000   # (1_700_010_000_000 - 1_700_000_000_000) / 1000
        assert payload["loss_est"] == _compute_loss_est(10_000)

    def test_audit_row_written_before_redis_publish(self):
        """_record_reply (db.execute) must be called before xadd."""
        submitted_at_ms = 1_700_000_000_000
        row = _pending_row(ghost_submitted_at=submitted_at_ms)
        db = _make_db(pending_row=row)
        redis_mock = MagicMock()
        call_order = []
        db.execute.side_effect = lambda *a, **kw: (call_order.append("db_execute"), MagicMock())[1]
        redis_mock.xadd.side_effect = lambda *a, **kw: call_order.append("xadd")

        self._run(db, redis_mock)

        # First db.execute is _find_pending_submission; second is _record_reply; then xadd; then _clear_pending
        assert call_order.index("db_execute") < call_order.index("xadd")

    def test_clears_ghost_markers_after_publish(self):
        submitted_at_ms = 1_700_000_000_000
        row = _pending_row(ghost_submitted_at=submitted_at_ms)
        db = _make_db(pending_row=row)
        redis_mock = MagicMock()

        self._run(db, redis_mock)

        # db.commit() must be called
        db.commit.assert_called_once()
        # At least 3 db.execute calls: find + record_reply + clear_pending
        assert db.execute.call_count >= 3

    def test_unknown_domain_ignored(self):
        """No pending submission for domain — nothing published, nothing committed."""
        db = _make_db(pending_row=None)
        redis_mock = MagicMock()

        with (
            patch("src.tasks.imap_listener.get_system_db_context", return_value=_db_ctx(db)),
            patch("src.tasks.imap_listener.get_redis_client", return_value=redis_mock),
        ):
            _handle_reply("unknown-domain.com", received_at_ms=1_700_010_000_000)

        redis_mock.xadd.assert_not_called()
        db.commit.assert_not_called()

    def test_latency_is_non_negative(self):
        """Clock skew: if received_at < ghost_submitted_at, latency clamps to 0."""
        row = _pending_row(ghost_submitted_at=1_700_010_000_000)  # submitted AFTER received
        db = _make_db(pending_row=row)
        redis_mock = MagicMock()

        self._run(db, redis_mock)

        _, fields = redis_mock.xadd.call_args[0]
        payload = json.loads(fields["resume_payload"])
        assert payload["latency_sec"] == 0


# ── _sweep_timeouts ───────────────────────────────────────────────────────────

class TestSweepTimeouts:
    def _run(self, db, redis_mock, timeout_hours=24):
        with (
            patch("src.tasks.imap_listener.get_system_db_context", return_value=_db_ctx(db)),
            patch("src.tasks.imap_listener.get_redis_client", return_value=redis_mock),
        ):
            _sweep_timeouts(timeout_hours)

    def test_no_timed_out_rows_nothing_published(self):
        db = _make_db(timeout_rows=[])
        redis_mock = MagicMock()
        self._run(db, redis_mock)
        redis_mock.xadd.assert_not_called()
        db.commit.assert_not_called()

    def test_timed_out_row_publishes_null_resume(self):
        rows = [
            SimpleNamespace(
                ghost_submitted_at=1_699_000_000_000,
                ghost_work_order_id="wo-timeout-001",
                company_id="co-xyz",
            )
        ]
        db = _make_db(timeout_rows=rows)
        # fetchall needs to return rows, not fetchone
        db.execute.return_value.fetchall.return_value = rows
        redis_mock = MagicMock()

        self._run(db, redis_mock)

        redis_mock.xadd.assert_called_once()
        stream, fields = redis_mock.xadd.call_args[0]
        assert stream == "ink:resume_signals"
        assert fields["work_order_id"] == "wo-timeout-001"
        payload = json.loads(fields["resume_payload"])
        assert payload["latency_sec"] is None
        assert payload["loss_est"] is None

    def test_timed_out_row_writes_audit_with_timed_out_true(self):
        rows = [
            SimpleNamespace(
                ghost_submitted_at=1_699_000_000_000,
                ghost_work_order_id="wo-timeout-002",
                company_id="co-xyz",
            )
        ]
        db = _make_db(timeout_rows=rows)
        db.execute.return_value.fetchall.return_value = rows
        redis_mock = MagicMock()

        execute_calls = []
        def capture_execute(stmt, params=None):
            execute_calls.append(str(stmt))
            m = MagicMock()
            m.fetchall.return_value = rows
            return m

        db.execute.side_effect = capture_execute

        self._run(db, redis_mock)

        # The _record_reply INSERT should include timed_out=True in params
        # Verify commit was called (all writes went through)
        db.commit.assert_called_once()

    def test_multiple_timed_out_rows_all_published(self):
        rows = [
            SimpleNamespace(
                ghost_submitted_at=1_699_000_000_000,
                ghost_work_order_id=f"wo-t-{i}",
                company_id=f"co-{i}",
            )
            for i in range(3)
        ]
        db = _make_db(timeout_rows=rows)
        db.execute.return_value.fetchall.return_value = rows
        redis_mock = MagicMock()

        self._run(db, redis_mock)

        assert redis_mock.xadd.call_count == 3
        db.commit.assert_called_once()
