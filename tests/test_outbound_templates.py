"""Tests for outbound template validation — Dev 4 / Week 0 AC #5 coverage."""

import pytest
from src.services.outbound_templates import (
    validate_template,
    require_client_firm_tag,
    resolve_tags,
    build_instantly_sequence,
)

# ---------------------------------------------------------------------------
# require_client_firm_tag
# ---------------------------------------------------------------------------

def test_require_client_firm_tag_passes():
    require_client_firm_tag("Hi, this is {client_firm} reaching out.")


def test_require_client_firm_tag_raises_when_absent():
    with pytest.raises(ValueError, match="client_firm"):
        require_client_firm_tag("Hi, no firm tag here.")


def test_require_client_firm_tag_empty_string_raises():
    with pytest.raises(ValueError):
        require_client_firm_tag("")


# ---------------------------------------------------------------------------
# validate_template
# ---------------------------------------------------------------------------

_VALID_STEPS = [
    {
        "step_number": 1,
        "delay_days": 0,
        "subject": "Speed audit for {company} — from {client_firm}",
        "body": "Hi {first_name}, {client_firm} found you lose ${loss_dollars}/yr.",
    }
]

_MISSING_CLIENT_FIRM_STEPS = [
    {
        "step_number": 1,
        "delay_days": 0,
        "subject": "Speed audit for {company}",
        "body": "Hi {first_name}, no firm tag here.",
    }
]

_UNKNOWN_TAG_STEPS = [
    {
        "step_number": 1,
        "delay_days": 0,
        "subject": "From {client_firm}",
        "body": "Hi {unknown_field}, check {client_firm}.",
    }
]


def test_validate_template_valid():
    assert validate_template(_VALID_STEPS) == []


def test_validate_template_missing_client_firm():
    errors = validate_template(_MISSING_CLIENT_FIRM_STEPS)
    assert any("client_firm" in e for e in errors)


def test_validate_template_unknown_tag():
    errors = validate_template(_UNKNOWN_TAG_STEPS)
    assert any("unknown_field" in e for e in errors)


def test_validate_template_multiple_steps_each_checked():
    steps = _MISSING_CLIENT_FIRM_STEPS + _VALID_STEPS
    errors = validate_template(steps)
    # Step 1 missing, step 2 valid — exactly one error
    assert len([e for e in errors if "client_firm" in e]) == 1


# ---------------------------------------------------------------------------
# resolve_tags
# ---------------------------------------------------------------------------

def test_resolve_tags_substitutes_known():
    result = resolve_tags("Hi {first_name} from {client_firm}.", {"first_name": "Jane", "client_firm": "SunCoast PM"})
    assert result == "Hi Jane from SunCoast PM."


def test_resolve_tags_leaves_missing_as_placeholder():
    result = resolve_tags("Hi {first_name}.", {})
    assert result == "Hi {first_name}."


# ---------------------------------------------------------------------------
# build_instantly_sequence — dispatch-time gate
# ---------------------------------------------------------------------------

_CONTEXT = {
    "client_firm": "SunCoast PM",
    "first_name": "Jane",
    "company": "Acme Properties",
    "loss_dollars": "14200",
}


def test_build_instantly_sequence_valid():
    result = build_instantly_sequence(_VALID_STEPS, _CONTEXT)
    assert len(result) == 1
    assert result[0]["type"] == "email"
    assert "SunCoast PM" in result[0]["variants"][0]["body"]


def test_build_instantly_sequence_raises_if_client_firm_missing():
    with pytest.raises(ValueError, match="client_firm"):
        build_instantly_sequence(_MISSING_CLIENT_FIRM_STEPS, _CONTEXT)


def test_build_instantly_sequence_delay_preserved():
    steps = [{**_VALID_STEPS[0], "delay_days": 4}]
    result = build_instantly_sequence(steps, _CONTEXT)
    assert result[0]["delay"] == 4
