"""The interaction layer — Bolt listeners for work-order action cards and
the Relay halt/resume surface.

Two kinds of action, deliberately handled by different code paths:

  WORK-ORDER actions (approve/reject/skip/mark_done/snooze/revise) — carry
  a JSON button value of {client_id, action_id, decision, payload_hash}.
  Every one of these goes through the same prelude: authorize -> load the
  order (tenant-scoped) -> verify the hash -> check it's still QUEUED.
  approve/reject/skip/mark_done/snooze are TERMINAL (the click decides the
  order and the card is closed out); revise is NOT terminal (it opens a
  modal, and the actual decision happens later in the view_submission
  handler) — this distinction matters because a terminal click's card gets
  its buttons stripped immediately, while revise's card stays live until
  the modal is submitted or cancelled.

  CONTROL actions (halt_resume, and the /blackink-halt /blackink-resume
  slash commands) have NO work order at all — a global halt has no
  client_id, so the work-order prelude above would fail on every one of
  its steps. These go straight to src.agents.relay.halt_service.

Every @app.* decorator here is a THIN wrapper: it pulls what it needs out
of Bolt's injected args and calls a plain async function that takes no
Bolt-specific types. That split is deliberate — the plain functions are
unit-testable with AsyncMock, without spinning up a real Bolt dispatcher.

Socket Mode note: there is no HTTP request here for
src.services.slack.auth.verify_slack_signature() to check — Bolt/Slack's
own Socket Mode transport is already the trust boundary (see
bolt_app.py's module docstring). Only approver_authorized() (WHO clicked,
not whether the request is genuine) applies in this file.
"""

from __future__ import annotations

import hmac as _hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from datetime import datetime as _datetime

from src.agents.relay import halt_service
from src.agents.relay.resume_auth import generate_resume_token
from src.core.database import get_db_context, get_system_db_context
from src.services import work_orders as wo
from src.services.booking_link import resolve_booking_link
from src.services.email_sender import build_email_sender
from src.services.email_unsubscribe import append_unsubscribe_footer, unsubscribe_url
from src.services.mailbox_dispatcher import (
	AllMailboxesCapped,
	NoMailboxAvailable,
	get_active_mailbox_for_client,
)
from src.services.ovs_lookup import fetch_latest_ovs, ovs_card_lines
from src.services.no_show_prompts import verify_no_show_token
from src.services.no_show_recovery import already_recorded, trigger_recovery
from src.services.events import log_event as _shared_log_event, MalformedEventError
from src.services.meeting_outcomes import record_outcome
from src.services.slack import payload_hash, post
from src.services.slack.auth import approver_authorized
from src.services.slack.bolt_app import get_listener_app
from sqlalchemy import text

logger = logging.getLogger(__name__)

# get_listener_app(), NOT get_bolt_app(): this binding happens at IMPORT
# time and src/api/main.py imports this module to register the handlers, so
# raising here on a missing SLACK_BOT_TOKEN would take the whole API down
# with it. The stub degrades to "no listeners registered" instead — see
# bolt_app._UnconfiguredApp.
app = get_listener_app()

# ── Vocabulary ───────────────────────────────────────────────────────────

# action_id -> (terminal decision string, is terminal). "revise" opens a
# modal instead of deciding immediately, so it carries no decision here.
_TERMINAL_ACTION_DECISIONS = {
	"approve": "APPROVED",
	"reject": "REJECTED",
	"skip": "SKIPPED",
	"mark_done": "DONE",
}

# Snooze durations — Dev 3 plan §9 item 2 decided default. Timezone
# fallback is a ponytail-marked simplification: all Week 0/1 target
# metros are Florida, so a fixed zone is correct for now.
_SNOOZE_TIMEZONE = "America/New_York"  # ponytail: hardcoded tenant timezone, add a clients.timezone column if metros expand outside FL
_SNOOZE_OPTIONS = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}


def _snooze_until(duration_key: str, now: Optional[datetime] = None) -> datetime:
	now = now or datetime.now(timezone.utc)
	if duration_key == "tomorrow_9am":
		local_now = now.astimezone(ZoneInfo(_SNOOZE_TIMEZONE))
		local_tomorrow_9am = (local_now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
		return local_tomorrow_9am.astimezone(timezone.utc)
	delta = _SNOOZE_OPTIONS.get(duration_key)
	if delta is None:
		raise ValueError(f"Unknown snooze duration key: {duration_key!r}")
	return now + delta


# ── Calling-hours indicator (Touch 2 / DIAL_TASK — ticket 25) ────────────

# Hardcoded ET for September — all 10 launch counties are Eastern (D14).
# Conservative default: 8 PM = red until D15 (calling-hours window) confirmed.
# Effective window: 8 AM ≤ hour < 20 (8 AM–8 PM ET exclusive).
_CALLING_TZ = ZoneInfo("America/New_York")
_CALLING_WINDOW_START = 8   # 8 AM ET inclusive
_CALLING_WINDOW_END = 20    # 8 PM ET exclusive (8PM = red — conservative, D15 pending)


def _calling_hours_indicator(now: Optional[datetime] = None) -> str:
	"""🟢 if current ET time is within the valid calling window, 🔴 otherwise.

	Computed at post time and stamped into the card payload — not recalculated
	on subsequent views. A stale indicator is acceptable for an informational
	card; the setter should exercise judgment for edge cases (ticket 25 §C8)."""
	now_et = (now or datetime.now(timezone.utc)).astimezone(_CALLING_TZ)
	if _CALLING_WINDOW_START <= now_et.hour < _CALLING_WINDOW_END:
		return "🟢"
	return "🔴"


def _calling_hours_label(indicator: str) -> str:
	if indicator == "🟢":
		return "Valid calling hours (8 AM–8 PM ET)"
	return "Outside calling hours — do not call"


def _current_local_time_label(now: Optional[datetime] = None) -> str:
	"""Recipient's current local wall-clock time, stamped at post time.
	All 10 launch counties are Eastern (D14), so local == ET; the label names
	the zone explicitly so it stays honest if a non-ET county is ever added."""
	now_et = (now or datetime.now(timezone.utc)).astimezone(_CALLING_TZ)
	# %I is zero-padded and cross-platform (%-I is not on Windows); strip the pad.
	return now_et.strftime("%I:%M %p ET").lstrip("0")


def _log_event(client_id: str, event_type: str, *, entity_id: str, actor: str, payload: dict) -> None:
	"""Thin adapter over src.services.events.log_event — kept as a private
	wrapper (not a call-site-by-call-site rewrite) so this diff stays
	minimal; entity_type is fixed at 'work_order' here because every
	existing caller in this file logs against a work order."""
	try:
		_shared_log_event(client_id, event_type, entity_type="work_order", entity_id=entity_id, actor=actor, payload=payload)
	except MalformedEventError:
		logger.error("[listeners] malformed event payload, not written (event_type=%s)", event_type, exc_info=True)
	except Exception:
		logger.error("[listeners] failed to write events row (event_type=%s)", event_type, exc_info=True)


def _card_button_blocks(order: "wo.WorkOrder") -> list:
	"""The standard five-button action row plus three snooze-duration
	buttons, each value carrying a freshly recomputed hash (never
	order.payload_hash — see payload_hash.button_value's docstring for
	why).

	Snooze is three BUTTONS, not a static_select, despite the Dev 3 plan
	§9 item 2 default originally saying "static select" — confirmed live
	against the real Slack API that a select menu's OPTION value has a
	hard 150-character limit (a button's value field does not share this
	limit; every other button here already relies on the larger budget).
	A UUID (36 chars) plus a SHA-256 hex digest (64 chars) alone exceed
	150 once wrapped in JSON, so a select option can never carry this
	card's payload_hash. Three buttons deliver the same three fixed
	choices (1h / 4h / tomorrow 9am) within the budget every other button
	on this card already uses."""
	def val(decision: str, **extra) -> str:
		payload = json.loads(payload_hash.button_value(order, decision))
		payload.update(extra)
		return json.dumps(payload)

	return [
		{
			"type": "actions",
			"elements": [
				{"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "style": "primary", "action_id": "approve", "value": val("APPROVED")},
				{"type": "button", "text": {"type": "plain_text", "text": "Revise"}, "action_id": "revise", "value": val("REVISE")},
				{"type": "button", "text": {"type": "plain_text", "text": "Reject"}, "style": "danger", "action_id": "reject", "value": val("REJECTED")},
				{"type": "button", "text": {"type": "plain_text", "text": "Skip"}, "action_id": "skip", "value": val("SKIPPED")},
				{"type": "button", "text": {"type": "plain_text", "text": "Mark Done"}, "action_id": "mark_done", "value": val("DONE")},
			],
		},
		{
			"type": "actions",
			"elements": [
				{"type": "button", "text": {"type": "plain_text", "text": "Snooze 1h"}, "action_id": "snooze_1h", "value": val("SNOOZED", duration="1h")},
				{"type": "button", "text": {"type": "plain_text", "text": "Snooze 4h"}, "action_id": "snooze_4h", "value": val("SNOOZED", duration="4h")},
				{"type": "button", "text": {"type": "plain_text", "text": "Snooze until tomorrow 9am"}, "action_id": "snooze_tomorrow_9am", "value": val("SNOOZED", duration="tomorrow_9am")},
			],
		},
	]


def _card_text(order: "wo.WorkOrder") -> str:
	payload = order.payload if isinstance(order.payload, dict) else {}

	if order.action_class == "DISPATCH_EMAIL_TOUCH":
		touch_step = payload.get("touch_step", "?")
		run_id_short = str(payload.get("run_id", ""))[:8]
		subject = payload.get("subject") or f"Touch {touch_step} — cold outreach sequence"
		body_preview = payload.get("body_preview") or "(email copy generated at send time — placeholder pending Dev 2 assets)"
		return (
			f"*Email Touch {touch_step}* (`{order.action_id[:8]}`) — run `{run_id_short}`\n"
			f"To: `{order.recipient or 'n/a'}`\n"
			f"Subject: _{subject}_\n"
			f"{body_preview}"
		)

	if order.action_class == "DISPATCH_WINBACK_TOUCH":
		touch_step = payload.get("touch_step", "?")
		winback_row_id = payload.get("winback_row_id", "?")
		subject = payload.get("subject") or f"Touch {touch_step} — win-back sequence"
		body_preview = payload.get("body") or "(approved copy missing)"
		return (
			f"*Win-Back Touch {touch_step}* (`{order.action_id[:8]}`) — winback_row `{winback_row_id}`\n"
			f"To: `{order.recipient or 'n/a'}`\n"
			f"Subject: _{subject}_\n"
			f"{body_preview}"
		)

	subject = payload.get("subject")
	preview = subject or str(order.payload)[:120]
	return (
		f"*{order.action_class}* (`{order.action_id[:8]}`)\n"
		f"To: `{order.recipient or 'n/a'}`\n"
		f"{preview}"
	)


async def post_work_order_card(order: "wo.WorkOrder", *, channel_key: str) -> Optional["wo.WorkOrder"]:
	"""Posts a fresh card for a QUEUED order and persists its Slack
	location. Called after enqueue() and after every rotation
	(update_payload). Returns the order unchanged; None only if the post
	itself failed (Slack unconfigured, channel unresolved, or API error —
	post.post_action_card already logs the specific reason)."""
	posted = await post.post_action_card(channel_key=channel_key, text=_card_text(order), blocks=_card_text_blocks(order))
	if posted is None:
		return None
	wo.set_slack_message(order.client_id, order.action_id, channel_id=posted["channel_id"], message_ts=posted["message_ts"])
	return order


def _email_touch_content_blocks(order: "wo.WorkOrder") -> list:
	"""Rich Block Kit layout for a DISPATCH_EMAIL_TOUCH approval card: a header,
	a To/Touch fields row, the subject, a blockquoted body preview, and a
	context line for the attachment + run/action ids. Subject/body/attachment
	fill from the work-order payload once real copy is composed there (ticket 25);
	until then they show a clear 'pending' placeholder rather than a raw dict."""
	payload = order.payload if isinstance(order.payload, dict) else {}
	touch_step = payload.get("touch_step", "?")
	run_id_short = str(payload.get("run_id", ""))[:8]
	subject = payload.get("subject") or f"Touch {touch_step} — cold outreach sequence"
	body_preview = payload.get("body_preview") or "_Email copy is generated at send time — placeholder pending client copy (C2) and the Owner Visibility Score PDF (Dev 2, C3)._"
	attachment = payload.get("attachment_name") or "Owner Visibility Score PDF (pending Dev 2)"
	# Body preview as a blockquote, capped so the card stays compact.
	quoted = "\n".join(f"> {ln}" for ln in body_preview.splitlines()[:6]) or f"> {body_preview}"

	return [
		{"type": "header", "text": {"type": "plain_text", "text": f"\U0001F4E7 Email Touch {touch_step} · Approval needed", "emoji": True}},
		{
			"type": "section",
			"fields": [
				{"type": "mrkdwn", "text": f"*To*\n{order.recipient or 'n/a'}"},
				{"type": "mrkdwn", "text": f"*Touch*\nStep {touch_step} of 5"},
			],
		},
		{"type": "section", "text": {"type": "mrkdwn", "text": f"*Subject*\n{subject}"}},
		{"type": "section", "text": {"type": "mrkdwn", "text": f"*Preview*\n{quoted}"}},
		{
			"type": "context",
			"elements": [
				{"type": "mrkdwn", "text": f"\U0001F4CE {attachment}  ·  run `{run_id_short}`  ·  `{order.action_id[:8]}`"},
			],
		},
		{"type": "divider"},
	]


def _dial_task_content_blocks(order: "wo.WorkOrder") -> list:
	"""Informational card for Touch 2 (phone call). No approve gate — nothing
	sends. Posted immediately when Touch 1 is approved (60s SLA — event-driven,
	not the day-grain sweep). Calling-hours indicator computed at post time.
	Score/rank shown as 'pending' if not in payload (Dev-2 assets not yet
	wired — ticket 25)."""
	payload = order.payload if isinstance(order.payload, dict) else {}
	contact_name = payload.get("contact_name") or "Unknown"
	firm_name = payload.get("firm_name") or "Unknown"
	county = payload.get("county") or "Unknown"
	phone = payload.get("phone") or "_(no phone on record)_"
	run_id_short = str(payload.get("run_id", ""))[:8]
	# One clock read shared by the indicator and the local-time field so they
	# never disagree by a tick.
	now_utc = datetime.now(timezone.utc)
	indicator = _calling_hours_indicator(now_utc)
	hours_label = _calling_hours_label(indicator)
	local_time = _current_local_time_label(now_utc)

	fields = [
		{"type": "mrkdwn", "text": f"*Contact*\n{contact_name}"},
		{"type": "mrkdwn", "text": f"*Firm*\n{firm_name}"},
		{"type": "mrkdwn", "text": f"*County*\n{county}"},
		{"type": "mrkdwn", "text": f"*Phone*\n{phone}"},
		{"type": "mrkdwn", "text": f"*Local time*\n{local_time}"},
	]
	door_count = payload.get("door_count")
	if door_count is not None:
		fields.append({"type": "mrkdwn", "text": f"*Doors*\n{door_count}"})

	blocks: list = [
		{"type": "header", "text": {"type": "plain_text", "text": "📞 Touch 2 · Call now", "emoji": True}},
		{"type": "section", "fields": fields},
	]
	# Owner Visibility Score — shown when Dev-2's score exists; else omitted.
	ovs_lines = payload.get("ovs_lines")
	if ovs_lines:
		blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(ovs_lines)}})
	blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{indicator} *{hours_label}*"}})
	blocks.append({
		"type": "context",
		"elements": [{"type": "mrkdwn", "text": f"run `{run_id_short}`  ·  `{order.action_id[:8]}`"}],
	})
	blocks.append({"type": "divider"})
	return blocks


