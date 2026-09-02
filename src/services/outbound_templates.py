"""Outbound template validation for Blackink.

Ported from Forced Action's email_templates.py variable-validation pattern
(src/services/email_templates.py) and adapted for Blackink's requirements:

- Single-brace {var} syntax (Instantly merge tags), not FA's {{double}} style.
- {client_firm} is a REQUIRED tag on every outbound template — enforced at
  save time and re-asserted at dispatch. This is a compliance invariant:
  recipients must always see which firm is contacting them.
- Whitelist covers Contact fields available at dispatch time.
"""

import re
from typing import Optional

# Canonical merge-tag whitelist — maps tag name → Contact/Company field
ALLOWED_TAGS: dict[str, str] = {
    "client_firm":   "client display_name (REQUIRED — compliance)",
    "first_name":    "contacts.first_name",
    "last_name":     "contacts.last_name",
    "company":       "companies.company_name",
    "city":          "companies.market_metro",
    "door_count":    "companies.door_count_est",
    "audit_speed":   "events payload: audit_speed_score_sec",
    "loss_dollars":  "events payload: audit_loss_dollars_est",
    "video_url":     "sendspark dynamic video landing page URL",
}

REQUIRED_TAGS: frozenset[str] = frozenset({"client_firm"})

_TAG_RE = re.compile(r"\{(\w+)\}")


def extract_tags(text: str) -> list[str]:
    """Return unique tag names found in text, order-preserved."""
    return list(dict.fromkeys(_TAG_RE.findall(text)))


def require_client_firm_tag(text: str) -> None:
    """Raise ValueError if {client_firm} is absent from text.

    Single entry point called at both template-save and pre-dispatch so the
    invariant is checked at every gate without duplicating logic.
    """
    if "{client_firm}" not in text:
        raise ValueError(
            "Outbound template missing required {client_firm} tag. "
            "All outbound templates must identify the sending firm."
        )


def validate_template(steps: list[dict]) -> list[str]:
    """Validate all tags in every step's subject + body.

    Checks:
    1. {client_firm} present in at least one field per step (required tag).
    2. No unknown tags outside ALLOWED_TAGS.

    Returns list of error strings (empty = valid).
    Ported from FA's validate_variables() pattern.
    """
    errors: list[str] = []
    for i, step in enumerate(steps, start=1):
        step_text = " ".join(step.get(f, "") for f in ("subject", "body"))

        # Required tag check
        if "{client_firm}" not in step_text:
            errors.append(f"Step {i}: missing required {{client_firm}} tag")

        # Unknown tag check
        for tag in extract_tags(step_text):
            if tag not in ALLOWED_TAGS:
                errors.append(f"Step {i}: unknown tag {{{tag}}}")

    return errors


def resolve_tags(template_str: str, context: dict) -> str:
    """Replace {tag} placeholders with context values.

    Unknown tags are left as-is (not silently dropped) so missing context
    keys surface as visible broken placeholders rather than empty strings.
    Ported from FA's resolve_contact_variables() pattern.
    """
    def replacer(match: re.Match) -> str:
        key = match.group(1)
        value = context.get(key)
        if value is None:
            return match.group(0)  # leave broken placeholder visible
        return str(value)

    return _TAG_RE.sub(replacer, template_str)


def build_instantly_sequence(steps: list[dict], context: dict) -> list[dict]:
    """Resolve tags and expand steps into Instantly v2 sequence format.

    Asserts {client_firm} present before resolving — dispatch-time hard gate.
    Ported from FA's build_instantly_sequence() shape.

    Input step:  {step_number, delay_days, subject, body}
    Output step: {type, delay, variants:[{subject, body}]}
    """
    resolved = []
    for step in steps:
        subject = step.get("subject", "")
        body = step.get("body", "")

        # Dispatch-time compliance gate — re-check even if validated at save
        require_client_firm_tag(subject + " " + body)

        resolved.append({
            "type": "email",
            "delay": step.get("delay_days", 0),
            "variants": [{
                "subject": resolve_tags(subject, context),
                "body": resolve_tags(body, context),
            }],
        })

    return resolved
