"""Tests for the 3.1.2 additions to src/services/slack/listeners.py:
  - _calling_hours_indicator — green/red boundary, conservative default
  - _dial_task_content_blocks — card layout + pending-score degradation
  - _linkedin_task_content_blocks — note code block, fallback URL
  - _linkedin_action_buttons — url button, modal button, mark-sent
  - sales_reply_content_blocks — reply card with/without opt-out button
  - Touch-1-approval triggers _post_dial_task_after_touch1_approval

Tests only external behaviour (rendered block structure, SQL calls, Bolt ack).
Does NOT test Slack transport or live DB.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.services.slack.listeners import (
    _calling_hours_indicator,
    _calling_hours_label,
    _current_local_time_label,
    _dial_task_content_blocks,
    _linkedin_action_buttons,
    _linkedin_task_content_blocks,
    _post_dial_task_after_touch1_approval,
    sales_reply_content_blocks,
    _simple_action_button_blocks,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fake_order(action_class="DISPATCH_EMAIL_TOUCH", payload=None, action_id=None, client_id="client-x"):
    """Minimal fake WorkOrder for card-block builder tests."""
    order = MagicMock()
    order.action_class = action_class
    order.payload = payload or {}
    order.action_id = (action_id or "abcd1234-efef-efef-efef-abcd1234abcd")
    order.client_id = client_id
    order.recipient = None
    order.slack_channel_id = None
    order.slack_message_ts = None
    return order


_ET = ZoneInfo("America/New_York")


def _at_hour(hour: int) -> datetime:
    """Return a datetime whose ET hour equals `hour` (minute=0, UTC-based)."""
    from datetime import timedelta
    # Build a naive ET datetime and attach the zone
    et_dt = datetime(2026, 9, 5, hour, 0, 0, tzinfo=_ET)
    return et_dt.astimezone(timezone.utc)


# ── Calling-hours indicator ───────────────────────────────────────────────────


def test_calling_hours_green_during_window():
    """8 AM ET → green."""
    now = _at_hour(8)
    assert _calling_hours_indicator(now) == "🟢"


def test_calling_hours_green_at_1pm():
    now = _at_hour(13)
    assert _calling_hours_indicator(now) == "🟢"


def test_calling_hours_red_at_8pm():
    """8 PM ET → red (conservative default — D15 pending)."""
    now = _at_hour(20)
    assert _calling_hours_indicator(now) == "🔴"


def test_calling_hours_red_at_9pm():
    now = _at_hour(21)
    assert _calling_hours_indicator(now) == "🔴"


def test_calling_hours_red_before_8am():
    now = _at_hour(7)
    assert _calling_hours_indicator(now) == "🔴"


def test_calling_hours_label_green():
    assert "Valid" in _calling_hours_label("🟢")


def test_calling_hours_label_red():
    assert "Outside" in _calling_hours_label("🔴")


# ── Current local time ─────────────────────────────────────────────────────────


def test_current_local_time_label_formats_et():
    """1 PM ET → '1:00 PM ET' (no zero-pad, zone named)."""
    assert _current_local_time_label(_at_hour(13)) == "1:00 PM ET"


def test_current_local_time_label_morning():
    assert _current_local_time_label(_at_hour(8)) == "8:00 AM ET"


def test_dial_task_shows_current_local_time():
    """DoD 3.1.2: dial card renders the prospect's current local time."""
    order = _fake_order(
        action_class="DIAL_TASK",
        payload={"contact_name": "Jane Doe", "firm_name": "Acme PM", "county": "Hillsborough", "phone": "+18135550100", "run_id": "r1"},
    )
    txt = str(_dial_task_content_blocks(order))
    assert "Local time" in txt
    assert "ET" in txt


# ── Dial-task card blocks ─────────────────────────────────────────────────────


def test_dial_task_header_block():
    """Card starts with a header block containing 'Touch 2'."""
    order = _fake_order(
        action_class="DIAL_TASK",
        payload={"contact_name": "Jane Doe", "firm_name": "Acme PM", "county": "Hillsborough", "phone": "+18135550100", "run_id": "abcd1234"},
    )
    blocks = _dial_task_content_blocks(order)
    assert blocks[0]["type"] == "header"
    assert "Touch 2" in blocks[0]["text"]["text"]


def test_dial_task_fields_section_contains_phone():
    order = _fake_order(
        action_class="DIAL_TASK",
        payload={"contact_name": "Jane Doe", "firm_name": "Acme PM", "county": "Hillsborough", "phone": "+18135550100", "run_id": "run1"},
    )
    blocks = _dial_task_content_blocks(order)
    fields_text = str(blocks)
    assert "+18135550100" in fields_text
    assert "Jane Doe" in fields_text
    assert "Acme PM" in fields_text