def _linkedin_task_content_blocks(order: "wo.WorkOrder") -> list:
	"""Informational card for Touch 4 (LinkedIn connection note). No approve
	gate — nothing sends. Day-grain sweep posts this when due_at arrives.
	Note in a code block for desktop hover-copy; modal fallback for mobile
	(ticket 26, research ticket 29 — Block Kit has no clipboard button)."""
	payload = order.payload if isinstance(order.payload, dict) else {}
	contact_name = payload.get("contact_name") or "Unknown"
	firm_name = payload.get("firm_name") or "Unknown"
	county = payload.get("county") or "Unknown"
	run_id_short = str(payload.get("run_id", ""))[:8]
	# Note: ≤300 chars (LinkedIn connection note limit). Score-free placeholder
	# for September; swap in client copy C7 with no code change (just update payload).
	note = payload.get("connection_note") or (
		f"Hi {contact_name.split()[0] if contact_name != 'Unknown' else 'there'}, "
		f"noticed your firm manages properties in {county}. "
		"Would love to connect and share some county-level market insights."
	)[:300]

	return [
		{"type": "header", "text": {"type": "plain_text", "text": "🔗 Touch 4 · LinkedIn connection", "emoji": True}},
		{
			"type": "section",
			"fields": [
				{"type": "mrkdwn", "text": f"*Contact*\n{contact_name}"},
				{"type": "mrkdwn", "text": f"*Firm*\n{firm_name}"},
				{"type": "mrkdwn", "text": f"*County*\n{county}"},
			],
		},
		{
			"type": "section",
			"text": {"type": "mrkdwn", "text": f"*Connection note* — copy below, paste into LinkedIn:\n```{note}```"},
		},
		{
			"type": "context",
			"elements": [
				{"type": "mrkdwn", "text": f"run `{run_id_short}`  ·  `{order.action_id[:8]}`"},
			],
		},
		{"type": "divider"},
	]


def _winback_touch_content_blocks(order: "wo.WorkOrder") -> list:
	"""Rich Block Kit layout for a DISPATCH_WINBACK_TOUCH approval card
	(Subtask 3.1.2) — same shape as _email_touch_content_blocks above, but
	for the 3-touch win-back sequence: 'Step N of 3' (not 5), no run_id/
	attachment concepts (win-back has neither — no threading id to show
	until a reply exists, no PDF attachment), winback_row_id shown instead
	in the context line. Reads payload['body'] directly (the actual key
	winback_sequencer.arm_winback_run persists — not 'body_preview', which
	nothing in this sequence's payload ever sets)."""
	payload = order.payload if isinstance(order.payload, dict) else {}
	touch_step = payload.get("touch_step", "?")
	winback_row_id = payload.get("winback_row_id", "?")
	subject = payload.get("subject") or f"Touch {touch_step} — win-back sequence"
	body = payload.get("body") or "_Approved copy missing — this card should not have been posted._"
	quoted = "\n".join(f"> {ln}" for ln in body.splitlines()[:6]) or f"> {body}"

	return [
		{"type": "header", "text": {"type": "plain_text", "text": f"\U0001F504 Win-Back Touch {touch_step} · Approval needed", "emoji": True}},
		{
			"type": "section",
			"fields": [
				{"type": "mrkdwn", "text": f"*To*\n{order.recipient or 'n/a'}"},
				{"type": "mrkdwn", "text": f"*Touch*\nStep {touch_step} of 3"},
			],
		},
		{"type": "section", "text": {"type": "mrkdwn", "text": f"*Subject*\n{subject}"}},
		{"type": "section", "text": {"type": "mrkdwn", "text": f"*Preview*\n{quoted}"}},
		{
			"type": "context",
			"elements": [
				{"type": "mrkdwn", "text": f"\U0001F3E0 winback_row `{winback_row_id}`  ·  `{order.action_id[:8]}`"},
			],
		},
		{"type": "divider"},
	]


def _stl_cadence_touch_content_blocks(order: "wo.WorkOrder") -> list:
	"""Approval card for DISPATCH_STL_CADENCE_TOUCH (Task 4.2.2).

	Shape mirrors _winback_touch_content_blocks: header, prospect/step fields,
	subject preview, body preview, context line. 'Step N of 5' for the 5-day
	follow-up cadence. message_id shown instead of run_id/winback_row_id."""
	payload = order.payload if isinstance(order.payload, dict) else {}
	touch_step = payload.get("touch_step", "?")
	message_id = payload.get("message_id", "?")
	prospect_name = payload.get("prospect_name") or "Unknown"
	prospect_email = payload.get("prospect_email") or order.recipient or "n/a"
	subject = payload.get("subject") or f"Follow-up {touch_step} — Speed-to-Lead"
	body = payload.get("body") or "_Approved copy missing — this card should not have been posted._"
	# Strip HTML tags for the preview (simple regex — same as speed_to_lead_sweep)
	import re
	preview_text = re.sub(r"<[^>]+>", "", body)[:300]
	quoted = "\n".join(f"> {ln}" for ln in preview_text.splitlines()[:5]) or f"> {preview_text}"

	return [
		{"type": "header", "text": {"type": "plain_text", "text": f"⚡ STL Follow-Up Touch {touch_step} · Approval needed", "emoji": True}},
		{
			"type": "section",
			"fields": [
				{"type": "mrkdwn", "text": f"*Prospect*\n{prospect_name} — {prospect_email}"},
				{"type": "mrkdwn", "text": f"*Touch*\nStep {touch_step} of 5"},
			],
		},
		{"type": "section", "text": {"type": "mrkdwn", "text": f"*Subject*\n{subject}"}},
		{"type": "section", "text": {"type": "mrkdwn", "text": f"*Preview*\n{quoted}"}},
		{
			"type": "context",
			"elements": [
				{"type": "mrkdwn", "text": f"\U0001F4E8 message `{message_id}`  ·  `{order.action_id[:8]}`"},
			],
		},
		{"type": "divider"},
	]


