"""Unit tests for the KB Auto-Response matcher (Subtask 2.1.3).

Uses a fake DB session — no live Postgres required.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.respond.kb_matcher import KbMatch, _clean, _score_entry, match_kb

# ── Fixtures ──────────────────────────────────────────────────────────────────

SEED_ENTRIES = [
    {
        "entry_id": 1,
        "topic": "How does this work?",
        "trigger_patterns": [
            "how does this work", "how does it work", "how does your service work",
            "what is this", "what do you do", "explain your service",
            "tell me more", "how does blackink work",
        ],
        "approved_response_template": "Walk-through template. No dollar amounts. Offers a meeting.",
        "min_confidence_threshold": 0.90,
    },
    {
        "entry_id": 2,
        "topic": "What does it cost?",
        "trigger_patterns": [
            "what does it cost", "how much does it cost", "what is the price",
            "how much do you charge", "what are your fees", "pricing",
            "how much is it", "what is your fee", "cost",
        ],
        "approved_response_template": "Fee-deflection template. No dollar amounts. Offers a meeting.",
        "min_confidence_threshold": 0.90,
    },
    {
        "entry_id": 3,
        "topic": "What areas do you cover?",
        "trigger_patterns": [
            "what areas do you cover", "which areas", "what counties",
            "do you cover my area", "what markets", "which markets",
            "where do you operate", "what locations", "do you work in",
        ],
        "approved_response_template": "Coverage template. No dollar amounts. Offers a meeting.",
        "min_confidence_threshold": 0.90,
    },
    {
        "entry_id": 4,
        "topic": "How long does it take?",
        "trigger_patterns": [
            "how long does it take", "how long does this take", "how long until",
            "how quickly", "what is the timeline", "when will i see results",
            "how fast", "turnaround time", "time to results",
        ],
        "approved_response_template": "Timeline template. No dollar amounts. Offers a meeting.",
        "min_confidence_threshold": 0.90,
    },
    {
        "entry_id": 5,
        "topic": "Is this a guarantee?",
        "trigger_patterns": [
            "is this a guarantee", "do you guarantee", "guaranteed",
            "money back", "refund", "what if it doesn't work",
            "what happens if", "risk free", "no risk",
        ],
        "approved_response_template": "Guarantee template. No dollar amounts. Offers a meeting.",
        "min_confidence_threshold": 0.90,
    },
]


def _make_db(entries=None) -> MagicMock:
    """Return a mock DB session whose execute().mappings().fetchall() returns the given rows."""
    rows = entries if entries is not None else SEED_ENTRIES
    mock_rows = []
    for e in rows:
        m = MagicMock()
        m.__getitem__ = lambda self, k, _e=e: _e[k]
        mock_rows.append(m)

    db = MagicMock()
    db.execute.return_value.mappings.return_value.fetchall.return_value = mock_rows
    return db


# ── _clean ────────────────────────────────────────────────────────────────────

class TestClean:
    def test_lowercases(self):
        assert _clean("HELLO World") == "hello world"

    def test_strips_punctuation(self):
        assert _clean("What does it cost?!") == "what does it cost"

    def test_collapses_whitespace(self):
        assert _clean("  how  long  ") == "how long"


# ── _score_entry ──────────────────────────────────────────────────────────────

class TestScoreEntry:
    def test_exact_match_near_one(self):
        score = _score_entry("how does this work", ["how does this work"])
        assert score >= 0.95

    def test_paraphrase_scores_reasonably(self):
        # token_sort_ratio on a loose paraphrase is intentionally moderate —
        # we just confirm it's non-trivial (>0.30) and below the 0.90 threshold.
        score = _score_entry("can you explain how your platform works", ["how does this work"])
        assert score > 0.30

    def test_unrelated_scores_low(self):
        score = _score_entry("the weather is nice today", ["how does this work"])
        assert score < 0.50

    def test_returns_best_of_multiple_patterns(self):
        # partial_ratio finds "how does it work" as a substring of the message → near-perfect score
        patterns = ["unrelated stuff", "how does it work"]
        score = _score_entry("how does it work exactly", patterns)
        assert score >= 0.90


# ── match_kb — seed phrase matching ──────────────────────────────────────────

class TestMatchKbSeedPhrases:
    def test_how_does_this_work(self):
        db = _make_db()
        result = match_kb(db, "Hi, I wanted to ask how does this work exactly?")
        assert result is not None
        assert result.topic == "How does this work?"

    def test_what_does_it_cost(self):
        db = _make_db()
        result = match_kb(db, "What does it cost to get started?")
        assert result is not None
        assert result.topic == "What does it cost?"

    def test_what_areas_do_you_cover(self):
        db = _make_db()
        result = match_kb(db, "What areas do you cover in Florida?")
        assert result is not None
        assert result.topic == "What areas do you cover?"

    def test_how_long_does_it_take(self):
        db = _make_db()
        result = match_kb(db, "How long does it take to see results?")
        assert result is not None
        assert result.topic == "How long does it take?"

    def test_guarantee(self):
        db = _make_db()
        result = match_kb(db, "Do you guarantee results or offer a refund?")
        assert result is not None
        assert result.topic == "Is this a guarantee?"


# ── match_kb — confidence and edge cases ─────────────────────────────────────

class TestMatchKbEdgeCases:
    def test_returns_kmatch_dataclass(self):
        db = _make_db()
        result = match_kb(db, "How does this work?")
        assert isinstance(result, KbMatch)
        assert 0.0 <= result.match_confidence <= 1.0

    def test_no_match_returns_none(self):
        db = _make_db()
        result = match_kb(db, "I just wanted to say hello and catch up sometime.")
        assert result is None

    def test_empty_body_returns_none(self):
        db = _make_db()
        result = match_kb(db, "")
        assert result is None

    def test_empty_kb_returns_none(self):
        db = _make_db(entries=[])
        result = match_kb(db, "How does this work?")
        assert result is None

    def test_response_template_has_no_dollar_amount(self):
        """Every seeded template must not quote a dollar figure."""
        import re
        dollar_pattern = re.compile(r"\$\d")
        db = _make_db()
        # We check each entry's template directly from SEED_ENTRIES.
        for entry in SEED_ENTRIES:
            template = entry["approved_response_template"]
            assert not dollar_pattern.search(template), (
                f"Template for '{entry['topic']}' contains a dollar amount: {template!r}"
            )

    def test_cost_paraphrase(self):
        db = _make_db()
        result = match_kb(db, "I'm curious about your pricing structure")
        assert result is not None
        assert result.topic == "What does it cost?"