def test_dial_task_missing_phone_shows_placeholder():
    """No phone → graceful 'no phone on record' placeholder."""
    order = _fake_order(action_class="DIAL_TASK", payload={"run_id": "r1"})
    blocks = _dial_task_content_blocks(order)
    text = str(blocks)
    assert "no phone on record" in text


def test_dial_task_has_divider():
    order = _fake_order(action_class="DIAL_TASK", payload={"run_id": "r1"})
    blocks = _dial_task_content_blocks(order)
    assert any(b.get("type") == "divider" for b in blocks)


def test_dial_task_shows_door_count_and_ovs_when_present():
    order = _fake_order(
        action_class="DIAL_TASK",
        payload={"run_id": "r1", "door_count": 120, "ovs_lines": ["*OVS* 76/100  ·  county rank #4"]},
    )
    txt = str(_dial_task_content_blocks(order))
    assert "120" in txt
    assert "76/100" in txt


def test_dial_task_omits_ovs_when_absent():
    order = _fake_order(action_class="DIAL_TASK", payload={"run_id": "r1"})
    txt = str(_dial_task_content_blocks(order))
    assert "OVS" not in txt  # no score wired → section omitted


def test_simple_action_button_mark_done():
    """_simple_action_button_blocks returns a single actions block with mark_done."""
    order = _fake_order()
    # patch payload_hash.button_value to return something
    with patch("src.services.slack.listeners.payload_hash") as ph:
        ph.button_value.return_value = '{"action_id": "x", "decision": "DONE"}'
        blocks = _simple_action_button_blocks(order, label="Mark Called ✓")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "actions"
    elements = blocks[0]["elements"]
    assert len(elements) == 1
    assert elements[0]["action_id"] == "mark_done"
    assert elements[0]["text"]["text"] == "Mark Called ✓"


# ── LinkedIn task card blocks ─────────────────────────────────────────────────


def test_linkedin_task_header_block():
    order = _fake_order(
        action_class="LINKEDIN_TASK",
        payload={"contact_name": "Bob Smith", "firm_name": "Acme PM", "county": "Pinellas", "run_id": "run2"},
    )
    blocks = _linkedin_task_content_blocks(order)
    assert blocks[0]["type"] == "header"
    assert "Touch 4" in blocks[0]["text"]["text"]


def test_linkedin_task_note_in_code_block():
    """Connection note appears in a code block (desktop hover-copy)."""
    order = _fake_order(
        action_class="LINKEDIN_TASK",
        payload={"contact_name": "Bob", "firm_name": "Acme PM", "county": "Pinellas",
                 "connection_note": "Hello from Blackink", "run_id": "r2"},
    )
    blocks = _linkedin_task_content_blocks(order)
    text_block = next((b for b in blocks if b.get("type") == "section" and "note" in str(b).lower()), None)
    assert text_block is not None
    assert "Hello from Blackink" in str(text_block)
    assert "```" in str(text_block)  # code block


def test_linkedin_action_buttons_include_url_button():
    """LinkedIn action buttons include a url button for the profile."""
    order = _fake_order(
        action_class="LINKEDIN_TASK",
        payload={"linkedin_url": "https://linkedin.com/in/bob", "connection_note": "Hi Bob"},
    )
    with patch("src.services.slack.listeners.payload_hash") as ph:
        ph.button_value.return_value = '{"action_id": "x", "decision": "DONE"}'
        action_blocks = _linkedin_action_buttons(order)
    elements = action_blocks[0]["elements"]
    url_buttons = [e for e in elements if e.get("url")]
    assert len(url_buttons) == 1
    assert url_buttons[0]["url"] == "https://linkedin.com/in/bob"
    assert url_buttons[0]["action_id"] == "open_linkedin_url"


def test_linkedin_action_buttons_fallback_search_url():
    """No stored linkedin_url → fallback to people-search URL."""
    order = _fake_order(
        action_class="LINKEDIN_TASK",
        payload={"contact_name": "Jane Doe", "firm_name": "Acme", "connection_note": "Hi"},
    )
    with patch("src.services.slack.listeners.payload_hash") as ph:
        ph.button_value.return_value = '{"action_id": "x", "decision": "DONE"}'
        action_blocks = _linkedin_action_buttons(order)
    elements = action_blocks[0]["elements"]
    url_btn = next((e for e in elements if e.get("url")), None)
    assert url_btn is not None
    assert "linkedin.com/search" in url_btn["url"]


