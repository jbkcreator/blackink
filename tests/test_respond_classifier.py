"""Tests for the Respond intent classifier.

Tests for:
  - Deterministic UNSUBSCRIBE detection (fast-path, no LLM)
  - Deterministic LEGAL_GRIEF detection (fast-path, no LLM)
  - Deterministic classes produce confidence=1.0
  - Deterministic fast-path takes priority over LLM
  - LLM fallback behaviour when ANTHROPIC_API_KEY is not set
  - Idempotency key computation
  - Queue message parsing round-trip
"""
import hashlib

import pytest

from src.agents.respond.classifier import ClassificationResult, classify
from src.agents.respond.intents import Intent, is_legal_grief, is_unsubscribe


# ---------------------------------------------------------------------------
# Deterministic UNSUBSCRIBE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,subject", [
    ("Please unsubscribe me from your list.", ""),
    ("I'd like to opt out of these emails.", ""),
    ("Please remove me. I'm not interested.", ""),
    ("Stop emailing me.", ""),
    ("Take me off your mailing list.", ""),
    ("Do not contact me again.", ""),
    ("", "Unsubscribe request"),
    ("I don't want any more messages from you.", ""),
    ("opt-out", ""),
])
def test_is_unsubscribe_true(body, subject):
    assert is_unsubscribe(body, subject) is True


@pytest.mark.parametrize("body,subject", [
    ("I'm interested in learning more about your services.", ""),
    ("What are your management fees?", ""),
    ("I have 5 properties and I'm considering switching.", ""),
    ("", ""),
])
def test_is_unsubscribe_false(body, subject):
    assert is_unsubscribe(body, subject) is False


# ---------------------------------------------------------------------------
# Deterministic LEGAL_GRIEF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,subject", [
    ("I've spoken to my attorney and we plan to file a complaint.", ""),
    ("This is harassment. I will sue you.", ""),
    ("Cease and desist immediately.", ""),
    ("My lawyer will be in touch.", ""),
    ("I'm filing a fair housing complaint.", ""),
    ("", "Cease and Desist Notice"),
    ("We are prepared to take legal action.", ""),
    ("I'm going to report you to the regulatory body.", ""),
])
def test_is_legal_grief_true(body, subject):
    assert is_legal_grief(body, subject) is True


@pytest.mark.parametrize("body,subject", [
    ("I have questions about the management contract.", ""),
    ("Can we schedule a call?", ""),
    ("", ""),
])
def test_is_legal_grief_false(body, subject):
    assert is_legal_grief(body, subject) is False


# ---------------------------------------------------------------------------
# classify() fast-path — no LLM needed
# ---------------------------------------------------------------------------

def test_classify_unsubscribe_returns_deterministic():
    result = classify(body_text="Please unsubscribe me.", subject="")
    assert result.intent == Intent.UNSUBSCRIBE
    assert result.confidence == 1.0
    assert result.meta.get("path") == "deterministic"


def test_classify_legal_grief_returns_deterministic():
    result = classify(body_text="My attorney is sending a cease and desist.", subject="")
    assert result.intent == Intent.LEGAL_GRIEF
    assert result.confidence == 1.0
    assert result.meta.get("path") == "deterministic"


def test_classify_unsubscribe_takes_priority_over_llm(monkeypatch):
    # Even if we monkeypatched the LLM to return something, the deterministic
    # path fires first and the LLM is never called.
    called = []

    def fake_llm(*args, **kwargs):
        called.append(True)
        return ClassificationResult(intent=Intent.QUESTION, confidence=0.9, reasoning="LLM said so")

    monkeypatch.setattr("src.agents.respond.classifier._classify_with_llm", fake_llm)
    result = classify(body_text="opt out please", subject="")

    assert result.intent == Intent.UNSUBSCRIBE
    assert called == []  # LLM was never invoked


def test_legal_grief_takes_priority_over_unsubscribe():
    # A message with both legal-threat and opt-out language must escalate, not suppress.
    result = classify(
        body_text="My attorney is sending a cease and desist. Stop contacting me.",
        subject="",
    )
    assert result.intent == Intent.LEGAL_GRIEF
    assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# LLM fallback when API key is absent
# ---------------------------------------------------------------------------

def test_classify_fallback_when_no_api_key(monkeypatch):
    monkeypatch.setattr(
        "src.agents.respond.classifier._classify_with_llm",
        lambda body, subject, sender: ClassificationResult(
            intent=Intent.NURTURE,
            confidence=0.0,
            reasoning="Classifier error — conservative fallback pending manual review.",
        ),
    )
    result = classify(body_text="I'm thinking about it.", subject="", sender_email="")
    assert result.intent == Intent.NURTURE
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Group D / D-10 — the fallback result must carry a distinguishing flag, or
# an errored classification is silently indistinguishable from a genuine
# NURTURE (worker.py's requires_human_review / alert logic depends on it).
# ---------------------------------------------------------------------------