def _simple_action_button_blocks(order: "wo.WorkOrder", *, label: str) -> list:
	"""A single 'Mark Done' button — for informational task cards (DIAL_TASK,
	LINKEDIN_TASK) that need only one outcome and no approve/reject/snooze."""
	return [
		{
			"type": "actions",
			"elements": [
				{
					"type": "button",
					"text": {"type": "plain_text", "text": label},
					"style": "primary",
					"action_id": "mark_done",
					"value": payload_hash.button_value(order, "DONE"),
				},
			],
		}
	]


def _linkedin_action_buttons(order: "wo.WorkOrder") -> list:
	"""Action buttons for LINKEDIN_TASK card:
	  - Open LinkedIn Profile (url button — opens profile directly)
	  - Copy Note (Mobile) (opens a prefilled modal — desktop hover-copy on
	    the code block is unavailable on mobile, ticket 26 / research 29)
	  - Mark Sent ✓ (mark_done — closes the work order as DONE)
	"""
	payload = order.payload if isinstance(order.payload, dict) else {}
	contact_name = payload.get("contact_name") or ""
	firm_name = payload.get("firm_name") or ""
	note = payload.get("connection_note") or ""
	# Stored linkedin_url or fall back to people-search URL (ticket 26).
	linkedin_url = payload.get("linkedin_url") or (
		f"https://www.linkedin.com/search/results/people/?keywords={quote(f'{contact_name} {firm_name}')}"
	)

	elements = [
		{
			"type": "button",
			"text": {"type": "plain_text", "text": "Open LinkedIn Profile"},
			"url": linkedin_url,
			"action_id": "open_linkedin_url",
		},
	]
	if note:
		elements.append({
			"type": "button",
			"text": {"type": "plain_text", "text": "📋 Copy note"},
			"action_id": "open_linkedin_note",
			"value": json.dumps({"note": note[:300]}),
		})
	elements.append({
		"type": "button",
		"text": {"type": "plain_text", "text": "Mark Sent ✓"},
		"style": "primary",
		"action_id": "mark_done",
		"value": payload_hash.button_value(order, "DONE"),
	})
	return [{"type": "actions", "elements": elements}]


def sales_reply_content_blocks(
	*,
	from_address: str,
	contact_name: Optional[str],
	firm_name: Optional[str],
	run_id: Optional[str],
	touch_step: Optional[int],
	attribution_status: str,
	subject: Optional[str],
	raw_body: Optional[str],
	contact_id: Optional[int],
	client_id: str,
	inbound_id: str,
	firm_domain: Optional[str] = None,
	thread_lines: Optional[list] = None,
	door_count: Optional[int] = None,
	ovs_lines: Optional[list] = None,
) -> list:
	"""Block Kit layout for a #sales-replies inbound reply card (ticket 28/30).

	Shows the inbound reply body, attribution status, and (if attributed) the
	run context. Has a 'Mark Opt-Out' button (ticket 27) iff contact_id is
	known — the button never expires (ticket 15) since it carries no payload
	hash, only the contact_id and client_id directly.

	Unattributed cards: no opt-out button (no contact to target). Human reads
	+ routes manually. BCC echoes (our own outbound message_id arriving back)
	are filtered BEFORE this function is called and never posted."""
	attributed = attribution_status == "attributed"
	badge = "🟢 Attributed" if attributed else "🟠 Unattributed"
	if run_id and touch_step:
		run_ctx = f"Touch {touch_step} · run `{str(run_id)[:8]}`"
	elif run_id:
		run_ctx = f"run `{str(run_id)[:8]}`"
	else:
		run_ctx = "no matched sequence"

	contact_label = contact_name or from_address
	firm_bits = [b for b in (firm_name, firm_domain) if b]

	# Body preview — first 20 lines, blockquoted.
	body_lines = (raw_body or "(no body)").splitlines()[:20]
	quoted = "\n".join(f"> {ln}" for ln in body_lines) or "> (no body)"

	# Title line: who replied, at a glance.
	title = contact_name or from_address
	subtitle_bits = [b for b in firm_bits] or [from_address if contact_name else ""]
	subtitle = "  ·  ".join([b for b in subtitle_bits if b])

	blocks: list = [
		{"type": "section", "text": {"type": "mrkdwn", "text": f"*💬 {title}* replied"
			+ (f"\n{subtitle}" if subtitle else "")}},
		{"type": "context", "elements": [{"type": "mrkdwn", "text": f"{badge}  ·  {run_ctx}  ·  from `{from_address}`"}]},
	]

	# Compact facts row — only the fields we actually have.
	facts = []
	if door_count is not None:
		facts.append(f"*Doors:* {door_count}")
	if facts:
		blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(facts)}]})

	# Owner Visibility Score — only when Dev-2's score exists for this firm.
	if ovs_lines:
		blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(ovs_lines)}})

	if subject:
		blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"*Subject:* {subject}"}]})
	blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": quoted}})

	# Thread history — last 3 prior messages (v2 §3.1.3). Newest first.
	if thread_lines:
		blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "*Recent thread*"}]})
		blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(thread_lines)}})

	blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"inbound `{inbound_id[:8]}`"}]})

	# Actions: Reply in Thread (always), Book Meeting (always — S-11, the
	# booking engine now exists; resolve_booking_link() itself returns None
	# and the handler fails visibly if no default sales-booking connection
	# is flagged, rather than the button silently doing nothing), Mark
	# Opt-Out (only when a contact_id is known — never expires, no payload
	# hash, ticket 15/27).
	elements: list = [
		{
			"type": "button",
			"text": {"type": "plain_text", "text": "Reply in Thread"},
			"action_id": "reply_in_thread",
			"value": json.dumps({"inbound_id": inbound_id, "contact_id": contact_id, "client_id": client_id}),
		},
		{
			"type": "button",
			"text": {"type": "plain_text", "text": "Book Meeting"},
			"action_id": "book_meeting",
			"value": json.dumps({"inbound_id": inbound_id, "contact_id": contact_id, "client_id": client_id}),
		},
	]
	if contact_id is not None:
		elements.append({
			"type": "button",
			"text": {"type": "plain_text", "text": "Mark Opt-Out"},
			"style": "danger",
			"action_id": "opt_out_contact",
			"value": json.dumps({"contact_id": contact_id, "client_id": client_id}),
			"confirm": {
				"title": {"type": "plain_text", "text": "Opt out this contact?"},
				"text": {
					"type": "mrkdwn",
					"text": (
						f"This will permanently halt *{contact_label}'s* active sequence "
						"and block future sends *globally across all clients*. Cannot be undone."
					),
				},
				"confirm": {"type": "plain_text", "text": "Yes, opt out"},
				"deny": {"type": "plain_text", "text": "Cancel"},
			},
		})
	blocks.append({"type": "actions", "elements": elements})

	return blocks


def _card_text_blocks(order: "wo.WorkOrder") -> list:
	if order.action_class == "DISPATCH_EMAIL_TOUCH":
		return _email_touch_content_blocks(order) + _card_button_blocks(order)
	if order.action_class == "DISPATCH_WINBACK_TOUCH":
		return _winback_touch_content_blocks(order) + _card_button_blocks(order)
	if order.action_class == "DISPATCH_STL_CADENCE_TOUCH":    # Task 4.2.2
		return _stl_cadence_touch_content_blocks(order) + _card_button_blocks(order)
	if order.action_class == "DIAL_TASK":
		return _dial_task_content_blocks(order) + _simple_action_button_blocks(order, label="Mark Called ✓")
	if order.action_class == "LINKEDIN_TASK":
		return _linkedin_task_content_blocks(order) + _linkedin_action_buttons(order)
	return [{"type": "section", "text": {"type": "mrkdwn", "text": _card_text(order)}}] + _card_button_blocks(order)


# ── Work-order click prelude — shared by every terminal action ──────────


class ClickRejected(Exception):
	"""Raised by _load_and_verify to short-circuit a listener with an
	already-sent ephemeral response — callers catch this once at the
	decorator boundary rather than checking a sentinel return value at
	every step."""


async def _load_and_verify(value: dict, user_id: str, *, respond) -> "wo.WorkOrder":
	"""Steps 4-9 of the Dev 3 plan's §4.3 click flow (steps 1-3, signature
	verification and type branching, are Bolt's own job under Socket Mode
	— see this module's docstring). Raises ClickRejected after already
	sending the appropriate ephemeral response; callers should let it
	propagate and stop."""
	client_id = value.get("client_id")
	action_id = value.get("action_id")
	provided_hash = value.get("payload_hash")

	if not approver_authorized(user_id, client_id=client_id):
		await respond(response_type="ephemeral", text=":no_entry: Not authorized to act on this card.")
		raise ClickRejected("not authorized")

	order = wo.get(client_id, action_id) if client_id and action_id else None
	if order is None:
		await respond(response_type="ephemeral", text=":warning: This work order could not be found (it may belong to a different tenant, or the id is stale).")
		raise ClickRejected("not found")

	verdict = payload_hash.verify(order, provided_hash)
	if not verdict.integrity_ok:
		logger.error(
			"[listeners] payload_hash INTEGRITY MISMATCH action_id=%s stored=%s recomputed=%s — a writer bypassed update_payload()",
			action_id, verdict.stored, verdict.recomputed,
		)
		await post.post_notice(
			channel_key="qa",
			text=f":rotating_light: work_order_hash_integrity_failure action_id=`{action_id}` client_id=`{client_id}`",
		)
		_log_event(client_id, "work_order_hash_integrity_failure", entity_id=action_id, actor=f"slack:{user_id}", payload={"stored": verdict.stored, "recomputed": verdict.recomputed})

	if not verdict.fresh:
		_log_event(client_id, "work_order_stale_click_rejected", entity_id=action_id, actor=f"slack:{user_id}", payload={"provided_hash": verdict.provided, "recomputed_hash": verdict.recomputed})
		await respond(
			response_type="ephemeral",
			text=(
				":warning: This card is out of date — the message or its configuration changed "
				"after this card was posted, so the click was not executed.\n"
				"Nothing was sent. A refreshed card has been posted below; review and act on that one."
			),
		)
		refreshed = wo.get(client_id, action_id)
		if refreshed is not None:
			await post_work_order_card(refreshed, channel_key="setter")
		raise ClickRejected("stale hash")

	if order.status != "QUEUED":
		await respond(response_type="ephemeral", text=f":information_source: Already decided (status={order.status}).")
		raise ClickRejected("already decided")

	return order