def test_linkedin_action_buttons_mobile_modal():
    """Note button → opens_linkedin_note action for mobile copy."""
    order = _fake_order(
        action_class="LINKEDIN_TASK",
        payload={"connection_note": "Hi there", "linkedin_url": "https://linkedin.com/x"},
    )
    with patch("src.services.slack.listeners.payload_hash") as ph:
        ph.button_value.return_value = '{"action_id": "x", "decision": "DONE"}'
        action_blocks = _linkedin_action_buttons(order)
    elements = action_blocks[0]["elements"]
    modal_btn = next((e for e in elements if e.get("action_id") == "open_linkedin_note"), None)
    assert modal_btn is not None
    assert "Hi there" in modal_btn["value"]


def test_linkedin_action_buttons_mark_sent():
    """Last element is the 'Mark Sent ✓' button (mark_done action_id)."""
    order = _fake_order(action_class="LINKEDIN_TASK", payload={})
    with patch("src.services.slack.listeners.payload_hash") as ph:
        ph.button_value.return_value = '{"action_id": "x", "decision": "DONE"}'
        action_blocks = _linkedin_action_buttons(order)
    last_elem = action_blocks[0]["elements"][-1]
    assert last_elem["action_id"] == "mark_done"


# ── Sales-reply card blocks ───────────────────────────────────────────────────


def test_sales_reply_header():
    blocks = sales_reply_content_blocks(
        from_address="prospect@example.com",
        contact_name="Jane Doe",
        firm_name="Acme PM",
        run_id="run-1234",
        touch_step=1,
        attribution_status="attributed",
        subject="Re: Your Visibility Report",
        raw_body="Thanks for reaching out!",
        contact_id=42,
        client_id="client-abc",
        inbound_id="inbound-uuid-1234",
    )
    # Title line leads with who replied.
    assert blocks[0]["type"] == "section"
    assert "Jane Doe" in blocks[0]["text"]["text"]
    assert "replied" in blocks[0]["text"]["text"]


def test_sales_reply_attributed_badge():
    blocks = sales_reply_content_blocks(
        from_address="p@example.com", contact_name=None, firm_name=None,
        run_id="r1", touch_step=1, attribution_status="attributed",
        subject=None, raw_body="text", contact_id=1, client_id="c1", inbound_id="i1",
    )
    assert "Attributed" in str(blocks)


def test_sales_reply_unattributed_badge():
    blocks = sales_reply_content_blocks(
        from_address="p@example.com", contact_name=None, firm_name=None,
        run_id=None, touch_step=None, attribution_status="unattributed",
        subject=None, raw_body="text", contact_id=None, client_id="c1", inbound_id="i1",
    )
    assert "Unattributed" in str(blocks)


def _reply_action_ids(blocks):
    action_blocks = [b for b in blocks if b.get("type") == "actions"]
    if not action_blocks:
        return []
    return [e["action_id"] for e in action_blocks[0]["elements"]]


def test_sales_reply_opt_out_button_present_when_contact_id_known():
    """opt_out_contact button present when contact_id is set."""
    blocks = sales_reply_content_blocks(
        from_address="p@example.com", contact_name="Jane", firm_name="Acme",
        run_id="r1", touch_step=1, attribution_status="attributed",
        subject=None, raw_body="hi", contact_id=99, client_id="cli-1", inbound_id="i1",
    )
    action_blocks = [b for b in blocks if b.get("type") == "actions"]
    assert len(action_blocks) == 1
    btn = next(e for e in action_blocks[0]["elements"] if e["action_id"] == "opt_out_contact")
    value = json.loads(btn["value"])
    assert value["contact_id"] == 99
    assert value["client_id"] == "cli-1"


def test_sales_reply_reply_in_thread_button_always_present():
    """Reply in Thread button appears on every card, attributed or not."""
    attributed = sales_reply_content_blocks(
        from_address="p@example.com", contact_name="Jane", firm_name="Acme",
        run_id="r1", touch_step=1, attribution_status="attributed",
        subject=None, raw_body="hi", contact_id=99, client_id="cli-1", inbound_id="i1",
    )
    unattributed = sales_reply_content_blocks(
        from_address="p@example.com", contact_name=None, firm_name=None,
        run_id=None, touch_step=None, attribution_status="unattributed",
        subject=None, raw_body="hi", contact_id=None, client_id="cli-1", inbound_id="i1",
    )
    assert "reply_in_thread" in _reply_action_ids(attributed)
    assert "reply_in_thread" in _reply_action_ids(unattributed)


def test_sales_reply_no_opt_out_button_when_unattributed():
    """No opt_out button on unattributed cards (no contact to target) — but the
    Reply in Thread action block still exists."""
    blocks = sales_reply_content_blocks(
        from_address="p@example.com", contact_name=None, firm_name=None,
        run_id=None, touch_step=None, attribution_status="unattributed",
        subject=None, raw_body="hi", contact_id=None, client_id="cli-1", inbound_id="i1",
    )
    assert "opt_out_contact" not in _reply_action_ids(blocks)


