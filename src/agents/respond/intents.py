"""Reply intent taxonomy for the Respond agent.

Two of the 10 classes have deterministic fast-paths that bypass the LLM:
  UNSUBSCRIBE  — keyword match on opt-out language
  LEGAL_GRIEF  — keyword match on legal-threat language

Deterministic checks run BEFORE any LLM call. A pattern match is absolute —
the LLM result is not consulted even if it would disagree.
"""
from __future__ import annotations

import enum
import re


class Intent(str, enum.Enum):
    HOT_LEAD = "HOT_LEAD"        # Ready to book / schedule an appointment now
    QUESTION = "QUESTION"         # Asking for information: fees, services, process, availability
    OBJECTION = "OBJECTION"       # Specific concern to overcome before committing
    LATER = "LATER"               # Re-engage in future; currently under contract or seasonally unavailable
    NURTURE = "NURTURE"           # Interested but undecided; needs more time or information
    UNSUBSCRIBE = "UNSUBSCRIBE"   # Wants to stop receiving communications — deterministic, no LLM
    COMPLAINT = "COMPLAINT"       # Unhappy with communication tone, frequency, or a specific experience
    LEGAL_GRIEF = "LEGAL_GRIEF"   # Legal threat or regulatory complaint language — deterministic, no LLM
    WHALE_OWNER = "WHALE_OWNER"   # 10+ doors or large portfolio; priority routing flag
    PARTNER = "PARTNER"           # Agent, attorney, investor, or referral-source inquiry


_UNSUBSCRIBE_PATTERN = re.compile(
    r"\b("
    r"unsubscribe|"
    r"opt[\s\-]?out|opt me out|"
    r"remove me|"
    r"stop emailing|stop contacting|stop sending|"
    r"do not contact|do not email|"
    r"take me off|off your list|off your mailing list|"
    r"no more emails|no more messages|"
    r"any more emails|any more messages|"
    r"don['’]t email|don['’]t contact|don['’]t reach out|"
    r"please remove|please unsubscribe|"
    r"not interested,?\s+stop|not interested,?\s+please\s+stop"
    r")\b",
    re.IGNORECASE,
)

_LEGAL_PATTERN = re.compile(
    r"\b("
    r"cease and desist|"
    r"attorney|lawyer|solicitor|"
    r"legal action|legal team|legal counsel|my attorney|my lawyer|"
    r"lawsuit|sue you|suing you|suing|litigation|"
    r"harassment|harassing|harass|"
    r"report you|report to|file a complaint|regulatory complaint|"
    r"court|subpoena|defamation|slander|libel|"
    r"fair housing complaint|housing discrimination"
    r")\b",
    re.IGNORECASE,
)


def is_unsubscribe(body_text: str, subject: str = "") -> bool:
    """True if the combined subject + body contains clear opt-out language."""
    return bool(_UNSUBSCRIBE_PATTERN.search(f"{subject} {body_text}"))


def is_legal_grief(body_text: str, subject: str = "") -> bool:
    """True if the combined subject + body contains legal-threat language."""
    return bool(_LEGAL_PATTERN.search(f"{subject} {body_text}"))
