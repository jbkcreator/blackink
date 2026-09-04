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

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from src.agents.relay import halt_service
from src.agents.relay.resume_auth import generate_resume_token
from src.core.database import get_db_context
from src.services import work_orders as wo
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


def _log_event(client_id: str, event_type: str, *, entity_id: str, actor: str, payload: dict) -> None:
	"""Matches the raw-SQL pattern already established in
	src/tasks/promotion_sweep.py — no shared events-writer helper exists
	yet in this codebase, so this stays consistent with that rather than
	introducing a new abstraction for one caller."""
	try:
		with get_db_context(client_id=client_id) as session:
			session.execute(
				text(
					"INSERT INTO events (client_id, event_type, entity_type, entity_id, actor, payload) "
					"VALUES (:client_id, :event_type, 'work_order', :entity_id, :actor, :payload)"
				),
				{
					"client_id": client_id,
					"event_type": event_type,
					"entity_id": entity_id,
					"actor": actor,
					"payload": json.dumps(payload),
				},
			)
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
	indicator = _calling_hours_indicator()
	hours_label = _calling_hours_label(indicator)

	return [
		{"type": "header", "text": {"type": "plain_text", "text": "📞 Touch 2 · Call now", "emoji": True}},
		{
			"type": "section",
			"fields": [
				{"type": "mrkdwn", "text": f"*Contact*\n{contact_name}"},
				{"type": "mrkdwn", "text": f"*Firm*\n{firm_name}"},
				{"type": "mrkdwn", "text": f"*County*\n{county}"},
				{"type": "mrkdwn", "text": f"*Phone*\n{phone}"},
			],
		},
		{
			"type": "section",
			"text": {"type": "mrkdwn", "text": f"{indicator} *{hours_label}*"},
		},
		{
			"type": "context",
			"elements": [
				{"type": "mrkdwn", "text": f"run `{run_id_short}`  ·  `{order.action_id[:8]}`"},
			],
		},
		{"type": "divider"},
	]


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
			"text": {"type": "plain_text", "text": "Copy Note (Mobile)"},
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


def _sales_reply_content_blocks(
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
) -> list:
	"""Block Kit layout for a #sales-replies inbound reply card (ticket 28/30).

	Shows the inbound reply body, attribution status, and (if attributed) the
	run context. Has a 'Mark Opt-Out' button (ticket 27) iff contact_id is
	known — the button never expires (ticket 15) since it carries no payload
	hash, only the contact_id and client_id directly.

	Unattributed cards: no opt-out button (no contact to target). Human reads
	+ routes manually. BCC echoes (our own outbound message_id arriving back)
	are filtered BEFORE this function is called and never posted."""
	attribution_badge = "✅ attributed" if attribution_status == "attributed" else "⚠️ unattributed"
	if run_id and touch_step:
		run_ctx = f"Touch {touch_step} · run `{str(run_id)[:8]}`"
	elif run_id:
		run_ctx = f"run `{str(run_id)[:8]}`"
	else:
		run_ctx = "no run match"

	contact_label = contact_name or from_address
	firm_label = f"  ·  {firm_name}" if firm_name else ""

	# Body preview — first 20 lines, blockquoted
	body_lines = (raw_body or "(no body)").splitlines()[:20]
	quoted = "\n".join(f"> {ln}" for ln in body_lines)

	blocks: list = [
		{"type": "header", "text": {"type": "plain_text", "text": "💬 Reply received", "emoji": True}},
		{
			"type": "section",
			"fields": [
				{"type": "mrkdwn", "text": f"*From*\n{from_address}"},
				{"type": "mrkdwn", "text": f"*Contact*\n{contact_label}{firm_label}"},
				{"type": "mrkdwn", "text": f"*Attribution*\n{attribution_badge}"},
				{"type": "mrkdwn", "text": f"*Thread*\n{run_ctx}"},
			],
		},
	]
	if subject:
		blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Subject*\n_{subject}_"}})
	blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Message*\n{quoted}"}})
	blocks.append({
		"type": "context",
		"elements": [{"type": "mrkdwn", "text": f"inbound `{inbound_id[:8]}`"}],
	})
	blocks.append({"type": "divider"})

	# Opt-out button — only when we have a contact_id to target (never expires,
	# no payload hash — ticket 15 / spec). Confirm dialog prevents fat-finger.
	if contact_id is not None:
		blocks.append({
			"type": "actions",
			"elements": [
				{
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
				}
			],
		})

	return blocks


def _card_text_blocks(order: "wo.WorkOrder") -> list:
	if order.action_class == "DISPATCH_EMAIL_TOUCH":
		return _email_touch_content_blocks(order) + _card_button_blocks(order)
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
	contact_id = payload.get("contact_id")
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
					"       co.company_name, co.county_slug "
					"FROM contacts c "
					"JOIN companies co ON co.company_id = c.company_id "
					"WHERE c.contact_id = :contact_id"
				),
				{"contact_id": contact_id},
			).mappings().first()
			if row:
				contact_data = dict(row)
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
	}

	idempotency_key = wo.default_idempotency_key(
		order.client_id, str(contact_id), "DIAL_TASK", datetime.now(timezone.utc)
	)
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
			"title": {"type": "plain_text", "text": "Connection note"},
			"close": {"type": "plain_text", "text": "Done"},
			"blocks": [
				{
					"type": "input",
					"block_id": "note_block",
					"label": {"type": "plain_text", "text": "Select all and copy"},
					"element": {
						"type": "plain_text_input",
						"action_id": "note_text",
						"multiline": True,
						"initial_value": note,
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
		"contact_opted_out",
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