def test_sales_reply_opt_out_has_confirm_dialog():
    """Opt-out button includes a confirmation dialog to prevent fat-finger."""
    blocks = sales_reply_content_blocks(
        from_address="p@example.com", contact_name="Jane", firm_name=None,
        run_id=None, touch_step=None, attribution_status="attributed",
        subject=None, raw_body="hi", contact_id=1, client_id="c1", inbound_id="i1",
    )
    action_block = [b for b in blocks if b.get("type") == "actions"][0]
    btn = next(e for e in action_block["elements"] if e["action_id"] == "opt_out_contact")
    assert "confirm" in btn


def test_sales_reply_shows_domain_and_thread():
    """Company domain and recent-thread lines render when supplied."""
    blocks = sales_reply_content_blocks(
        from_address="p@example.com", contact_name="Jane", firm_name="Acme",
        run_id="r1", touch_step=1, attribution_status="attributed",
        subject=None, raw_body="hi", contact_id=1, client_id="c1", inbound_id="i1",
        firm_domain="acme.com", thread_lines=["➡️ _Sep 01_ — Touch 1 sent", "⬅️ _Sep 02_ — yes"],
    )
    txt = str(blocks)
    assert "acme.com" in txt
    assert "Recent thread" in txt
    assert "Touch 1 sent" in txt


def test_sales_reply_shows_door_count_and_ovs():
    blocks = sales_reply_content_blocks(
        from_address="p@example.com", contact_name="Jane", firm_name="Acme",
        run_id="r1", touch_step=1, attribution_status="attributed",
        subject=None, raw_body="hi", contact_id=1, client_id="c1", inbound_id="i1",
        door_count=200, ovs_lines=["*OVS* 88/100  ·  county rank #2"],
    )
    txt = str(blocks)
    assert "200" in txt
    assert "88/100" in txt


def test_sales_reply_body_blockquoted():
    """Body is rendered with '>' blockquote prefix."""
    blocks = sales_reply_content_blocks(
        from_address="p@example.com", contact_name=None, firm_name=None,
        run_id=None, touch_step=None, attribution_status="unattributed",
        subject=None, raw_body="Hello\nWorld",
        contact_id=None, client_id="c1", inbound_id="i1",
    )
    text_sections = [b for b in blocks if b.get("type") == "section"]
    message_section = next((s for s in text_sections if "> Hello" in str(s)), None)
    assert message_section is not None
    assert "> World" in str(message_section)


# ── PR #26 finding 2: dial task must be created for normally-enrolled Touch 1 ──


def _touch1_order(payload, entity_id="42", client_id="c1"):
    from types import SimpleNamespace
    return SimpleNamespace(
        payload=payload, entity_id=entity_id, client_id=client_id, action_id="a1",
    )


def _fake_db_ctx(contact_row):
    session = MagicMock()
    result = MagicMock()
    result.mappings.return_value.first.return_value = contact_row
    session.execute.return_value = result
    cm = MagicMock()
    cm.__enter__ = lambda s: session
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_dial_task_created_when_touch1_payload_lacks_contact_id():
    """enroll_contact()'s Touch-1 payload has only {run_id, touch_step} — the
    dial poster must fall back to order.entity_id, not early-return (finding 2)."""
    contact_row = {
        "first_name": "Jane", "last_name": "Doe", "phone": "+18135550100",
        "company_name": "Acme PM", "county_slug": "hillsborough",
        "company_id": "co-1", "door_count_est": 120,
    }
    order = _touch1_order(payload={"run_id": "run-1", "touch_step": 1})
    with patch("src.services.slack.listeners.get_db_context", return_value=_fake_db_ctx(contact_row)), \
         patch("src.services.slack.listeners.fetch_latest_ovs", return_value=None), \
         patch("src.services.slack.listeners.wo.enqueue") as mock_enqueue, \
         patch("src.services.slack.listeners.post_work_order_card", new_callable=AsyncMock) as mock_post:
        mock_enqueue.return_value = object()
        import asyncio
        asyncio.run(_post_dial_task_after_touch1_approval(order))

    mock_enqueue.assert_called_once()
    kwargs = mock_enqueue.call_args.kwargs
    assert kwargs["action_class"] == "DIAL_TASK"
    assert kwargs["entity_id"] == "42"          # fell back to order.entity_id
    assert kwargs["idempotency_key"] == "seq:run-1:touch:2"
    mock_post.assert_awaited_once()


def test_dial_task_skipped_when_no_run_id():
    """Still guards the genuinely-unusable case: no run_id → no enqueue."""
    order = _touch1_order(payload={"touch_step": 1})  # no run_id
    with patch("src.services.slack.listeners.wo.enqueue") as mock_enqueue:
        import asyncio
        asyncio.run(_post_dial_task_after_touch1_approval(order))
    mock_enqueue.assert_not_called()
