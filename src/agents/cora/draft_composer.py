"""Cora draft composer — generate a 5-touch cold email sequence via Claude.

Prompt instructs the LLM to write templates using Instantly merge-tag syntax
({first_name}, {company}, {client_firm}, {audit_speed}, {loss_dollars},
{video_url}). Tags are resolved later by the relay worker — NOT here.

Fail-open: if ANTHROPIC_API_KEY is unset or the API call fails, falls back to
a hardcoded audit-finding sequence so the Slack card still posts and the
campaign can proceed. The fallback is clearly logged.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from config.settings import get_settings
from src.services.outbound_templates import validate_template

logger = logging.getLogger(__name__)

_MODEL          = "claude-haiku-4-5-20251001"
_FALLBACK_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = """\
You are a B2B cold email specialist for Blackink, a property management growth SaaS.
You write concise, credible cold email sequences that lead with a specific audit finding
rather than generic pitches. Every email must identify the sending firm clearly.
"""

_USER_PROMPT_TEMPLATE = """\
Write a 5-touch cold email sequence targeting {contact_name} at {company_name},
a property management firm in {city} managing approximately {door_count} doors.

AUDIT FINDING: We submitted a test inquiry to their contact form.
Response time: {audit_speed}
Estimated annual owner-lead revenue at risk from that delay: {loss_dollars}

Sequence rules:
- Touch 1 (Day 0): lead with the specific audit finding; include {{video_url}}
- Touch 2 (Day 3): different angle, lighter follow-up
- Touch 3 (Day 7): social proof or case study angle
- Touch 4 (Day 12): "last try" urgency
- Touch 5 (Day 18): clean break-up / offer to reconnect later
- Subject lines: ≤50 characters, no emoji, no ALL-CAPS
- Body: ≤140 words per touch; conversational, not salesy
- Sender is identified as {{client_firm}} on every touch (compliance requirement)

Merge tags to use (use EXACTLY these names in single curly braces):
  {{first_name}} — recipient first name (REQUIRED in every body)
  {{company}}    — company name
  {{client_firm}} — sending firm name (REQUIRED in every subject or body)
  {{audit_speed}} — detected response time
  {{loss_dollars}} — annual revenue at risk
  {{video_url}}  — personalised video link (Touch 1 only)

