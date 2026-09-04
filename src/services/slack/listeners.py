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
from zoneinfo import ZoneInfo

from src.agents.relay import halt_service
from src.agents.relay.resume_auth import generate_resume_token
from src.core.database import get_db_context
from src.services import work_orders as wo
from src.services.meeting_outcome_prompts import outcome_recorded_for_booking
from src.services.meeting_outcomes import MeetingOutcomeRecord, record_meeting_outcome
from src.services.no_show_prompts import verify_no_show_token
from src.services.no_show_recovery import already_recorded, trigger_recovery
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
	subject = order.payload.get("subject") if isinstance(order.payload, dict) else None
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


def _card_text_blocks(order: "wo.WorkOrder") -> list:
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


async def _finalize_terminal_decision(order: "wo.WorkOrder", *, decision: str, user_id: str, respond) -> None:
	decided = wo.record_decision(order.client_id, order.action_id, decision=decision, decided_by=f"slack:{user_id}")
	if decided is None:
		# Lost a race to a concurrent decision between _load_and_verify's
		# QUEUED check and this UPDATE — the double-tap guard in
		# work_orders.record_decision caught it. Not an error; just no-op.
		await respond(response_type="ephemeral", text=":information_source: This was just decided by someone else.")
		return
	_log_event(order.client_id, "work_order_decided", entity_id=order.action_id, actor=f"slack:{user_id}", payload={"decision": decision})
	if decided.slack_channel_id and decided.slack_message_ts:
		await post.update_card(
			channel_id=decided.slack_channel_id,
			message_ts=decided.slack_message_ts,
			text=f"{_card_text(decided)}\n\n*Decision:* {decision} by <@{user_id}>",
		)


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


# ── Log Outcome — Addendum to Subtask 3.2.1, opens 4.2.2's modal ────────

_MEETING_OUTCOME_CALLBACK_ID = "meeting_outcome_submit"
# The card's own 24-hour life. NOT part of the payload hash (the preimage is
# FIXED — see payload_hash.py); enforced as an explicit created_at + TTL
# check here and in the submit handler, so a click on an expired card is
# rejected exactly like a click on an altered one.
_MEETING_OUTCOME_CARD_TTL = timedelta(hours=24)
_ATTENDANCE_OPTIONS = [
	("ATTENDED", "Attended"),
	("NO_SHOW", "No-show"),
	("RESCHEDULED", "Rescheduled"),
	("CANCELLED", "Cancelled"),
]


def _meeting_outcome_expired(order: "wo.WorkOrder", *, now: datetime) -> bool:
	return now > order.created_at + _MEETING_OUTCOME_CARD_TTL