async def _post_dial_task_after_touch1_approval(order: "wo.WorkOrder") -> None:
	"""Post a DIAL_TASK card immediately when Touch 1 is approved (ticket 25).

	60-second SLA means we cannot wait for the day-grain sweep — this is
	fired event-driven from _finalize_terminal_decision on Touch 1 approval.
	The DIAL_TASK work order's due_at is a 'call after' hint; posting is ours.

	Contact info is looked up synchronously from DB (same pattern as
	_log_event — get_db_context is sync, safe to call from async context)."""
	payload = order.payload if isinstance(order.payload, dict) else {}
	# enroll_contact()'s email-touch payload carries only {run_id, touch_step} —
	# no contact_id — so fall back to the work order's own entity_id (the
	# contact id every touch order is keyed to). Without this fallback EVERY
	# normally-enrolled Touch 1 hit the early return and no dial card was ever
	# posted (PR #26 finding 2).
	contact_id = payload.get("contact_id") or order.entity_id
	run_id = payload.get("run_id")
	if not contact_id or not run_id:
		logger.warning(
			"[listeners] DIAL_TASK skip — no contact_id/run_id in Touch 1 payload action_id=%s",
			order.action_id,
		)
		return

	# Look up contact + company info to populate the card.
	contact_data: dict = {}
	try:
		with get_db_context(client_id=order.client_id) as session:
			row = session.execute(
				text(
					"SELECT c.first_name, c.last_name, c.phone, "
					"       co.company_name, co.county_slug, co.company_id, co.door_count_est "
					"FROM contacts c "
					"JOIN companies co ON co.company_id = c.company_id "
					"WHERE c.contact_id = :contact_id"
				),
				{"contact_id": contact_id},
			).mappings().first()
			if row:
				contact_data = dict(row)
				ovs = fetch_latest_ovs(session, row["company_id"])
				if ovs:
					contact_data["ovs_lines"] = ovs_card_lines(ovs)
	except Exception:
		logger.warning(
			"[listeners] DIAL_TASK — contact lookup failed, posting with minimal info",
			exc_info=True,
		)

	first = contact_data.get("first_name") or ""
	last = contact_data.get("last_name") or ""
	contact_name = f"{first} {last}".strip() or "Unknown"
	firm_name = contact_data.get("company_name") or "Unknown"
	county = contact_data.get("county_slug") or "Unknown"
	phone = contact_data.get("phone")

	dial_payload = {
		"contact_id": contact_id,
		"run_id": run_id,
		"contact_name": contact_name,
		"firm_name": firm_name,
		"county": county,
		"phone": phone,
		"door_count": contact_data.get("door_count_est"),
		"ovs_lines": contact_data.get("ovs_lines"),
	}

	# Stable run/touch key — matches the other touches' convention and
	# guarantees exactly one dial order per run regardless of approval retries
	# (a time-bucketed key could double-post across the bucket boundary).
	idempotency_key = f"seq:{run_id}:touch:2"
	try:
		dial_order = wo.enqueue(
			client_id=order.client_id,
			entity_type="contact",
			entity_id=str(contact_id),
			agent_id="cold_outbound_sequencer",
			action_class="DIAL_TASK",
			autonomy_band="BAND_1_HUMAN_ALWAYS",
			risk_class="LOW",
			payload=dial_payload,
			config_fingerprint={"channel": "dial", "touch_step": 2},
			idempotency_key=idempotency_key,
			recipient=phone,
		)
	except Exception:
		logger.error("[listeners] DIAL_TASK enqueue failed", exc_info=True)
		return

	await post_work_order_card(dial_order, channel_key="dial")


async def _finalize_terminal_decision(order: "wo.WorkOrder", *, decision: str, user_id: str, respond) -> None:
	decided = wo.record_decision(order.client_id, order.action_id, decision=decision, decided_by=f"slack:{user_id}")
	if decided is None:
		# Lost a race to a concurrent decision between _load_and_verify's
		# QUEUED check and this UPDATE — the double-tap guard in
		# work_orders.record_decision caught it. Not an error; just no-op.
		await respond(response_type="ephemeral", text=":information_source: This was just decided by someone else.")
		return
	# Decrement Cora's approval backlog for human review decisions so the
	# throttle can resume when the queue clears. SKIPPED/SNOOZED are not
	# "reviewed" — only a genuine approve or reject counts as a resolved draft.
	if decision in {"APPROVED", "REJECTED"}:
		from src.agents.cora.throttle import notify_approval_resolved
		notify_approval_resolved()
	_log_event(order.client_id, "work_order_decided", entity_id=order.action_id, actor=f"slack:{user_id}", payload={"decision": decision})
	if decided.slack_channel_id and decided.slack_message_ts:
		await post.update_card(
			channel_id=decided.slack_channel_id,
			message_ts=decided.slack_message_ts,
			text=f"{_card_text(decided)}\n\n*Decision:* {decision} by <@{user_id}>",
		)
	# Event-driven DIAL_TASK: post immediately when Touch 1 approved (60s SLA).
	if decision == "APPROVED" and order.action_class == "DISPATCH_EMAIL_TOUCH":
		order_payload = order.payload if isinstance(order.payload, dict) else {}
		if order_payload.get("touch_step") == 1:
			await _post_dial_task_after_touch1_approval(order)


# ── Work-order action listeners ──────────────────────────────────────────


@app.action("approve")
@app.action("reject")
@app.action("skip")
@app.action("mark_done")
async def handle_terminal_action(ack, body, respond, action):
	await ack()
	action_id_clicked = action["action_id"]
	decision = _TERMINAL_ACTION_DECISIONS[action_id_clicked]
	user_id = body.get("user", {}).get("id", "")
	try:
		value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		await respond(response_type="ephemeral", text=":warning: Malformed button payload.")
		return
	try:
		order = await _load_and_verify(value, user_id, respond=respond)
	except ClickRejected:
		return
	await _finalize_terminal_decision(order, decision=decision, user_id=user_id, respond=respond)


# ── LinkedIn actions — url-button ack + note modal ───────────────────────


@app.action("open_linkedin_url")
async def handle_open_linkedin_url(ack, **_):
	"""Block Kit url buttons still fire an action payload — ack immediately,
	the URL opens in the browser independently. No work-order state change."""
	await ack()


_LINKEDIN_NOTE_MODAL_ID = "linkedin_note_copy_modal"


@app.action("open_linkedin_note")
async def handle_open_linkedin_note(ack, body, respond, action, client):
	"""Opens a prefilled modal so the setter can select-and-copy the LinkedIn
	connection note on mobile (desktop hover-copy on the code block is
	unavailable on mobile — ticket 26 / research ticket 29)."""
	await ack()
	try:
		value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		await respond(response_type="ephemeral", text=":warning: Malformed button payload.")
		return
	note = value.get("note", "")
	trigger_id = body.get("trigger_id")
	if not trigger_id:
		return
	await client.views_open(
		trigger_id=trigger_id,
		view={
			"type": "modal",
			"callback_id": _LINKEDIN_NOTE_MODAL_ID,
			"title": {"type": "plain_text", "text": "Copy connection note"},
			"close": {"type": "plain_text", "text": "Done"},
			"blocks": [
				{
					"type": "context",
					"elements": [{"type": "mrkdwn", "text": "Tap the note → *Select all* → *Copy*, then paste into LinkedIn."}],
				},
				{
					"type": "input",
					"block_id": "note_block",
					"label": {"type": "plain_text", "text": "Connection note"},
					"element": {
						"type": "plain_text_input",
						"action_id": "note_text",
						"multiline": True,
						"initial_value": note,
						"focus_on_load": True,
					},
					"optional": True,
				}
			],
		},
	)


@app.view(_LINKEDIN_NOTE_MODAL_ID)
async def handle_linkedin_note_modal_closed(ack):
	"""Modal submit is a no-op — the setter just needed to copy the text."""
	await ack()


# ── Reply in Thread — #sales-replies card button (v2 §3.1.3) ────────────
# S-11: this now actually emails the prospect (previously an interim manual
# bridge that only posted the rep's text into the Slack thread — see
# docs/plans/2026-09-10-s8-s11-tracking-and-reply-send.md). Idempotent on
# inbound_messages.status='RESPONDED' (also what mailbox_dispatcher.py's
# rolling-24h capacity subquery already recognizes, so a manual reply
# correctly counts against the sending mailbox's daily cap).

_REPLY_THREAD_MODAL_ID = "sales_reply_thread_modal"


def _load_inbound_message(session, inbound_id):
	return session.execute(
		text(
			"SELECT id, client_id, contact_id, sender_email, original_message_id, "
			"       subject, status "
			"FROM inbound_messages WHERE id = :id"
		),
		{"id": inbound_id},
	).first()


def _is_opted_out(session, contact_id) -> bool:
	if contact_id is None:
		return False
	row = session.execute(
		text("SELECT is_opted_out FROM contacts WHERE contact_id = :cid"),
		{"cid": contact_id},
	).first()
	return bool(row and row.is_opted_out)


@app.action("reply_in_thread")
async def handle_reply_in_thread(ack, body, respond, client, action):
	"""Open a modal for the rep to compose a real emailed reply."""
	await ack()
	trigger_id = body.get("trigger_id")
	channel_id = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id")
	message_ts = (body.get("container") or {}).get("message_ts")
	if not trigger_id or not channel_id or not message_ts:
		await respond(response_type="ephemeral", text=":warning: Could not open the reply composer.")
		return
	try:
		card_value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		card_value = {}
	await client.views_open(
		trigger_id=trigger_id,
		view={
			"type": "modal",
			"callback_id": _REPLY_THREAD_MODAL_ID,
			# card_value (inbound_id/contact_id/client_id) previously discarded
			# here — the modal only carried {channel, ts} and had no idea who
			# to actually email. Fixed by merging it in, mirroring the
			# LinkedIn-note modal's own already-correct pattern of passing
			# its button value through.
			"private_metadata": json.dumps({"channel": channel_id, "ts": message_ts, **card_value}),
			"title": {"type": "plain_text", "text": "Reply in thread"},
			"submit": {"type": "plain_text", "text": "Send reply"},
			"close": {"type": "plain_text", "text": "Cancel"},
			"blocks": [
				{
					"type": "input",
					"block_id": "reply_block",
					"label": {"type": "plain_text", "text": "Your reply"},
					"element": {"type": "plain_text_input", "action_id": "reply_text", "multiline": True},
				}
			],
		},
	)