Return ONLY a JSON array with exactly 5 objects, no prose before or after:
[
  {{"step_number": 1, "delay_days": 0, "subject": "...", "body": "..."}},
  {{"step_number": 2, "delay_days": 3, "subject": "...", "body": "..."}},
  ...
]
"""

# Hardcoded fallback — used when Anthropic API is unavailable.
_FALLBACK_SEQUENCE: List[dict] = [
    {
        "step_number": 1,
        "delay_days":  0,
        "subject":     "Your contact form: {audit_speed} reply time",
        "body": (
            "Hi {first_name},\n\n"
            "We ran a response-time audit on {company}'s contact form and measured a "
            "{audit_speed} reply time. Based on industry benchmarks for PM firms in your "
            "market, that delay puts roughly {loss_dollars}/year in owner leads at risk.\n\n"
            "I put together a 2-min audit walkthrough -- {video_url}\n\n"
            "Worth a quick look? This is {client_firm} -- happy to show you how our "
            "clients are responding in under 5 minutes, 24/7.\n\nBest,\n{client_firm} Team"
        ),
    },
    {
        "step_number": 2,
        "delay_days":  3,
        "subject":     "Quick follow-up on your audit, {first_name}",
        "body": (
            "Hi {first_name},\n\n"
            "Just circling back on the response-time audit I sent over. PM firms in your "
            "market that have tightened their response window to <5 minutes see 20-40% "
            "more owner conversions from the same lead volume.\n\n"
            "Would a 15-minute call make sense? -- {client_firm}"
        ),
    },
    {
        "step_number": 3,
        "delay_days":  7,
        "subject":     "How {company} compares to top PM firms",
        "body": (
            "Hi {first_name},\n\n"
            "One of our clients in a similar market cut their average response from "
            "6 hours to 4 minutes and added 11 new owner contracts in the first quarter.\n\n"
            "The audit we ran on {company} shows {audit_speed} -- there's a real gap "
            "to close here.\n\nHappy to show you the playbook. -- {client_firm}"
        ),
    },
    {
        "step_number": 4,
        "delay_days":  12,
        "subject":     "Last note on the {audit_speed} audit",
        "body": (
            "Hi {first_name},\n\n"
            "I'll keep this short -- the {audit_speed} response time we measured at "
            "{company} is costing you roughly {loss_dollars} per year in owner leads "
            "that move on before you reply.\n\n"
            "If now isn't the right time, just say the word and I'll reach out in Q2 "
            "instead. -- {client_firm}"
        ),
    },
    {
        "step_number": 5,
        "delay_days":  18,
        "subject":     "Closing the loop, {first_name}",
        "body": (
            "Hi {first_name},\n\n"
            "I'll stop reaching out -- clearly not the right moment for {company}.\n\n"
            "If that ever changes and you'd like to revisit the audit findings, "
            "I'm easy to find. Wishing you a strong quarter.\n\n-- {client_firm}"
        ),
    },
]


class CompositionError(Exception):
    """Raised when LLM generation fails AND the fallback is also broken."""


def compose(
    company_name:  str,
    contact_name:  str,
    city:          str,
    door_count:    str,
    audit_speed:   str,
    loss_dollars:  str,
) -> List[dict]:
    """Return 5 step dicts. Falls back to _FALLBACK_SEQUENCE on API failure."""
    prompt = _USER_PROMPT_TEMPLATE.format(
        company_name=company_name,
        contact_name=contact_name,
        city=city,
        door_count=door_count,
        audit_speed=audit_speed,
        loss_dollars=loss_dollars,
    )

    settings = get_settings()
    if settings.anthropic_api_key:
        try:
            steps = _call_api(prompt, settings.anthropic_api_key.get_secret_value())
            errors = validate_template(steps)
            if errors:
                logger.warning(
                    "draft_composer: LLM sequence failed validation %s — falling back",
                    errors,
                )
            else:
                logger.info("draft_composer: LLM sequence generated %d steps", len(steps))
                return steps
        except Exception as exc:
            logger.warning("draft_composer: LLM call failed %s — falling back", exc)
    else:
        logger.warning("draft_composer: ANTHROPIC_API_KEY unset — using fallback sequence")

    errors = validate_template(_FALLBACK_SEQUENCE)
    if errors:
        raise CompositionError(f"Fallback sequence invalid: {errors}")
    return _FALLBACK_SEQUENCE


def _call_api(prompt: str, api_key: str) -> List[dict]:
    import anthropic  # noqa: F401 — optional; absent in test environments

    client = anthropic.Anthropic(api_key=api_key)
    for model in (_MODEL, _FALLBACK_MODEL):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            return _parse_response(raw)
        except Exception as exc:
            logger.warning("draft_composer: model=%s failed: %s", model, exc)
            continue
    raise CompositionError("All models failed")


def _parse_response(raw: str) -> List[dict]:
    """Extract the JSON array from the model response, tolerating markdown fences."""
    # Strip ```json ... ``` if present
    match = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if match:
        raw = match.group(1).strip()

    steps: List[Any] = json.loads(raw)
    if not isinstance(steps, list) or len(steps) != 5:
        raise ValueError(f"Expected list of 5 steps, got {type(steps).__name__}({len(steps) if isinstance(steps, list) else '?'})")

    result = []
    for s in steps:
        result.append({
            "step_number": int(s["step_number"]),
            "delay_days":  int(s["delay_days"]),
            "subject":     str(s["subject"]),
            "body":        str(s["body"]),
        })
    return result
