"""Tests for Blackink outbound prompt variant registry."""

import pytest

from config.prompt_variants import (
    CHAMPION_VARIANTS,
    CHALLENGER_VARIANTS,
    get_champion_prompt,
)

# ---------------------------------------------------------------------------
# Champion registry
# ---------------------------------------------------------------------------

def test_champion_exists_for_all_email_touches():
    for touch in (1, 3, 5):
        assert touch in CHAMPION_VARIANTS, f"No champion for touch {touch}"


def test_each_champion_has_system_template():
    for touch, variant in CHAMPION_VARIANTS.items():
        assert "system_template" in variant, f"Touch {touch} champion missing system_template"
        assert len(variant["system_template"]) > 50


def test_champion_is_marked():
    for touch, variant in CHAMPION_VARIANTS.items():
        assert variant["is_champion"] is True, f"Touch {touch} champion not marked is_champion"


def test_champion_is_golden_set_approved():
    for touch, variant in CHAMPION_VARIANTS.items():
        assert variant["golden_set_approved"] is True, f"Touch {touch} champion not golden_set_approved"


# ---------------------------------------------------------------------------
# Compliance: {client_firm} in all champion prompts
# ---------------------------------------------------------------------------

def test_client_firm_tag_in_all_champion_prompts():
    for touch, variant in CHAMPION_VARIANTS.items():
        assert "{client_firm}" in variant["system_template"], (
            f"Touch {touch} champion prompt missing {{client_firm}} — compliance violation"
        )


# ---------------------------------------------------------------------------
# get_champion_prompt
# ---------------------------------------------------------------------------

def test_get_champion_prompt_returns_string():
    for touch in (1, 3, 5):
        prompt = get_champion_prompt(touch)
        assert isinstance(prompt, str)
        assert len(prompt) > 0


def test_get_champion_prompt_raises_for_manual_touch_2():
    with pytest.raises(KeyError, match="Touch 2"):
        get_champion_prompt(2)


def test_get_champion_prompt_raises_for_manual_touch_4():
    with pytest.raises(KeyError, match="Touch 4"):
        get_champion_prompt(4)


def test_get_champion_prompt_raises_for_unknown_touch():
    with pytest.raises(KeyError):
        get_champion_prompt(99)


# ---------------------------------------------------------------------------
# Challenger guard — no unapproved challengers in live pool
# ---------------------------------------------------------------------------

def test_no_unapproved_challengers_in_live_pool():
    for touch, challengers in CHALLENGER_VARIANTS.items():
        for c in challengers:
            assert c["golden_set_approved"] is True, (
                f"Touch {touch} challenger '{c['name']}' in live pool but not golden_set_approved"
            )


# ---------------------------------------------------------------------------
# Touch identity
# ---------------------------------------------------------------------------

def test_champion_touch_numbers_match_registry_keys():
    for touch, variant in CHAMPION_VARIANTS.items():
        assert variant["touch"] == touch