@app.view(_REPLY_THREAD_MODAL_ID)
async def handle_reply_thread_modal_submit(ack, body, view, client):
	"""Actually email the prospect, then post the sent copy into the thread."""
	user_id = body.get("user", {}).get("id", "")
	try:
		meta = json.loads(view.get("private_metadata", "{}"))
	except (json.JSONDecodeError, TypeError):
		await ack(response_action="errors", errors={"reply_block": "Internal error — could not read card context. Close this and try again."})
		return
	channel = meta.get("channel")
	ts = meta.get("ts")
	inbound_id = meta.get("inbound_id")
	client_id = meta.get("client_id")
	contact_id = meta.get("contact_id")
	text_val = (
		view.get("state", {}).get("values", {})
		.get("reply_block", {}).get("reply_text", {}).get("value", "")
	)
	if not channel or not ts or not text_val:
		await ack(response_action="errors", errors={"reply_block": "Missing reply text or card context."})
		return
	if not inbound_id or not client_id:
		# Pre-S-11 cards (posted before this fix landed) never carried these
		# in their button value — fail visibly rather than silently sending
		# nothing, matching this repo's own "never silently no-op" posture.
		await ack(response_action="errors", errors={"reply_block": "This card is too old to reply from — ask for a fresh one."})
		return
	# Code-review fix (Critical): this handler sends a REAL outbound email
	# from the company's mailbox — the same class of consequential action
	# as opt_out_contact/book_meeting/every work-order decision, all of
	# which check approver_authorized(). This one didn't; a click-through
	# from any channel member could previously trigger a real, externally-
	# visible send with zero authorization check.
	if not approver_authorized(user_id, client_id=client_id):
		await ack(response_action="errors", errors={"reply_block": "Not authorized to reply from this card."})
		return

	with get_db_context(client_id=client_id) as session:
		row = _load_inbound_message(session, inbound_id)
		if row is None:
			await ack(response_action="errors", errors={"reply_block": "Original message not found."})
			return
		if row.status == "RESPONDED":
			await ack(response_action="errors", errors={"reply_block": "Already replied to this message."})
			return
		if _is_opted_out(session, contact_id):
			await ack(response_action="errors", errors={"reply_block": "This contact has opted out — reply blocked."})
			return

		try:
			mailbox = get_active_mailbox_for_client(session, client_id)
		except AllMailboxesCapped:
			await ack(response_action="errors", errors={"reply_block": "All sending mailboxes are at capacity today — try again later."})
			return
		except NoMailboxAvailable:
			await ack(response_action="errors", errors={"reply_block": "No sending mailbox configured for this client."})
			return

		unsub_url = unsubscribe_url(client_id, row.sender_email)
		body_with_footer = append_unsubscribe_footer(text_val, unsub_url)
		subject = row.subject or "Re: your message"
		if not subject.lower().startswith("re:"):
			subject = f"Re: {subject}"

		try:
			build_email_sender().send(
				from_address=mailbox.mailbox_address,
				to_address=row.sender_email,
				subject=subject,
				body=body_with_footer,
				sending_domain=mailbox.sending_domain,
				in_reply_to=row.original_message_id,
				list_unsubscribe_url=unsub_url,
			)
		except Exception as exc:  # noqa: BLE001 — surface as a modal error, never a silent send failure
			logger.error("[listeners] reply send failed inbound_id=%s: %s", inbound_id, exc, exc_info=True)
			await ack(response_action="errors", errors={"reply_block": "Send failed — try again or contact support."})
			return

		session.execute(
			text("UPDATE inbound_messages SET status = 'RESPONDED', responded_at = NOW() WHERE id = :id"),
			{"id": inbound_id},
		)
		_shared_log_event(
			client_id,
			"sales_reply_sent",
			entity_type="inbound_message",
			entity_id=str(inbound_id),
			payload={"inbound_id": str(inbound_id), "contact_id": contact_id},
			actor=f"slack:{user_id}",
			session=session,
		)
		session.commit()

	await ack()
	await client.chat_postMessage(
		channel=channel,
		thread_ts=ts,
		text=f":envelope_with_arrow: Reply sent by <@{user_id}>:\n>{text_val}",
	)


# ── Book Meeting — #sales-replies card button (S-11) ─────────────────────

@app.action("book_meeting")
async def handle_book_meeting(ack, body, respond, action):
	"""Emails the prospect a real booking link (Blackink's own internal
	sales-demo calendar — resolve_booking_link()'s INTERNAL_SALES_DEMO
	scope), reusing the same mailbox/send path as Reply in Thread. Fails
	visibly (never a silent no-op) if no default sales-booking connection
	is flagged — see booking_link.py's own docstring for that pre-existing,
	separate gap."""
	await ack()
	user_id = body.get("user", {}).get("id", "")
	try:
		value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		await respond(response_type="ephemeral", text=":warning: Malformed card payload.")
		return

	inbound_id = value.get("inbound_id")
	client_id = value.get("client_id")
	contact_id = value.get("contact_id")
	if not approver_authorized(user_id, client_id=client_id):
		await respond(response_type="ephemeral", text=":no_entry: Not authorized to act on this card.")
		return
	if not inbound_id or not client_id:
		await respond(response_type="ephemeral", text=":warning: This card is too old — ask for a fresh one.")
		return

	with get_db_context(client_id=client_id) as session:
		row = _load_inbound_message(session, inbound_id)
		if row is None:
			await respond(response_type="ephemeral", text=":warning: Original message not found.")
			return
		if _is_opted_out(session, contact_id):
			await respond(response_type="ephemeral", text=":warning: This contact has opted out.")
			return

		link = resolve_booking_link(session, email=row.sender_email)
		if link is None:
			await respond(
				response_type="ephemeral",
				text=":warning: No default sales-booking calendar is configured — an operator must flag one before this button can send a real link.",
			)
			return

		try:
			mailbox = get_active_mailbox_for_client(session, client_id)
		except AllMailboxesCapped:
			await respond(response_type="ephemeral", text=":warning: All sending mailboxes are at capacity today.")
			return
		except NoMailboxAvailable:
			await respond(response_type="ephemeral", text=":warning: No sending mailbox configured for this client.")
			return

		unsub_url = unsubscribe_url(client_id, row.sender_email)
		body_text = (
			f"Here's a link to grab a time that works for you: {link.url}\n\n"
			"Talk soon,\nThe Blackink team"
		)
		body_with_footer = append_unsubscribe_footer(body_text, unsub_url)
		subject = row.subject or "Let's find a time"
		if not subject.lower().startswith("re:"):
			subject = f"Re: {subject}"

		try:
			build_email_sender().send(
				from_address=mailbox.mailbox_address,
				to_address=row.sender_email,
				subject=subject,
				body=body_with_footer,
				sending_domain=mailbox.sending_domain,
				in_reply_to=row.original_message_id,
				list_unsubscribe_url=unsub_url,
			)
		except Exception as exc:  # noqa: BLE001
			logger.error("[listeners] book_meeting send failed inbound_id=%s: %s", inbound_id, exc, exc_info=True)
			await respond(response_type="ephemeral", text=":warning: Send failed — try again or contact support.")
			return

		session.execute(
			text("UPDATE inbound_messages SET status = 'RESPONDED', responded_at = NOW() WHERE id = :id"),
			{"id": inbound_id},
		)
		_shared_log_event(
			client_id,
			"sales_meeting_link_sent",
			entity_type="inbound_message",
			entity_id=str(inbound_id),
			payload={"inbound_id": str(inbound_id), "contact_id": contact_id},
			actor=f"slack:{user_id}",
			session=session,
		)
		session.commit()

	await respond(response_type="ephemeral", text=f":calendar: Booking link sent to {row.sender_email}.")


# ── Opt-out — Mark Opt-Out button on #sales-replies cards (ticket 27) ────
# This action carries contact_id + client_id directly in its value (NOT a
# payload hash). The button intentionally never expires — ticket 15 specifies
# that the opt-out button must remain actionable on old cards.


@app.action("opt_out_contact")
async def handle_opt_out_contact(ack, body, respond, action):
	"""Halt a contact's active sequence and mark them globally opted out.
	Called from the Mark Opt-Out button on #sales-replies cards (ticket 27)."""
	await ack()
	user_id = body.get("user", {}).get("id", "")
	try:
		value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		await respond(response_type="ephemeral", text=":warning: Malformed opt-out payload.")
		return

	contact_id_raw = value.get("contact_id")
	client_id = value.get("client_id")

	if not approver_authorized(user_id, client_id=client_id):
		await respond(response_type="ephemeral", text=":no_entry: Not authorized to opt out contacts.")
		return

	if not contact_id_raw:
		await respond(response_type="ephemeral", text=":warning: Missing contact ID in opt-out payload.")
		return

	try:
		contact_id = int(contact_id_raw)
	except (TypeError, ValueError):
		await respond(response_type="ephemeral", text=":warning: Invalid contact ID in opt-out payload.")
		return

	try:
		from src.services.sequence_halt import halt_sequence_for_contact
		halt_sequence_for_contact(contact_id=contact_id)
	except Exception:
		logger.error("[listeners] opt_out_contact failed contact_id=%s", contact_id, exc_info=True)
		await respond(
			response_type="ephemeral",
			text=":warning: Failed to process opt-out. Try again or contact support.",
		)
		return

	_log_event(
		client_id or "system",
		"opt_out_recorded",
		entity_id=str(contact_id),
		actor=f"slack:{user_id}",
		payload={"contact_id": contact_id},
	)
	await respond(
		response_type="ephemeral",
		text=f":white_check_mark: Contact `{contact_id}` opted out — all active sequences halted globally.",
	)


@app.action("snooze_1h")
@app.action("snooze_4h")
@app.action("snooze_tomorrow_9am")
async def handle_snooze(ack, body, respond, action):
	await ack()
	user_id = body.get("user", {}).get("id", "")
	try:
		value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		await respond(response_type="ephemeral", text=":warning: Malformed button payload.")
		return
	try:
		order = await _load_and_verify(value, user_id, respond=respond)
	except ClickRejected:
		return
	until = _snooze_until(value.get("duration", "1h"))
	snoozed = wo.snooze(order.client_id, order.action_id, until=until, decided_by=f"slack:{user_id}")
	if snoozed is None:
		await respond(response_type="ephemeral", text=":information_source: This was just decided by someone else.")
		return
	_log_event(order.client_id, "work_order_decided", entity_id=order.action_id, actor=f"slack:{user_id}", payload={"decision": "SNOOZED", "until": until.isoformat()})
	if snoozed.slack_channel_id and snoozed.slack_message_ts:
		await post.update_card(
			channel_id=snoozed.slack_channel_id,
			message_ts=snoozed.slack_message_ts,
			text=f"{_card_text(snoozed)}\n\n*Snoozed* until {until.isoformat()} by <@{user_id}>",
		)


# ── Revise — non-terminal, opens a modal (Dev 3 plan §9 item 1 decided
# default: captures a free-text note, records SKIPPED with the note in
# execution_receipt, posts to #blackink-setter. Does NOT edit the payload
# in Week 0.) ──────────────────────────────────────────────────────────

