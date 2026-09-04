"""Outbound Slack Web API calls — post/update cards, open modals.

Kept out of the router/listener module deliberately: this is called both
from the interactive-click path (src/services/slack/listeners.py) and from
non-interactive background work (a background job posting a work-order
card has no HTTP/Bolt request context at all). Using AsyncApp.client
(an AsyncWebClient) rather than constructing a separate client keeps a
single token/session across every call site.

No-ops (logs and returns None) when Slack isn't configured — mirrors
FA/src/services/relay/slack_post.py:44-56's reasoning: keeps local/dev and
the test suite usable without a live Slack app.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from slack_sdk.errors import SlackApiError

from config.settings import get_settings
from config.slack_channels import resolve_channel_id
from src.services.slack.bolt_app import SlackNotConfiguredError, get_bolt_app

logger = logging.getLogger(__name__)


def _client_or_none():
	"""Returns the shared AsyncWebClient, or None if Slack isn't
	configured. Deliberately checks settings directly rather than
	try/except around get_bolt_app() — constructing the AsyncApp has
	other side effects (see bolt_app.get_bolt_app's docstring) that
	callers here shouldn't trigger just to discover it's unconfigured."""
	settings = get_settings()
	if not settings.slack_bot_token or not settings.slack_signing_secret:
		return None
	try:
		return get_bolt_app().client
	except SlackNotConfiguredError:
		return None


async def post_action_card(
	*,
	channel_key: str,
	text: str,
	blocks: Sequence[dict],
) -> Optional[dict]:
	"""Posts an interactive card. Returns {"channel_id": ..., "message_ts": ...}
	on success, None if Slack/the channel is unconfigured or the post fails.

	Callers are responsible for persisting the returned channel_id/message_ts
	(e.g. work_orders.set_slack_message) — this function does not know about
	work orders or any other domain concept, only how to post a message.
	"""
	client = _client_or_none()
	if client is None:
		logger.info("[slack.post] Slack not configured — card not posted (channel_key=%s)", channel_key)
		return None

	channel_id = resolve_channel_id(channel_key)
	if not channel_id:
		logger.warning("[slack.post] channel_key=%r has no configured channel ID — card not posted", channel_key)
		return None

	try:
		response = await client.chat_postMessage(channel=channel_id, text=text, blocks=list(blocks))
	except SlackApiError as exc:
		logger.error("[slack.post] chat.postMessage failed (channel_key=%s): %s", channel_key, exc.response.get("error") if exc.response else exc)
		return None

	return {"channel_id": channel_id, "message_ts": response["ts"]}


async def update_card(*, channel_id: str, message_ts: str, text: str, blocks: Optional[Sequence[dict]] = None) -> bool:
	"""Replaces a previously-posted card's content in place — used to strip
	buttons and show the decision outcome, or to mark a card superseded
	after a payload rotation (Dev 3 plan §5.6). Never raises: a failed
	edit must not abort whatever business-logic transition triggered it."""
	client = _client_or_none()
	if client is None:
		logger.info("[slack.post] Slack not configured — card not updated (channel_id=%s)", channel_id)
		return False

	try:
		await client.chat_update(
			channel=channel_id,
			ts=message_ts,
			text=text,
			blocks=list(blocks) if blocks is not None else None,
		)
		return True
	except SlackApiError as exc:
		logger.error(
			"[slack.post] chat.update failed (channel_id=%s, ts=%s): %s",
			channel_id, message_ts, exc.response.get("error") if exc.response else exc,
		)
		return False


async def open_modal(*, trigger_id: str, view: dict) -> bool:
	"""Opens a modal in response to a block_actions click. trigger_id is
	single-use and expires ~3s after the triggering interaction, per
	Slack's own constraint — callers must call this immediately, not after
	any await that could cross that window."""
	client = _client_or_none()
	if client is None:
		logger.info("[slack.post] Slack not configured — modal not opened")
		return False

	try:
		await client.views_open(trigger_id=trigger_id, view=view)
		return True
	except SlackApiError as exc:
		logger.error("[slack.post] views.open failed: %s", exc.response.get("error") if exc.response else exc)
		return False


async def post_notice(
	*,
	channel_key: str,
	text: str,
	blocks: Optional[Sequence[dict]] = None,
	thread_ts: Optional[str] = None,
) -> Optional[str]:
	"""Non-interactive, fire-and-forget posts — #blackink-qa health alerts,
	#blackink-economics rollups. Returns the message ts on success, else
	None. Never raises.

	thread_ts (optional) replies in an existing message's thread instead of
	posting to the channel top level — used by the "Log Outcome" card's
	4-hour unclicked reminder ping, which the addendum to Subtask 3.2.1
	requires land "in the same channel/thread" as the card it's nudging.
	Omitted (None) keeps the historical top-level behavior for every
	existing caller."""
	client = _client_or_none()
	if client is None:
		logger.info("[slack.post] Slack not configured — notice not posted (channel_key=%s)", channel_key)
		return None

	channel_id = resolve_channel_id(channel_key)
	if not channel_id:
		logger.warning("[slack.post] channel_key=%r has no configured channel ID — notice not posted", channel_key)
		return None

	try:
		response = await client.chat_postMessage(
			channel=channel_id,
			text=text,
			blocks=list(blocks) if blocks else None,
			thread_ts=thread_ts,
		)
		return response["ts"]
	except SlackApiError as exc:
		logger.error("[slack.post] chat.postMessage (notice) failed (channel_key=%s): %s", channel_key, exc.response.get("error") if exc.response else exc)
		return None
