"""Tests for src/services/inbound_attribution.py (Task 3.1.3 / ticket 20).

Verifies attribution behavior:
  - Tier 1: In-Reply-To → sequence_touch_dispatches → contact
  - Tier 2: from_address → contacts.email scoped to client
  - Unattributed fallback
  - BCC-echo detection (is_bcc_echo)
  - Client resolution from alias (resolve_client_from_alias)
  - from_address parsing (display name stripping)

FakeSession pattern — no live DB required.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.services.inbound_attribution import (
    AttributionResult,
    attribute,
    is_bcc_echo,
    resolve_client_from_alias,
    strip_display_name,
)


# ── strip_display_name ───────────────────────────────────────────────────────


def test_strip_display_name_angle_bracket_form():
    assert strip_display_name("Jane Doe <jane@acme.com>") == "jane@acme.com"


def test_strip_display_name_bare_email_unchanged():
    assert strip_display_name("jane@acme.com") == "jane@acme.com"


def test_strip_display_name_trims_whitespace():
    assert strip_display_name("  bob@smith.com  ") == "bob@smith.com"


def test_strip_display_name_empty():
    assert strip_display_name("") == ""


# ── FakeSession helpers ──────────────────────────────────────────────────────


def _session_with_first(row=None):
    """Session whose execute().mappings().first() returns `row`."""
    session = MagicMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = row
    result.first.return_value = row
    session.execute.return_value = result
    return session


def _session_sequence(returns):
    """Session whose successive execute calls return items from `returns`."""
    session = MagicMock()
    side_effects = []
    for ret in returns:
        result = MagicMock()
        result.mappings.return_value.first.return_value = ret
        result.first.return_value = ret
        side_effects.append(result)
    session.execute.side_effect = side_effects
    return session


# ── resolve_client_from_alias ────────────────────────────────────────────────


def test_resolve_client_known_active():
    """Valid alias → returns client_id handle."""
    row = SimpleNamespace(client_id="client-abc")
    session = _session_with_first(row=row)
    result = resolve_client_from_alias(session, "client-abc@inbound.getblackink.com")
    assert result == "client-abc"


def test_resolve_client_unknown():
    """Unknown alias handle → None."""
    session = _session_with_first(row=None)
    result = resolve_client_from_alias(session, "unknown@inbound.getblackink.com")
    assert result is None


def test_resolve_client_malformed_no_at():
    session = _session_with_first(row=None)
    result = resolve_client_from_alias(session, "noemail")
    assert result is None


def test_resolve_client_empty_handle():
    session = _session_with_first(row=None)
    result = resolve_client_from_alias(session, "@inbound.getblackink.com")
    assert result is None


# ── is_bcc_echo ───────────────────────────────────────────────────────────────


def test_bcc_echo_detected_when_message_id_matches_outbound():
    """If the message_id is in sequence_touch_dispatches, it is a BCC echo."""
    row = SimpleNamespace()  # any truthy value
    session = _session_with_first(row=row)
    assert is_bcc_echo(session, "<sent-msg-id@mail.example.com>") is True


def test_not_bcc_echo_when_not_in_outbound():
    session = _session_with_first(row=None)
    assert is_bcc_echo(session, "<unknown@msg.example.com>") is False


# ── attribute — tier 1 (In-Reply-To) ─────────────────────────────────────────


def test_attribute_tier1_in_reply_to_match():
    """In-Reply-To matches a SENT dispatch → attributed to run + contact."""
    dispatch_row = {
        "run_id": "run-uuid-1234",
        "touch_step": 1,
        "client_id": "client-from-dispatch",
        "contact_id": 99,
        "first_name": "Jane",
        "last_name": "Doe",
        "company_name": "Acme PM",
    }
    # Tier 1 returns a row; tier 2 is never called
    session = _session_with_first(row=dispatch_row)

    result = attribute(
        session,
        client_id="client-alias",
        in_reply_to="<msg-id-123@example.com>",
        from_address="jane@example.com",
    )

    assert result.attribution_status == "attributed"
    assert result.contact_id == 99
    assert result.run_id == "run-uuid-1234"
    assert result.touch_step == 1
    assert result.contact_name == "Jane Doe"
    assert result.firm_name == "Acme PM"
    # client_id comes from the dispatch row
    assert result.client_id == "client-from-dispatch"


# ── attribute — tier 2 (sender email) ────────────────────────────────────────


def test_attribute_tier2_sender_email_match():
    """No In-Reply-To match → fall back to sender email."""
    contact_row = {
        "contact_id": 55,
        "first_name": "Bob",
        "last_name": "Smith",
        "company_name": "Smith Properties",
    }
    # First call (tier 1) → None; second call (tier 2) → contact_row
    session = _session_sequence([None, contact_row])

    result = attribute(
        session,
        client_id="client-xyz",
        in_reply_to="<unknown-msg@example.com>",
        from_address="bob@smith.com",
    )

    assert result.attribution_status == "attributed"
    assert result.contact_id == 55
    assert result.run_id is None  # tier 2 has no run
    assert result.contact_name == "Bob Smith"
    assert result.client_id == "client-xyz"


def test_attribute_tier2_from_address_display_name_stripped():
    """'First Last <email@example.com>' display name is stripped before lookup."""
    contact_row = {"contact_id": 5, "first_name": "A", "last_name": "B", "company_name": "Co"}
    session = _session_sequence([None, contact_row])

    result = attribute(
        session,
        client_id="c",
        in_reply_to=None,
        from_address="Alice Smith <alice@smith.com>",
    )
    # The DB lookup should use the stripped email
    # We can verify by checking the SQL params passed to execute for tier2
    # (second call)
    tier2_params = session.execute.call_args_list[0][0][1]
    assert tier2_params.get("email") == "alice@smith.com"


# ── attribute — unattributed ──────────────────────────────────────────────────


def test_attribute_unattributed_when_both_tiers_fail():
    """Neither tier matches → unattributed result."""
    session = _session_sequence([None, None])

    result = attribute(
        session,
        client_id="client-abc",
        in_reply_to="<no-match@msg.com>",
        from_address="unknown@stranger.com",
    )

    assert result.attribution_status == "unattributed"
    assert result.contact_id is None
    assert result.run_id is None
    assert result.client_id == "client-abc"


def test_attribute_unattributed_no_in_reply_to():
    """No In-Reply-To and no email match → unattributed."""
    session = _session_sequence([None])

    result = attribute(
        session,
        client_id="client-abc",
        in_reply_to=None,
        from_address="unknown@stranger.com",
    )

    assert result.attribution_status == "unattributed"