_REVISE_CALLBACK_ID = "revise_work_order"


@app.action("revise")
async def handle_revise_open(ack, body, respond, action, client):
	"""Goes through the SAME _load_and_verify prelude as every terminal
	action. It previously hand-rolled its own wo.get() load, which silently
	skipped approver_authorized() — any workspace member who could see a
	card could open this modal and (via handle_revise_submit) force the
	order to SKIPPED, defeating the Slack approval control entirely."""
	await ack()
	user_id = body.get("user", {}).get("id", "")
	try:
		value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		await respond(response_type="ephemeral", text=":warning: Malformed button payload.")
		return
	try:
		order = await _load_and_verify(value, user_id, respond=respond)
	except ClickRejected:
		return
	private_metadata = json.dumps(
		{
			"client_id": order.client_id,
			"action_id": order.action_id,
			"payload_hash": payload_hash.compute(order),
			"channel_id": order.slack_channel_id,
			"message_ts": order.slack_message_ts,
		}
	)
	await client.views_open(
		trigger_id=body["trigger_id"],
		view={
			"type": "modal",
			"callback_id": _REVISE_CALLBACK_ID,
			"private_metadata": private_metadata,
			"title": {"type": "plain_text", "text": "Revise"},
			"submit": {"type": "plain_text", "text": "Submit"},
			"close": {"type": "plain_text", "text": "Cancel"},
			"blocks": [
				{
					"type": "input",
					"block_id": "revision_note_block",
					"label": {"type": "plain_text", "text": "Revision note"},
					"element": {"type": "plain_text_input", "action_id": "revision_note", "multiline": True},
				}
			],
		},
	)


@app.view(_REVISE_CALLBACK_ID)
async def handle_revise_submit(ack, body, view):
	user_id = body.get("user", {}).get("id", "")
	try:
		meta = json.loads(view.get("private_metadata", "{}"))
	except (json.JSONDecodeError, TypeError):
		await ack(response_action="errors", errors={"revision_note_block": "Internal error — could not read card context. Close this and try again."})
		return

	client_id, action_id, provided_hash = meta.get("client_id"), meta.get("action_id"), meta.get("payload_hash")

	# Re-checked here even though handle_revise_open already gated the modal
	# open: a view_submission is a SEPARATE inbound request carrying only
	# private_metadata this app wrote earlier, and nothing in it proves the
	# submitter is the person the modal was opened for. Defense in depth,
	# the same reason _load_and_verify re-reads status rather than trusting
	# the card.
	if not approver_authorized(user_id, client_id=client_id):
		await ack(response_action="errors", errors={"revision_note_block": "Not authorized to act on this card."})
		return

	order = wo.get(client_id, action_id) if client_id and action_id else None
	if order is None:
		await ack(response_action="errors", errors={"revision_note_block": "This card is no longer available."})
		return

	verdict = payload_hash.verify(order, provided_hash)
	if not verdict.fresh:
		await ack(
			response_action="errors",
			errors={"revision_note_block": "This card is out of date — nothing was saved. Close this and use the refreshed card."},
		)
		refreshed = wo.get(client_id, action_id)
		if refreshed is not None:
			await post_work_order_card(refreshed, channel_key="setter")
		return

	if order.status != "QUEUED":
		await ack(response_action="errors", errors={"revision_note_block": f"Already decided (status={order.status})."})
		return

	note = view["state"]["values"]["revision_note_block"]["revision_note"]["value"] or ""

	await ack()

	with get_db_context(client_id=client_id) as session:
		session.execute(
			text(
				"UPDATE agent_work_orders SET status = 'SKIPPED', decided_by = :decided_by, "
				"decided_at = NOW(), updated_at = NOW(), execution_receipt = :receipt "
				"WHERE action_id = :action_id AND client_id = :client_id AND status = 'QUEUED'"
			),
			{
				"decided_by": f"slack:{user_id}",
				"receipt": json.dumps({"revision_note": note}),
				"action_id": action_id,
				"client_id": client_id,
			},
		)

	_log_event(client_id, "work_order_revised", entity_id=action_id, actor=f"slack:{user_id}", payload={"note": note})

	if order.slack_channel_id and order.slack_message_ts:
		await post.update_card(
			channel_id=order.slack_channel_id,
			message_ts=order.slack_message_ts,
			text=f"{_card_text(order)}\n\n*Revision requested* by <@{user_id}>: {note}",
		)
	await post.post_notice(channel_key="setter", text=f":pencil2: Revision on `{action_id[:8]}` by <@{user_id}>: {note}")


@app.view_closed(_REVISE_CALLBACK_ID)
async def handle_revise_closed(ack):
	await ack()


# ── Relay halt / resume — control actions, NO work order involved ───────


def _halt_confirmation_blocks(halt_id: int, token: str) -> list:
	return [
		{
			"type": "actions",
			"elements": [
				{
					"type": "button",
					"text": {"type": "plain_text", "text": "Resume"},
					"style": "danger",
					"action_id": "halt_resume",
					"value": json.dumps({"halt_id": halt_id, "token": token}),
				}
			],
		}
	]


@app.command("/blackink-halt")
async def handle_halt_command(ack, respond, command):
	await ack()
	user_id = command.get("user_id", "")
	text_arg = (command.get("text") or "").strip()

	if text_arg.lower() == "status":
		halts = halt_service.get_active_halts()
		if not halts:
			await respond(response_type="ephemeral", text="No active halts.")
			return
		lines = [f"#{h.halt_id} {h.scope}" + (f":{h.scope_id}" if h.scope_id else "") + f" — {h.reason} (by {h.issued_by})" for h in halts]
		await respond(response_type="ephemeral", text="Active halts:\n" + "\n".join(lines))
		return

	parts = text_arg.split(maxsplit=2)
	if not parts or parts[0].upper() not in ("GLOBAL", "CLIENT", "CAMPAIGN"):
		await respond(response_type="ephemeral", text="Usage: `/blackink-halt <global|client|campaign> [scope_id] [reason...]` or `/blackink-halt status`")
		return

	scope = parts[0].upper()
	scope_id: Optional[str] = None
	reason = ""
	if scope == "GLOBAL":
		reason = " ".join(parts[1:]) if len(parts) > 1 else "(no reason given)"
	else:
		if len(parts) < 2:
			await respond(response_type="ephemeral", text=f"Usage: `/blackink-halt {scope.lower()} <scope_id> [reason...]`")
			return
		scope_id = parts[1]
		reason = parts[2] if len(parts) > 2 else "(no reason given)"

	client_id_for_auth = scope_id if scope == "CLIENT" else None
	if not approver_authorized(user_id, client_id=client_id_for_auth):
		await respond(response_type="ephemeral", text=":no_entry: Not authorized to issue halts.")
		return

	halt_id = halt_service.issue_halt(scope, scope_id=scope_id, reason=reason, issued_by=f"slack:{user_id}")
	token = generate_resume_token(halt_id)

	await respond(response_type="in_channel", text=f":stop_sign: Halt #{halt_id} issued ({scope}" + (f":{scope_id}" if scope_id else "") + f") by <@{user_id}>: {reason}")
	await post.post_action_card(
		channel_key="command",
		text=f":stop_sign: Halt #{halt_id} — {scope}" + (f":{scope_id}" if scope_id else "") + f"\nReason: {reason}\nIssued by: <@{user_id}>",
		blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f":stop_sign: *Halt #{halt_id}* — {scope}" + (f":{scope_id}" if scope_id else "") + f"\nReason: {reason}\nIssued by: <@{user_id}>"}}] + _halt_confirmation_blocks(halt_id, token),
	)


@app.command("/blackink-resume")
async def handle_resume_command(ack, respond, command):
	"""Fallback path — normally the operator clicks the Resume button on
	the halt card, which already carries the token. This exists for when
	that card has scrolled out of view."""
	await ack()
	user_id = command.get("user_id", "")
	text_arg = (command.get("text") or "").strip()
	if not text_arg.isdigit():
		await respond(response_type="ephemeral", text="Usage: `/blackink-resume <halt_id>` — token-less resume is not supported; use the Resume button on the halt card.")
		return
	await respond(
		response_type="ephemeral",
		text=f"Halt #{text_arg}: use the Resume button on the original halt card in #blackink-command — this command cannot resume without the token that card carries.",
	)


@app.action("halt_resume")
async def handle_halt_resume_click(ack, body, respond, action):
	await ack()
	user_id = body.get("user", {}).get("id", "")
	try:
		value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		await respond(response_type="ephemeral", text=":warning: Malformed button payload.")
		return

	if not approver_authorized(user_id):
		await respond(response_type="ephemeral", text=":no_entry: Not authorized to resume halts.")
		return

	halt_id = value.get("halt_id")
	token = value.get("token", "")
	ok = halt_service.resume_halt(halt_id, token=token, resumed_by=f"slack:{user_id}")
	if not ok:
		await respond(response_type="ephemeral", text=f":warning: Could not resume halt #{halt_id} — it may already be resumed, or this button's token is stale (a newer halt on the same scope was issued since).")
		return

	await respond(response_type="in_channel", text=f":white_check_mark: Halt #{halt_id} resumed by <@{user_id}>.")


# ── Mark No-Show — Subtask 3.2.3, control action, no work order ─────────

_INTERNAL_SALES_CLIENT_ID = "BLACKINK_INTERNAL_SALES"
_NO_SHOW_WINDOW = timedelta(minutes=10)


@app.action("mark_no_show")
async def handle_mark_no_show(ack, body, respond, action):
	"""Steps 1-8 of Subtask 3.2.3's mark-no-show flow. ack() first (no
	network I/O before it — Slack's 3-second budget), then verify the
	token, the 10-minute timing window (both boundaries), and the
	double-click guard, THEN run trigger_recovery() as one transaction
	with no external calls inside it — the recovery email itself is sent
	later, out-of-band, by src/tasks/no_show_recovery_sender.py."""
	await ack()
	user_id = body.get("user", {}).get("id", "")
	try:
		value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		await respond(response_type="ephemeral", text=":warning: Malformed button payload.")
		return

	if not approver_authorized(user_id):
		await respond(response_type="ephemeral", text=":no_entry: Not authorized to mark no-shows.")
		return

	booking_id = value.get("booking_id")
	token = value.get("token", "")
	if not booking_id or not verify_no_show_token(booking_id, token):
		await respond(response_type="ephemeral", text=":warning: Stale or invalid button — this card may have been superseded.")
		return

	with get_db_context(client_id=_INTERNAL_SALES_CLIENT_ID) as session:
		booking = session.execute(
			text("SELECT booking_id, scheduled_at, event_status FROM bookings WHERE booking_id = :bid"),
			{"bid": booking_id},
		).first()
		if booking is None:
			await respond(response_type="ephemeral", text=":warning: Booking not found.")
			return
		if booking.event_status == "CANCELLED":
			await respond(response_type="ephemeral", text=":information_source: This booking was cancelled — nothing to mark.")
			return

		now = datetime.now(timezone.utc)
		if now < booking.scheduled_at:
			await respond(response_type="ephemeral", text=":warning: This meeting hasn't started yet.")
			return
		if now > booking.scheduled_at + _NO_SHOW_WINDOW:
			await respond(response_type="ephemeral", text=":warning: The 10-minute no-show window for this meeting has passed.")
			return

		if already_recorded(session, booking_id):
			await respond(response_type="ephemeral", text=":information_source: Already marked no-show by someone else.")
			return

		trigger_recovery(session, booking_id=booking_id, submitted_by=f"slack:{user_id}")

	await respond(response_type="in_channel", text=f":x: Marked no-show for booking `{booking_id}` by <@{user_id}> — recovery email enqueued.")