def test_fallback_carries_distinguishing_meta_flag():
    from src.agents.respond.classifier import _fallback

    result = _fallback()
    assert result.intent == Intent.NURTURE
    assert result.confidence == 0.0
    assert result.meta.get("path") == "fallback"


def test_classify_with_llm_returns_fallback_flag_when_sdk_missing(monkeypatch):
    import src.agents.respond.classifier as classifier_mod

    monkeypatch.setattr(classifier_mod, "anthropic", None)
    result = classifier_mod._classify_with_llm("body", "subject", "a@b.com")
    assert result.intent == Intent.NURTURE
    assert result.meta.get("path") == "fallback"


def test_classify_with_llm_returns_fallback_flag_when_api_key_unset(monkeypatch):
    import src.agents.respond.classifier as classifier_mod
    from types import SimpleNamespace

    monkeypatch.setattr(classifier_mod, "anthropic", object())
    monkeypatch.setattr(
        classifier_mod, "get_settings",
        lambda: SimpleNamespace(anthropic_api_key=None),
    )
    result = classifier_mod._classify_with_llm("body", "subject", "a@b.com")
    assert result.intent == Intent.NURTURE
    assert result.meta.get("path") == "fallback"


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------

def test_idempotency_key_is_deterministic():
    from src.api.inbound_router import _idempotency_key

    dest = "replies@acme-pm.getblackink.com"
    msg_id = "<abc123@mail.gmail.com>"
    key1 = _idempotency_key(dest, msg_id)
    key2 = _idempotency_key(dest, msg_id)
    assert key1 == key2
    assert len(key1) == 64  # SHA-256 hex digest


def test_idempotency_key_changes_with_different_inputs():
    from src.api.inbound_router import _idempotency_key

    k1 = _idempotency_key("replies@a.getblackink.com", "<msg1@x>")
    k2 = _idempotency_key("replies@b.getblackink.com", "<msg1@x>")
    k3 = _idempotency_key("replies@a.getblackink.com", "<msg2@x>")
    assert k1 != k2
    assert k1 != k3
    assert k2 != k3


# ---------------------------------------------------------------------------
# Queue round-trip (unit, no Redis)
# ---------------------------------------------------------------------------

def test_queue_parse_round_trip():
    from src.agents.respond.queue import _parse

    fields = {
        "db_id": "42",
        "client_id": "acme-pm",
        "idempotency_key": "abc123",
    }
    msg = _parse("1234-0", fields, delivery_count=2)
    assert msg.db_id == 42
    assert msg.client_id == "acme-pm"
    assert msg.idempotency_key == "abc123"
    assert msg.delivery_count == 2


# ---------------------------------------------------------------------------
# _call_model — markdown fence stripping
# ---------------------------------------------------------------------------

def test_call_model_strips_markdown_fences(monkeypatch):
    from src.agents.respond.classifier import _call_model

    class FakeContent:
        text = '```json\n{"intent": "HOT_LEAD", "confidence": 0.9, "reasoning": "test"}\n```'

    class FakeResponse:
        content = [FakeContent()]

    class FakeClient:
        def messages(self):
            pass
        class messages:
            @staticmethod
            def create(**kwargs):
                return FakeResponse()

    result = _call_model(FakeClient(), "claude-haiku-4-5-20251001", "test body")
    assert result.intent == Intent.HOT_LEAD
    assert result.confidence == 0.9


# ---------------------------------------------------------------------------
# Sonnet fallback when Haiku fails
# ---------------------------------------------------------------------------

def test_sonnet_fallback_on_haiku_failure(monkeypatch):
    import src.agents.respond.classifier as cls_module

    calls = []

    def fake_call_model(client, model, user_content):
        calls.append(model)
        if "haiku" in model:
            raise ValueError("haiku failed")
        return ClassificationResult(intent=Intent.QUESTION, confidence=0.8, reasoning="sonnet said so")

    class FakeSecret:
        def get_secret_value(self): return "test-key"

    class FakeSettings:
        anthropic_api_key = FakeSecret()

    class FakeClient:
        pass

    class FakeAnthropicModule:
        class Anthropic:
            def __init__(self, api_key=None):
                pass

    monkeypatch.setattr(cls_module, "_call_model", fake_call_model)
    monkeypatch.setattr("src.agents.respond.classifier.get_settings", lambda: FakeSettings())
    monkeypatch.setattr("src.agents.respond.classifier.anthropic", FakeAnthropicModule)

    result = cls_module._classify_with_llm("thinking about it", "", "")

    assert result.intent == Intent.QUESTION
    assert result.confidence == 0.8
    assert cls_module._CLASSIFIER_MODEL in calls[0]
    assert cls_module._CLASSIFIER_FALLBACK_MODEL in calls[1]