async def open_meeting_outcome_modal(
	client,
	*,
	trigger_id: str,
	contact_id,
	meeting_occurred_at,
	private_metadata: str,
) -> bool:
	"""Opens the post-meeting outcome modal (the receiving function the
	addendum's trigger card exists to call). Validates the two values the
	button carries — contact_id must be int-parseable, meeting_occurred_at
	must parse as ISO 8601 — and, on malformed input, logs a warning and
	returns WITHOUT opening, no crash and no modal (the DoD's own guard-
	clause line). trigger_id is single-use and ~3s-lived, so the caller must
	reach this with no awaited network I/O in between."""
	try:
		int(str(contact_id))
	except (TypeError, ValueError):
		logger.warning("[log_meeting_outcome] refusing to open modal — non-int contact_id=%r", contact_id)
		return False
	try:
		datetime.fromisoformat(str(meeting_occurred_at))
	except (TypeError, ValueError):
		logger.warning("[log_meeting_outcome] refusing to open modal — non-ISO meeting_occurred_at=%r", meeting_occurred_at)
		return False

	await client.views_open(
		trigger_id=trigger_id,
		view={
			"type": "modal",
			"callback_id": _MEETING_OUTCOME_CALLBACK_ID,
			"private_metadata": private_metadata,
			"title": {"type": "plain_text", "text": "Log Outcome"},
			"submit": {"type": "plain_text", "text": "Save"},
			"close": {"type": "plain_text", "text": "Cancel"},
			"blocks": [
				{
					"type": "input",
					"block_id": "attendance_block",
					"label": {"type": "plain_text", "text": "What happened?"},
					"element": {
						"type": "static_select",
						"action_id": "attendance_status",
						"placeholder": {"type": "plain_text", "text": "Select an outcome"},
						"options": [
							{"text": {"type": "plain_text", "text": label}, "value": value}
							for value, label in _ATTENDANCE_OPTIONS
						],
					},
				},
				{
					"type": "input",
					"block_id": "pm_software_block",
					"optional": True,
					"label": {"type": "plain_text", "text": "PM software (if stated)"},
					"element": {"type": "plain_text_input", "action_id": "pm_software_stated"},
				},
				{
					"type": "input",
					"block_id": "door_count_block",
					"optional": True,
					"label": {"type": "plain_text", "text": "Door count (if stated)"},
					"element": {"type": "plain_text_input", "action_id": "door_count_stated"},
				},
				{
					"type": "input",
					"block_id": "objections_block",
					"optional": True,
					"label": {"type": "plain_text", "text": "Objections"},
					"element": {"type": "plain_text_input", "action_id": "objections_stated", "multiline": True},
				},
				{
					"type": "input",
					"block_id": "next_action_block",
					"optional": True,
					"label": {"type": "plain_text", "text": "Next action"},
					"element": {"type": "plain_text_input", "action_id": "next_action", "multiline": True},
				},
			],
		},
	)
	return True


@app.action("log_meeting_outcome")
async def handle_log_meeting_outcome(ack, body, respond, action, client):
	"""Deliberately NOT _load_and_verify: that prelude re-posts a generic
	work-order card via post_work_order_card() on a stale hash, which is
	wrong for this card type. Everything here is in-memory / one DB read so
	views_open lands inside Slack's ~3s trigger_id window."""
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

	# The card is bound to the assigned closer via the work order's recipient,
	# which is inside the hash preimage — so a verified-fresh card already
	# proves recipient is unchanged; this compares the CLICKER against it.
	if order.recipient != user_id:
		await respond(response_type="ephemeral", text=":no_entry: This meeting is assigned to a different closer.")
		return

	payload = order.payload or {}
	private_metadata = json.dumps(
		{
			"client_id": order.client_id,
			"action_id": order.action_id,
			"payload_hash": payload_hash.compute(order),
			"booking_id": payload.get("booking_id"),
			"contact_id": value.get("contact_id"),
			"company_id": payload.get("company_id"),
			"meeting_occurred_at": value.get("meeting_occurred_at"),
			"channel_id": order.slack_channel_id,
			"message_ts": order.slack_message_ts,
		}
	)
	await open_meeting_outcome_modal(
		client,
		trigger_id=body["trigger_id"],
		contact_id=value.get("contact_id"),
		meeting_occurred_at=value.get("meeting_occurred_at"),
		private_metadata=private_metadata,
	)


def _modal_value(view: dict, block_id: str, action_id: str):
	block = view["state"]["values"].get(block_id, {}).get(action_id, {})
	if block.get("type") == "static_select":
		selected = block.get("selected_option")
		return selected.get("value") if selected else None
	return block.get("value")