# ── Post-meeting outcome modal (blueprint §3.1.7) — NO trigger wired here
# by design. The Outbound Sequencer & Booking Engine's post-meeting Slack
# card (not yet shipped) is the only entry point: its button handler calls
# open_meeting_outcome_modal() directly. See this task's brief for the
# rejected-alternative note on why there is deliberately no interim slash
# command. ─────────────────────────────────────────────────────────────

_MEETING_OUTCOME_CALLBACK_ID = "meeting_outcome_modal"

_PM_SOFTWARE_OPTIONS = ["AppFolio", "Buildium", "Propertyware", "Rent Manager", "Other", "Unknown"]
_OBJECTION_OPTIONS = ["Pricing", "Software Integration", "Capacity", "Existing Agency"]


def _meeting_outcome_modal_view(contact_id: str, meeting_occurred_at: str) -> dict:
	return {
		"type": "modal",
		"callback_id": _MEETING_OUTCOME_CALLBACK_ID,
		"private_metadata": json.dumps({"contact_id": contact_id, "meeting_occurred_at": meeting_occurred_at}),
		"title": {"type": "plain_text", "text": "Log Meeting Outcome"},
		"submit": {"type": "plain_text", "text": "Submit"},
		"close": {"type": "plain_text", "text": "Cancel"},
		"blocks": [
			{
				"type": "input", "block_id": "attendance_block",
				"label": {"type": "plain_text", "text": "Meeting Attendance Status"},
				"element": {
					"type": "static_select", "action_id": "attendance",
					"options": [{"text": {"type": "plain_text", "text": v}, "value": v} for v in ("Held", "No-Show", "Rescheduled")],
				},
			},
			{
				"type": "input", "block_id": "pm_software_block", "optional": True,
				"label": {"type": "plain_text", "text": "Target PM Software"},
				"element": {
					"type": "static_select", "action_id": "pm_software",
					"options": [{"text": {"type": "plain_text", "text": v}, "value": v} for v in _PM_SOFTWARE_OPTIONS],
				},
			},
			{
				"type": "input", "block_id": "door_count_block", "optional": True,
				"label": {"type": "plain_text", "text": "Estimated Door Count"},
				"element": {"type": "number_input", "action_id": "door_count", "is_decimal_allowed": False},
			},
			{
				"type": "input", "block_id": "objections_block", "optional": True,
				"label": {"type": "plain_text", "text": "Stated Objections"},
				"element": {
					"type": "multi_static_select", "action_id": "objections",
					"options": [{"text": {"type": "plain_text", "text": v}, "value": v} for v in _OBJECTION_OPTIONS],
				},
			},
			{
				"type": "input", "block_id": "next_action_block",
				"label": {"type": "plain_text", "text": "Next Action"},
				"element": {"type": "plain_text_input", "action_id": "next_action", "max_length": 280},
			},
		],
	}


async def open_meeting_outcome_modal(*, trigger_id: str, contact_id: str, meeting_occurred_at: str) -> bool:
	"""THE entry point into the post-meeting form — exported for the
	Outbound Sequencer & Booking Engine's booking-confirmation card handler
	to call from its "Log Outcome" button. There is deliberately no slash
	command and no other trigger (see this task's design note): that card
	is the only way in.

	trigger_id comes from the Slack interaction that is opening this modal
	and expires ~3 seconds after it — call this immediately on the click,
	never after an await that could cross that window.

	meeting_occurred_at is an ISO 8601 string, validated here rather than
	trusted: it becomes part of the modal's private_metadata and then the
	upsert key, so a malformed value would surface as a confusing failure
	at submit time, long after the mistake. contact_id is validated for the
	identical reason — it is int()-cast unguarded at submit time in
	handle_meeting_outcome_submit.
	"""
	try:
		_datetime.fromisoformat(meeting_occurred_at)
	except (ValueError, TypeError):
		logger.error(
			"[listeners] open_meeting_outcome_modal: meeting_occurred_at=%r is not ISO 8601 — modal not opened",
			meeting_occurred_at,
		)
		return False
	try:
		int(contact_id)
	except (ValueError, TypeError):
		logger.error(
			"[listeners] open_meeting_outcome_modal: contact_id=%r is not an int — modal not opened",
			contact_id,
		)
		return False
	return await post.open_modal(
		trigger_id=trigger_id,
		view=_meeting_outcome_modal_view(contact_id, meeting_occurred_at),
	)


@app.view(_MEETING_OUTCOME_CALLBACK_ID)
async def handle_meeting_outcome_submit(ack, body, view):
	user_id = body.get("user", {}).get("id", "")
	meta = json.loads(view.get("private_metadata", "{}"))
	values = view["state"]["values"]

	attendance = values["attendance_block"]["attendance"]["selected_option"]["value"]
	pm_software_option = values["pm_software_block"]["pm_software"].get("selected_option")
	door_count_raw = values["door_count_block"]["door_count"].get("value")
	objection_options = values["objections_block"]["objections"].get("selected_options") or []
	next_action = values["next_action_block"]["next_action"]["value"]

	await ack()

	# contacts has no client_id column of its own (scoped through its parent
	# companies.owning_client_id, per config/tenant_policies.py's "join"
	# mode) — resolving the real owning client_id for an arbitrary
	# contact_id requires a cross-tenant lookup. This is a batch/system-style
	# read used only to find which tenant a Slack-submitted contact_id
	# belongs to before writing through the tenant-scoped path, consistent
	# with queued_depth(client_id=None)'s existing precedent.
	with get_system_db_context() as session:
		row = session.execute(
			text("SELECT co.owning_client_id FROM contacts c JOIN companies co USING(company_id) WHERE c.contact_id = :cid"),
			{"cid": int(meta["contact_id"])},
		).mappings().first()
	if row is None or row["owning_client_id"] is None:
		await post.post_notice(channel_key="qa", text=f":warning: meeting_outcome submit for unresolvable contact_id={meta['contact_id']}")
		return
	client_id_for_contact = row["owning_client_id"]

	try:
		record_outcome(
			client_id_for_contact,
			contact_id=int(meta["contact_id"]),
			meeting_occurred_at=_datetime.fromisoformat(meta["meeting_occurred_at"]),
			attendance_status=attendance,
			pm_software=pm_software_option["value"] if pm_software_option else None,
			door_count_est=int(door_count_raw) if door_count_raw else None,
			objections=[o["value"] for o in objection_options],
			next_action=next_action,
			recorded_by=f"slack:{user_id}",
		)
	except Exception as exc:
		logger.error(
			"[listeners] record_outcome failed for contact_id=%s — outcome lost",
			meta["contact_id"], exc_info=True,
		)
		await post.post_notice(
			channel_key="qa",
			text=f":warning: meeting_outcome record_outcome failed for contact_id={meta['contact_id']}: {exc}",
		)
		return

	if attendance == "No-Show":
		# TODO(no-show-pause): the real outbound-sequence pause lives in the
		# Outbound Sequencer & Booking Engine's Subtask 3.2.3 no-show
		# handler, which does not exist on this branch yet — this notice is
		# a visible flag of that gap, not a substitute for the real pause.
		await post.post_notice(
			channel_key="setter",
			text=f":warning: No-show recorded for contact `{meta['contact_id']}` by <@{user_id}> — outbound sequence pause is the Booking Engine's no-show handler (Subtask 3.2.3), not yet wired here.",
		)


# ── Log Outcome trigger card (Addendum to Subtask 3.2.1) ────────────────
# The card posted by src/tasks/meeting_outcome_prompt_sender.py carries a
# "Log Outcome" button. This handler is the ONLY new piece the addendum
# adds to the outcome flow: it validates the card, then opens the canonical
# §3.1.7 modal above (open_meeting_outcome_modal). The modal, its submit
# handler, and record_outcome are the base's — not re-implemented here.

_MEETING_OUTCOME_CARD_TTL = timedelta(hours=24)


def _meeting_outcome_expired(order: "wo.WorkOrder", *, now: datetime) -> bool:
	# The 24h expiry is deliberately NOT part of the payload hash (that
	# preimage is FIXED); it's an explicit created_at + TTL check here.
	return now > order.created_at + _MEETING_OUTCOME_CARD_TTL


def _booking_is_cancelled(session, booking_id) -> bool:
	"""Live booking-status read — a booking cancelled after its card was
	posted must not still present the outcome form."""
	if booking_id is None:
		return False
	row = session.execute(
		text("SELECT event_status FROM bookings WHERE booking_id = :bid"),
		{"bid": booking_id},
	).first()
	return row is None or row.event_status == "CANCELLED"


@app.action("log_meeting_outcome")
async def handle_log_meeting_outcome(ack, body, respond, action, client):
	"""Opens the canonical meeting-outcome modal for the assigned closer.
	Everything here is in-memory / one DB read so open_meeting_outcome_modal
	lands inside Slack's ~3s trigger_id window."""
	await ack()
	user_id = body.get("user", {}).get("id", "")
	try:
		value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		await respond(response_type="ephemeral", text=":warning: Malformed button payload.")
		return

	client_id = value.get("client_id")
	action_id = value.get("action_id")
	provided_hash = value.get("payload_hash")

	if not approver_authorized(user_id, client_id=client_id):
		await respond(response_type="ephemeral", text=":no_entry: Not authorized to act on this card.")
		return

	order = wo.get(client_id, action_id) if client_id and action_id else None
	if order is None:
		await respond(response_type="ephemeral", text=":warning: This card could not be found (it may be stale or belong to a different tenant).")
		return

	verdict = payload_hash.verify(order, provided_hash)
	now = datetime.now(timezone.utc)
	if not verdict.fresh or _meeting_outcome_expired(order, now=now):
		_log_event(client_id, "meeting_outcome_prompt_rejected", entity_id=action_id, actor=f"slack:{user_id}", payload={"reason": "expired_or_altered"})
		await respond(response_type="ephemeral", text=":warning: This action has expired or was altered.")
		return

	if order.recipient != user_id:
		await respond(response_type="ephemeral", text=":no_entry: This meeting is assigned to a different closer.")
		return

	# Live cancellation re-check before opening the modal.
	with get_db_context(client_id=_INTERNAL_SALES_CLIENT_ID) as session:
		if _booking_is_cancelled(session, (order.payload or {}).get("booking_id")):
			await respond(response_type="ephemeral", text=":information_source: This meeting was cancelled — no outcome can be logged.")
			return

	# Hand off to the base's canonical §3.1.7 modal (it validates contact_id /
	# meeting_occurred_at itself and derives everything else at submit time).
	await open_meeting_outcome_modal(
		trigger_id=body["trigger_id"],
		contact_id=value.get("contact_id"),
		meeting_occurred_at=value.get("meeting_occurred_at"),
	)


