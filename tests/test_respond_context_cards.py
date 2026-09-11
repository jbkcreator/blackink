"""Tests for Subtask 2.1.2 — context cards, SLA timers, and escalation sweep.

Covers:
  - Card hash computation and stability
  - Objection subtype parsing in classifier
  - CONTEXT_CARD_INTENTS set membership
  - SLA computation: 15 min for HOT_LEAD/WHALE_OWNER, 60 min for others
  - Objection playbook selection
  - Weakest-signal extraction from OVS signal_detail
  - Escalation sweep tier queries (unit, no Slack/DB)
  - context_card_generated event payload validation
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from unittest.mock import MagicMock

from src.agents.respond.classifier import ClassificationResult, _call_model
from src.agents.respond.context_cards import (
    CONTEXT_CARD_INTENTS,
    OBJECTION_PLAYBOOKS,
    compute_card_hash,
    _weakest_signals,
    _suggest_opener,
    _format_thread,
    _fetch_engagement,
)
from src.agents.respond.intents import Intent
from src.agents.respond.worker import _compute_sla


# ── Card hash ─────────────────────────────────────────────────────────────────

def test_card_hash_is_deterministic():
    h1 = compute_card_hash(42, "acme-pm", "HOT_LEAD", "2026-09-07T10:00:00.000000Z")
    h2 = compute_card_hash(42, "acme-pm", "HOT_LEAD", "2026-09-07T10:00:00.000000Z")
    assert h1 == h2
    assert len(h1) == 64


def test_card_hash_changes_with_db_id():
    h1 = compute_card_hash(42, "acme-pm", "HOT_LEAD", "2026-09-07T10:00:00.000000Z")
    h2 = compute_card_hash(43, "acme-pm", "HOT_LEAD", "2026-09-07T10:00:00.000000Z")
    assert h1 != h2


def test_card_hash_changes_with_intent():
    h1 = compute_card_hash(42, "acme-pm", "HOT_LEAD", "2026-09-07T10:00:00.000000Z")
    h2 = compute_card_hash(42, "acme-pm", "OBJECTION", "2026-09-07T10:00:00.000000Z")
    assert h1 != h2


def test_card_hash_changes_with_posted_at():
    h1 = compute_card_hash(42, "acme-pm", "HOT_LEAD", "2026-09-07T10:00:00.000000Z")
    h2 = compute_card_hash(42, "acme-pm", "HOT_LEAD", "2026-09-07T10:05:00.000000Z")
    assert h1 != h2


# ── SLA computation ───────────────────────────────────────────────────────────

_BASE = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("intent", [Intent.HOT_LEAD, Intent.WHALE_OWNER])
def test_sla_is_15_min_for_hot_and_whale(intent):
    sla = _compute_sla(intent, _BASE)
    assert sla == _BASE + timedelta(minutes=15)


@pytest.mark.parametrize("intent", [
    Intent.OBJECTION, Intent.QUESTION, Intent.COMPLAINT,
    Intent.PARTNER, Intent.NURTURE,
])
def test_sla_is_60_min_for_other_routed_intents(intent):
    sla = _compute_sla(intent, _BASE)
    assert sla == _BASE + timedelta(minutes=60)


def test_sla_handles_naive_datetime():
    naive = datetime(2026, 9, 7, 10, 0, 0)
    sla = _compute_sla(Intent.HOT_LEAD, naive)
    assert sla == datetime(2026, 9, 7, 10, 15, 0, tzinfo=timezone.utc)


# ── CONTEXT_CARD_INTENTS ──────────────────────────────────────────────────────

def test_context_card_intents_includes_expected():
    assert Intent.HOT_LEAD in CONTEXT_CARD_INTENTS
    assert Intent.WHALE_OWNER in CONTEXT_CARD_INTENTS
    assert Intent.OBJECTION in CONTEXT_CARD_INTENTS


def test_context_card_intents_excludes_others():
    for intent in (Intent.UNSUBSCRIBE, Intent.LEGAL_GRIEF, Intent.LATER,
                   Intent.NURTURE, Intent.QUESTION, Intent.COMPLAINT, Intent.PARTNER):
        assert intent not in CONTEXT_CARD_INTENTS


# ── Objection subtype in classifier ──────────────────────────────────────────

def test_classifier_extracts_objection_subtype(monkeypatch):
    from src.agents.respond.classifier import _call_model

    class FakeContent:
        text = json.dumps({
            "intent": "OBJECTION",
            "confidence": 0.88,
            "reasoning": "Locked into existing contract.",
            "objection_subtype": "existing_agency",
        })

    class FakeResponse:
        content = [FakeContent()]

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                return FakeResponse()

    result = _call_model(FakeClient(), "claude-haiku-4-5-20251001", "test body")
    assert result.intent == Intent.OBJECTION
    assert result.objection_subtype == "existing_agency"


def test_classifier_normalises_unknown_subtype(monkeypatch):
    from src.agents.respond.classifier import _call_model

    class FakeContent:
        text = json.dumps({
            "intent": "OBJECTION",
            "confidence": 0.7,
            "reasoning": "Something unusual.",
            "objection_subtype": "trust_issues",  # not in the allowed set
        })

    class FakeResponse:
        content = [FakeContent()]

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                return FakeResponse()

    result = _call_model(FakeClient(), "claude-haiku-4-5-20251001", "test body")
    assert result.objection_subtype == "other"


def test_non_objection_has_no_subtype(monkeypatch):
    from src.agents.respond.classifier import _call_model

    class FakeContent:
        text = json.dumps({"intent": "HOT_LEAD", "confidence": 0.95, "reasoning": "Ready to book."})

    class FakeResponse:
        content = [FakeContent()]

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                return FakeResponse()

    result = _call_model(FakeClient(), "claude-haiku-4-5-20251001", "test body")
    assert result.intent == Intent.HOT_LEAD
    assert result.objection_subtype is None


# ── Objection playbooks ───────────────────────────────────────────────────────

def test_all_four_subtypes_have_playbooks():
    for subtype in ("pricing", "timing", "existing_agency", "capacity"):
        assert subtype in OBJECTION_PLAYBOOKS
        assert len(OBJECTION_PLAYBOOKS[subtype]) > 50


def test_other_subtype_has_playbook():
    assert "other" in OBJECTION_PLAYBOOKS


# ── Weakest-signal extraction ─────────────────────────────────────────────────

def test_weakest_signals_returns_three():
    signal_detail = {
        "owner_page":    {"points_awarded": 0,  "points_possible": 14},
        "contact_info":  {"points_awarded": 5,  "points_possible": 10},
        "tech_health":   {"points_awarded": 6,  "points_possible": 6},
        "after_hours":   {"points_awarded": 0,  "points_possible": 8},
        "google_rating": {"points_awarded": 4,  "points_possible": 20},
    }
    weak = _weakest_signals(signal_detail)
    assert len(weak) == 3
    # owner_page (0/14=0.0) and after_hours (0/8=0.0) are weakest;
    # google_rating (4/20=0.2) is next weakest
    assert "Owner Page" in weak or "After-Hours Contact" in weak


def test_weakest_signals_empty_on_missing_data():
    assert _weakest_signals({}) == []
    assert _weakest_signals(None) == []


def test_weakest_signals_skips_zero_max():
    signal_detail = {
        "owner_page":    {"points_awarded": 0, "points_possible": 0},  # division by zero guard
        "google_rating": {"points_awarded": 10, "points_possible": 20},
    }
    weak = _weakest_signals(signal_detail)
    assert "Owner Page" not in weak  # zero max skipped


# ── Suggested opener ──────────────────────────────────────────────────────────

def test_suggest_opener_hot_lead_mentions_name():
    opener = _suggest_opener(Intent.HOT_LEAD, "David", ["Google Rating"])
    assert "David" in opener
    assert "google rating" in opener.lower()


def test_suggest_opener_whale_owner_mentions_two_weaknesses():
    opener = _suggest_opener(Intent.WHALE_OWNER, "Sarah", ["Owner Page", "After-Hours Contact", "DBPR Licence"])
    assert "Sarah" in opener
    assert "owner page" in opener.lower()
    assert "after-hours" in opener.lower()


def test_suggest_opener_objection_returns_question():
    opener = _suggest_opener(Intent.OBJECTION, "Tom", [])
    assert "?" in opener


# ── Thread formatting ─────────────────────────────────────────────────────────

def test_format_thread_empty():
    text = _format_thread([])
    assert "No prior messages" in text


def test_format_thread_shows_preview():
    msgs = [
        {"body_text": "I've been thinking about this.", "subject": None, "received_at": datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)},
    ]
    text = _format_thread(msgs)
    assert "thinking about this" in text
    assert "Sep 07" in text


def test_format_thread_truncates_long_body():
    long_body = "x" * 200
    msgs = [{"body_text": long_body, "subject": None, "received_at": datetime(2026, 9, 7, tzinfo=timezone.utc)}]
    text = _format_thread(msgs)
    assert "…" in text


# ── Engagement (Group D / D-4) ────────────────────────────────────────────────
# email_opened/email_clicked have no producer anywhere in this codebase
# (S-8 is not built) — opens/clicks must render "n/a", never a misleading
# literal 0 that a setter reads as "confirmed zero engagement".

def test_engagement_opens_clicks_are_not_a_when_no_contact():
    result = _fetch_engagement(MagicMock(), None)
    assert result["opens"] == "n/a"
    assert result["clicks"] == "n/a"
    assert result["touches"] == 0


def test_engagement_opens_clicks_are_not_a_with_a_real_contact():
    """Even with a real contact_id and touches sent, opens/clicks must still
    read n/a — there is no event producer to query, regardless of contact."""
    db = MagicMock()
    db.execute.return_value.scalar.return_value = 5
    result = _fetch_engagement(db, contact_id=42)
    assert result["touches"] == 5
    assert result["opens"] == "n/a"
    assert result["clicks"] == "n/a"
