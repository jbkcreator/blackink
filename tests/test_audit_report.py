"""Tests for ghost-shopper audit report module."""

from unittest.mock import MagicMock
import pytest

from src.services.audit_report import (
    AuditDataMissing,
    AuditMetrics,
    assert_audit_complete,
    build_audit_context,
    get_audit_metrics,
    section_loss_estimate,
    section_speed_summary,
    _speed_grade,
    _speed_label,
)


# ---------------------------------------------------------------------------
# _speed_grade
# ---------------------------------------------------------------------------

def test_grade_fast():
    assert _speed_grade(60) == "FAST"

def test_grade_fast_boundary():
    assert _speed_grade(300) == "FAST"

def test_grade_average():
    assert _speed_grade(1800) == "AVERAGE"

def test_grade_slow():
    assert _speed_grade(7200) == "SLOW"

def test_grade_critical():
    assert _speed_grade(90000) == "CRITICAL"


# ---------------------------------------------------------------------------
# _speed_label
# ---------------------------------------------------------------------------

def test_label_seconds():
    assert _speed_label(45) == "45s"

def test_label_minutes():
    assert _speed_label(272) == "4m 32s"

def test_label_hours_with_minutes():
    assert _speed_label(4 * 3600 + 12 * 60) == "4h 12m"

def test_label_exact_hours():
    assert _speed_label(3600) == "1h"


# ---------------------------------------------------------------------------
# get_audit_metrics
# ---------------------------------------------------------------------------

def _session_with_audit(speed_sec=252.0, loss_est=47200.0, event_id="ev_001"):
    session = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = (event_id, {
        "audit_speed_score_sec": speed_sec,
        "audit_loss_dollars_est": loss_est,
    })
    session.execute.return_value = result
    return session


def _session_no_audit():
    session = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = None
    session.execute.return_value = result
    return session


def _session_incomplete_payload():
    session = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = ("ev_002", {"audit_speed_score_sec": 300.0})  # missing loss
    session.execute.return_value = result
    return session


def test_returns_audit_metrics():
    metrics = get_audit_metrics(_session_with_audit(), contact_id=1)
    assert isinstance(metrics, AuditMetrics)
    assert metrics.speed_score_sec == 252.0
    assert metrics.loss_dollars_est == 47200.0


def test_returns_none_when_no_audit():
    assert get_audit_metrics(_session_no_audit(), contact_id=1) is None


def test_returns_none_when_payload_incomplete():
    assert get_audit_metrics(_session_incomplete_payload(), contact_id=1) is None


def test_speed_grade_computed():
    metrics = get_audit_metrics(_session_with_audit(speed_sec=252.0), contact_id=1)
    assert metrics.speed_grade == "FAST"


def test_speed_label_computed():
    metrics = get_audit_metrics(_session_with_audit(speed_sec=272.0), contact_id=1)
    assert metrics.speed_label == "4m 32s"


def test_event_id_stored():
    metrics = get_audit_metrics(_session_with_audit(event_id="ev_xyz"), contact_id=1)
    assert metrics.audit_event_id == "ev_xyz"


# ---------------------------------------------------------------------------
# assert_audit_complete
# ---------------------------------------------------------------------------

def test_assert_passes_when_metrics_present():
    metrics = get_audit_metrics(_session_with_audit(), contact_id=5)
    result = assert_audit_complete(5, metrics)
    assert result is metrics


def test_assert_raises_when_none():
    with pytest.raises(AuditDataMissing, match="contact_id=5"):
        assert_audit_complete(5, None)


# ---------------------------------------------------------------------------
# build_audit_context
# ---------------------------------------------------------------------------

def test_build_context_keys():
    metrics = get_audit_metrics(_session_with_audit(speed_sec=272.0, loss_est=47200.0), 1)
    ctx = build_audit_context(metrics)
    assert ctx["audit_speed"] == "4m 32s"
    assert ctx["loss_dollars"] == "47200"


def test_loss_dollars_no_decimals():
    metrics = get_audit_metrics(_session_with_audit(loss_est=47200.99), 1)
    ctx = build_audit_context(metrics)
    assert "." not in ctx["loss_dollars"]


# ---------------------------------------------------------------------------
# Evidence packet sections
# ---------------------------------------------------------------------------

def test_section_speed_summary_shape():
    metrics = get_audit_metrics(_session_with_audit(speed_sec=7200.0), 1)
    s = section_speed_summary(metrics)
    assert s["section"] == "speed_summary"
    assert s["speed_grade"] == "SLOW"
    assert s["delta_sec"] == 7200.0 - 3600.0


def test_section_loss_estimate_shape():
    metrics = get_audit_metrics(_session_with_audit(loss_est=12500.0), 1)
    s = section_loss_estimate(metrics)
    assert s["section"] == "loss_estimate"
    assert s["loss_label"] == "$12,500"