# ── Context card claim (Subtask 2.1.2 — Reply Triage Agent) ─────────────────

from src.agents.respond.context_cards import CARD_EXPIRY as _CONTEXT_CARD_TTL, compute_card_hash


@app.action("claim_context_card")
async def handle_claim_context_card(ack, body, respond, action, client):
	"""Rep clicks "Claim this lead" on a HOT_LEAD/WHALE_OWNER/OBJECTION card.

	Verifies the card hash and 24-hour expiry, then:
	  - Sets claimed_at / claimed_by on the inbound_messages row
	  - Updates the Slack card in place to show who claimed it

	Ephemeral errors on: expired card, wrong hash, already claimed,
	row not found. Never raises — the rep sees a clear message in Slack.
	"""
	await ack()
	user_id = body.get("user", {}).get("id", "unknown")
	try:
		value = json.loads(action.get("value", "{}"))
	except (json.JSONDecodeError, TypeError):
		await respond(response_type="ephemeral", text=":warning: Malformed button payload.")
		return

	db_id = value.get("db_id")
	card_client_id = value.get("client_id")
	provided_hash = value.get("card_hash", "")

	if not db_id or not card_client_id:
		await respond(response_type="ephemeral", text=":warning: Malformed button payload — missing db_id or client_id.")
		return

	with get_system_db_context() as session:
		row = session.execute(
			text(
				"SELECT id, intent, card_posted_at, card_ts, card_channel_id, "
				"       claimed_at, claimed_by, status "
				"FROM inbound_messages WHERE id = :id"
			),
			{"id": db_id},
		).mappings().first()

	if row is None:
		await respond(response_type="ephemeral", text=":warning: Lead not found.")
		return

	if row["status"] not in ("ROUTED",):
		await respond(response_type="ephemeral", text=f":information_source: This lead is already in status `{row['status']}` — no claim needed.")
		return

	# 24-hour expiry check.
	card_posted_at = row["card_posted_at"]
	if card_posted_at is None:
		await respond(response_type="ephemeral", text=":warning: Card metadata missing — cannot verify. Contact support.")
		return
	if card_posted_at.tzinfo is None:
		card_posted_at = card_posted_at.replace(tzinfo=timezone.utc)
	if datetime.now(timezone.utc) > card_posted_at + _CONTEXT_CARD_TTL:
		await respond(response_type="ephemeral", text=":warning: This card has expired (>24 hours). The lead may have been reallocated.")
		return

	# Hash verification
	card_posted_at_iso = card_posted_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
	expected = compute_card_hash(db_id, card_client_id, row["intent"] or "", card_posted_at_iso)
	if not _hmac.compare_digest(provided_hash, expected):
		await respond(response_type="ephemeral", text=":warning: This action has expired or was altered.")
		return

	# Already claimed?
	if row["claimed_at"] is not None:
		await respond(response_type="ephemeral", text=f":information_source: Already claimed by <@{row['claimed_by']}>.")
		return

	# Claim it — WHERE clause guards against a race between two reps.
	with get_system_db_context() as session:
		result = session.execute(
			text(
				"UPDATE inbound_messages "
				"SET claimed_at = NOW(), claimed_by = :uid "
				"WHERE id = :id AND claimed_at IS NULL "
				"RETURNING id"
			),
			{"uid": user_id, "id": db_id},
		).first()
		session.commit()

	if result is None:
		await respond(response_type="ephemeral", text=":information_source: Another rep just claimed this lead — you were a fraction of a second too slow.")
		return

	# Update the Slack card in place.
	intent_label = (row["intent"] or "LEAD").replace("_", " ").title()
	if row["card_ts"] and row["card_channel_id"]:
		await client.chat_update(
			channel=row["card_channel_id"],
			ts=row["card_ts"],
			text=f"✅ {intent_label} — claimed by <@{user_id}>",
			blocks=[
				{
					"type": "section",
					"text": {
						"type": "mrkdwn",
						"text": f"✅ *{intent_label}* — claimed by <@{user_id}>",
					},
				}
			],
		)


# ── Ink Campaign — Approve / Reject draft card buttons ───────────────────────
#
# Cora posts a draft-approval card to #blackink-command for each campaign.
# These handlers publish a resume signal to ink:resume_signals so the Ink
# worker can proceed past wait_approve. No payload hash needed here — the
# approval decision is idempotent (publishing twice is harmless; the worker
# guards against a completed graph via get_state().next check).


async def _handle_ink_campaign_decision(
    ack, body, action, approved: bool,
) -> None:
    await ack()
    user_id = body.get("user", {}).get("id", "unknown")
    try:
        value = json.loads(action.get("value", "{}"))
    except (json.JSONDecodeError, TypeError):
        logger.warning("ink.listeners: malformed ink campaign button value")
        return

    work_order_id = value.get("work_order_id", "")
    if not work_order_id:
        logger.warning("ink.listeners: ink campaign button missing work_order_id")
        return

    from src.services.slack.auth import approver_authorized
    if not approver_authorized(user_id):
        logger.warning(
            "ink.listeners: unauthorized campaign decision attempt work_order_id=%s user_id=%s",
            work_order_id, user_id,
        )
        return

    try:
        from src.api.ink_webhook_router import publish_resume_signal
        publish_resume_signal(work_order_id=work_order_id, approved=approved, approved_by=user_id)
    except Exception as exc:
        logger.error(
            "ink.listeners: failed to publish resume signal work_order_id=%s: %s",
            work_order_id, exc,
        )
        return

    decision_label = "Approved" if approved else "Rejected"
    icon = ":white_check_mark:" if approved else ":x:"
    logger.info(
        "ink.listeners: campaign %s work_order_id=%s by user_id=%s",
        decision_label, work_order_id, user_id,
    )

    # Update the card in place so the buttons are replaced with a status line
    try:
        channel = body.get("channel", {}).get("id")
        ts      = body.get("message", {}).get("ts")
        original_blocks = body.get("message", {}).get("blocks", [])
        # Keep header + metrics blocks, replace actions block with status
        display_blocks = [b for b in original_blocks if b.get("type") != "actions"]
        display_blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{icon} *{decision_label}* by <@{user_id}>",
            },
        })
        if channel and ts:
            from src.services.slack.bolt_app import get_bolt_app
            await get_bolt_app().client.chat_update(
                channel=channel,
                ts=ts,
                blocks=display_blocks,
                text=f"{decision_label} by <@{user_id}>",
            )
    except Exception as exc:
        logger.warning("ink.listeners: card update failed work_order_id=%s: %s", work_order_id, exc)

    # Decrement Cora's approval backlog counter
    from src.agents.cora.throttle import notify_approval_resolved
    notify_approval_resolved()


@app.action("approve_ink_campaign")
async def handle_approve_ink_campaign(ack, body, action):
    await _handle_ink_campaign_decision(ack, body, action, approved=True)


@app.action("reject_ink_campaign")
async def handle_reject_ink_campaign(ack, body, action):
    await _handle_ink_campaign_decision(ack, body, action, approved=False)


# ── @Blackink mention/intent router (audit item 3.0.3 — no @mention router
# existed at all; macro pipeline queries were only ever served by the
# scheduled daily_digest.py cron, never conversationally) ──────────────────

_MENTION_HELP_TEXT = (
    "I understand:\n"
    "- `@Blackink pipeline` / `@Blackink digest` / `@Blackink status` — "
    "post the last-24h pipeline digest (same numbers as the scheduled "
    "#blackink-command post, on demand)\n"
    "- `@Blackink halt status` — list active halts (same as `/blackink-halt status`)\n"
    "Anything else, and I'll show this message."
)

_DIGEST_KEYWORDS = ("pipeline", "digest", "status", "numbers", "metrics")


def _mention_text_without_bot_id(event: dict) -> str:
    """Slack renders the bot mention itself as a literal `<@U0123...>` token
    at the start of event['text'] — strip it so keyword matching below
    doesn't need to know the bot's own user id."""
    raw = event.get("text") or ""
    parts = raw.split(maxsplit=1)
    if parts and parts[0].startswith("<@") and parts[0].endswith(">"):
        return parts[1].strip().lower() if len(parts) > 1 else ""
    return raw.strip().lower()


async def handle_app_mention(event: dict, say) -> None:
    """Thin wrapper's plain-function half — same split convention as every
    other listener in this file (unit-testable with a plain dict + AsyncMock
    `say`, no real Bolt event needed).

    Reuses src.tasks.daily_digest.build_digest_text() for the pipeline
    query rather than re-deriving the metrics — one computation, whether it
    reaches Slack via the 8am cron or an on-demand mention (repo's own
    "reuse existing architecture" rule); this is a deliberately small,
    read-only first router, not the full macro-query surface implied by the
    blueprint's "command & intent dispatcher" language — see the module's
    task-analysis plan for what's explicitly out of scope."""
    text_content = _mention_text_without_bot_id(event)
    thread_ts = event.get("thread_ts") or event.get("ts")

    # "halt" is checked first: "halt status" would otherwise also match the
    # digest branch's "status" keyword below, since the two intents share
    # that one ambiguous word.
    if "halt" in text_content:
        halts = halt_service.get_active_halts()
        if not halts:
            await say(text="No active halts.", thread_ts=thread_ts)
            return
        lines = [
            f"#{h.halt_id} {h.scope}" + (f":{h.scope_id}" if h.scope_id else "") + f" — {h.reason} (by {h.issued_by})"
            for h in halts
        ]
        await say(text="Active halts:\n" + "\n".join(lines), thread_ts=thread_ts)
        return

    if any(keyword in text_content for keyword in _DIGEST_KEYWORDS):
        from src.tasks.daily_digest import build_digest_text

        digest_text = build_digest_text()
        await say(text=digest_text, thread_ts=thread_ts)
        return

    await say(text=_MENTION_HELP_TEXT, thread_ts=thread_ts)


@app.event("app_mention")
async def on_app_mention(event, say):
    await handle_app_mention(event, say)
