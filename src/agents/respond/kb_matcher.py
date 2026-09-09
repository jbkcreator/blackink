"""KB matching logic for Subtask 2.1.3.

Loads active knowledge_base_entries from DB, scores each entry's
trigger_patterns against the cleaned message body using rapidfuzz
token-set ratio (handles word-order variance and partial phrasing),
and returns the best match above the entry's min_confidence_threshold.

No I/O side effects — callers own the DB session and the routing decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from rapidfuzz import fuzz
from sqlalchemy import text


@dataclass
class KbMatch:
    entry_id: int
    topic: str
    response_template: str
    match_confidence: float   # 0.0 – 1.0, normalised from rapidfuzz 0–100 score


def _clean(text_body: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text_body = text_body.lower()
    text_body = re.sub(r"[^a-z0-9\s]", " ", text_body)
    return re.sub(r"\s+", " ", text_body).strip()


def _score_entry(cleaned_body: str, patterns: list[str]) -> float:
    """Return the best score across all patterns, normalised 0–1.

    Uses partial_ratio: slides the shorter string over all same-length windows
    of the longer string and returns the best alignment score. This correctly
    handles a trigger phrase (e.g. "how does this work") embedded inside a
    longer owner message ("hi I wanted to ask how does this work exactly?")
    without the false-positive inflation that token_set_ratio produces for
    short patterns sharing common stopwords with unrelated messages.
    """
    best = 0.0
    for pattern in patterns:
        cleaned_pattern = _clean(pattern)
        score = fuzz.partial_ratio(cleaned_body, cleaned_pattern)
        if score > best:
            best = score
    return best / 100.0


def match_kb(db: Any, body_text: str) -> Optional[KbMatch]:
    """Return the highest-scoring active KB entry, or None if no entry clears its threshold.

    db must be an open SQLAlchemy session.
    """
    rows = db.execute(
        text(
            "SELECT entry_id, topic, trigger_patterns, approved_response_template, "
            "       min_confidence_threshold "
            "FROM knowledge_base_entries "
            "WHERE is_active = TRUE "
            "ORDER BY entry_id"
        )
    ).mappings().fetchall()

    if not rows:
        return None

    cleaned = _clean(body_text or "")
    best_match: Optional[KbMatch] = None
    best_score = 0.0

    for row in rows:
        patterns = row["trigger_patterns"] or []
        threshold = float(row["min_confidence_threshold"])
        score = _score_entry(cleaned, patterns)

        if score >= threshold and score > best_score:
            best_score = score
            best_match = KbMatch(
                entry_id=row["entry_id"],
                topic=row["topic"],
                response_template=row["approved_response_template"],
                match_confidence=score,
            )

    return best_match