@app.view(_MEETING_OUTCOME_CALLBACK_ID)
async def handle_meeting_outcome_submit(ack, body, view):
	"""Global — receives the submission regardless of what opened the modal
	(this handler and 4.2.2's are one and the same). Re-verifies auth, hash
	and the 24h window because a view_submission is a separate inbound
	request and the modal can sit open; then one transaction writes the
	outcome and closes out the card."""
	user_id = body.get("user", {}).get("id", "")
	try:
		meta = json.loads(view.get("private_metadata", "{}"))
	except (json.JSONDecodeError, TypeError):
		await ack(response_action="errors", errors={"attendance_block": "Internal error — could not read card context. Close this and try again."})
		return

	client_id = meta.get("client_id")
	action_id = meta.get("action_id")

	if not approver_authorized(user_id, client_id=client_id):
		await ack(response_action="errors", errors={"attendance_block": "Not authorized to act on this card."})
		return

	order = wo.get(client_id, action_id) if client_id and action_id else None
	if order is None:
		await ack(response_action="errors", errors={"attendance_block": "This card is no longer available."})
		return

	verdict = payload_hash.verify(order, meta.get("payload_hash"))
	if not verdict.fresh or _meeting_outcome_expired(order, now=datetime.now(timezone.utc)):
		await ack(response_action="errors", errors={"attendance_block": "This action has expired or was altered. Nothing was saved."})
		return

	if order.status != "QUEUED":
		await ack(response_action="errors", errors={"attendance_block": f"Already logged (status={order.status})."})
		return

	if order.recipient != user_id:
		await ack(response_action="errors", errors={"attendance_block": "This meeting is assigned to a different closer."})
		return

	# Parse the door count before ack — a non-numeric entry is a field-level
	# error the modal shows in place, not a silent drop.
	door_count_raw = _modal_value(view, "door_count_block", "door_count_stated")
	door_count = None
	if door_count_raw not in (None, ""):
		try:
			door_count = int(str(door_count_raw).strip())
		except ValueError:
			await ack(response_action="errors", errors={"door_count_block": "Enter a whole number, or leave blank."})
			return

	booking_id = meta.get("booking_id")
	contact_id = int(str(meta.get("contact_id")))
	company_id = meta.get("company_id")
	try:
		meeting_occurred_at = datetime.fromisoformat(str(meta.get("meeting_occurred_at")))
	except (TypeError, ValueError):
		await ack(response_action="errors", errors={"attendance_block": "Internal error — bad meeting time on this card."})
		return

	attendance_status = _modal_value(view, "attendance_block", "attendance_status")
	pm_software = _modal_value(view, "pm_software_block", "pm_software_stated") or None
	objections = _modal_value(view, "objections_block", "objections_stated") or None
	next_action = _modal_value(view, "next_action_block", "next_action") or None

	with get_db_context(client_id=_INTERNAL_SALES_CLIENT_ID) as session:
		# A NO_SHOW may already have been logged from the #blackink-command
		# "Mark No-Show" card — the two surfaces log the same meeting, and
		# whichever fires first closes the question.
		if booking_id is not None and outcome_recorded_for_booking(session, booking_id):
			await ack(response_action="errors", errors={"attendance_block": "An outcome was already logged for this meeting."})
			return

		try:
			record_meeting_outcome(
				session,
				MeetingOutcomeRecord(
					client_id=_INTERNAL_SALES_CLIENT_ID,
					contact_id=contact_id,
					company_id=company_id,
					meeting_occurred_at=meeting_occurred_at,
					attendance_status=attendance_status,
					pm_software_stated=pm_software,
					door_count_stated=door_count,
					objections_stated=objections,
					next_action=next_action,
					submitted_by=f"slack:{user_id}",
				),
			)
		except ValueError:
			await ack(response_action="errors", errors={"attendance_block": "Invalid outcome — pick one of the listed options."})
			return

		session.execute(
			text(
				"UPDATE agent_work_orders SET status = 'DONE', decided_by = :decided_by, "
				"decided_at = NOW(), updated_at = NOW() "
				"WHERE action_id = :action_id AND client_id = :client_id AND status = 'QUEUED'"
			),
			{"decided_by": f"slack:{user_id}", "action_id": action_id, "client_id": client_id},
		)

	await ack()

	_log_event(
		client_id, "meeting_outcome_recorded", entity_id=action_id, actor=f"slack:{user_id}",
		payload={"contact_id": contact_id, "booking_id": booking_id, "attendance_status": attendance_status},
	)

	if order.slack_channel_id and order.slack_message_ts:
		await post.update_card(
			channel_id=order.slack_channel_id,
			message_ts=order.slack_message_ts,
			text=f"{_card_text(order)}\n\n*Outcome logged* ({attendance_status}) by <@{user_id}>",
		)


@app.view_closed(_MEETING_OUTCOME_CALLBACK_ID)
async def handle_meeting_outcome_closed(ack):
	await ack()
