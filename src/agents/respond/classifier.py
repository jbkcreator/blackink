"""10-class intent classifier for the Respond Reply Triage Agent.

Fast-path (no LLM):
  UNSUBSCRIBE and LEGAL_GRIEF are detected via deterministic keyword
  patterns in src.agents.respond.intents. If either matches, a
  confidence=1.0 result is returned immediately — the LLM is never called.

LLM path (all other intents):
  Calls Anthropic messages API (claude-haiku-4-5-20251001). Returns a
  structured JSON envelope: {intent, confidence, reasoning}.

Fallback on any API or parse error:
  NURTURE with confidence=0.0. Conservative — keeps the message in the
  pipeline without auto-acting on it, unlike COMPLAINT or UNSUBSCRIBE
  which trigger immediate downstream actions.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from src.agents.respond.intents import Intent, is_legal_grief, is_unsubscribe

logger = logging.getLogger(__name__)

_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
_CLASSIFIER_FALLBACK_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = """\
You are an intent classifier for a B2B property-management outreach platform.
An owner has replied to an outreach email from a property-management firm.
Classify the reply into exactly one of these intent classes. Return ONLY valid JSON — no markdown, no extra text.

Classes:
HOT_LEAD    — Owner explicitly wants to book, schedule, or move forward now.
QUESTION    — Owner asks for specific information: fees, services, process, availability, property types.
OBJECTION   — Owner raises a specific concern to overcome before committing (price, bad experience, wrong timing).
LATER       — Owner wants to re-engage in the future but is currently committed elsewhere (active lease, 6+ months on contract).
NURTURE     — Owner is interested but undecided; open to dialogue but needs more time or information.
COMPLAINT   — Owner is unhappy with communication frequency, tone, or a specific experience (but not opting out).
WHALE_OWNER — Owner indicates 10 or more doors, or a large portfolio; same booking flow but flag for priority routing.
PARTNER     — Sender is a real-estate agent, attorney, investor, or referral-source asking about partnership — not an owner inquiry.

Note: UNSUBSCRIBE and LEGAL_GRIEF are handled deterministically before this prompt is called.
Never return either of those classes.

Required JSON format:
{"intent": "<CLASS>", "confidence": <0.0-1.0>, "reasoning": "<one sentence max>"}

If intent is OBJECTION, also include "objection_subtype" with exactly one of:
"pricing", "timing", "existing_agency", "capacity", "other"
"""

_DETERMINISTIC_CLASSES = {Intent.UNSUBSCRIBE, Intent.LEGAL_GRIEF}


@dataclass
class ClassificationResult:
    intent: Intent
    confidence: float
    reasoning: str
    meta: Dict[str, Any] = field(default_factory=dict)
    objection_subtype: Optional[str] = None  # "pricing"|"timing"|"existing_agency"|"capacity"|"other" — OBJECTION only


def _fallback() -> ClassificationResult:
    # Group D / D-10: meta={"path": "fallback"} + confidence==0.0 is the only
    # signal that this NURTURE came from an error, not a real classification —
    # worker.py must treat it as requiring human review and alert, never as
    # an ordinary routed NURTURE. See worker.py's use of confidence == 0.0.
    return ClassificationResult(
        intent=Intent.NURTURE,
        confidence=0.0,
        reasoning="Classifier error — conservative fallback pending manual review.",
        meta={"path": "fallback"},
    )


def classify(
    body_text: str,
    subject: str = "",
    sender_email: str = "",
) -> ClassificationResult:
    """Classify one inbound reply.

    Deterministic fast-path runs first. LLM is only called when neither
    UNSUBSCRIBE nor LEGAL_GRIEF matches.
    """
    safe_body = (body_text or "").strip()
    safe_subject = (subject or "").strip()

    # LEGAL_GRIEF checked first — a message containing both legal-threat and
    # opt-out language must escalate, not merely suppress.
    if is_legal_grief(safe_body, safe_subject):
        return ClassificationResult(
            intent=Intent.LEGAL_GRIEF,
            confidence=1.0,
            reasoning="Deterministic keyword match.",
            meta={"path": "deterministic"},
        )

    if is_unsubscribe(safe_body, safe_subject):
        return ClassificationResult(
            intent=Intent.UNSUBSCRIBE,
            confidence=1.0,
            reasoning="Deterministic keyword match.",
            meta={"path": "deterministic"},
        )

    return _classify_with_llm(safe_body, safe_subject, sender_email)


def _call_model(client: Any, model: str, user_content: str) -> ClassificationResult:
    """Single model call — raises on any error so the caller can retry."""
    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = response.content[0].text.strip()
    # Model sometimes wraps output in markdown fences despite the prompt
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    parsed = json.loads(raw)

    intent_str = parsed.get("intent", "").upper()
    intent = Intent(intent_str)  # raises ValueError for unknown intent

    if intent in _DETERMINISTIC_CLASSES:
        # Spurious match — deterministic check already passed without triggering.
        raise ValueError(f"LLM returned deterministic-only class {intent.value} on LLM path")

    confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    reasoning = str(parsed.get("reasoning", ""))[:500]
    objection_subtype: Optional[str] = None
    if intent == Intent.OBJECTION:
        raw_subtype = str(parsed.get("objection_subtype", "other")).lower().strip()
        objection_subtype = raw_subtype if raw_subtype in {"pricing", "timing", "existing_agency", "capacity"} else "other"
    return ClassificationResult(
        intent=intent,
        confidence=confidence,
        reasoning=reasoning,
        meta={"path": "llm", "model": model, "raw": raw[:1000]},
        objection_subtype=objection_subtype,
    )


try:
    import anthropic  # noqa: F401 — optional; absent in test environments without the SDK
except ImportError:
    anthropic = None  # type: ignore[assignment]

from config.settings import get_settings


def _classify_with_llm(body_text: str, subject: str, sender_email: str) -> ClassificationResult:
    try:
        if anthropic is None:
            logger.warning("respond.classifier: anthropic SDK not installed — returning fallback")
            return _fallback()

        settings = get_settings()
        if not settings.anthropic_api_key:
            logger.warning("respond.classifier: ANTHROPIC_API_KEY not set — returning fallback")
            return _fallback()

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())  # type: ignore[union-attr]
        user_content = (
            f"Subject: {subject or '(none)'}\n"
            f"Sender: {sender_email or '(unknown)'}\n\n"
            f"{body_text or '(empty body)'}"
        )

        try:
            return _call_model(client, _CLASSIFIER_MODEL, user_content)
        except Exception as primary_exc:
            logger.warning(
                "respond.classifier: %s failed (%s) — retrying with %s",
                _CLASSIFIER_MODEL, primary_exc, _CLASSIFIER_FALLBACK_MODEL,
            )
            return _call_model(client, _CLASSIFIER_FALLBACK_MODEL, user_content)

    except Exception:
        logger.exception("respond.classifier: both models failed — returning fallback")
        return _fallback()
