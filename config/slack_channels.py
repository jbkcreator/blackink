"""Slack channel registry — Dev 3 plan §7.2.

A registry, not settings: config/settings.py holds the env-backed channel
IDs; this holds the fixed vocabulary (channel key -> purpose -> which
settings attribute supplies its ID), same split Forced Action uses between
config/settings.py and relay/config.py (FA/relay/config.py:1-6 states this
rationale explicitly).

Channel names are verbatim from blueprint §3.0.3:156-161 — note that two
carry NO `blackink-` prefix (#sales-replies, #dial-tasks) while the other
four do. That is correct, not a typo; do not "fix" it.

IDs are stored, not names: names can be renamed in Slack out from under
this config, and resolving names at runtime would need the channels:read
scope for no benefit (Dev 3 plan §7.3 deliberately does not request it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config.settings import get_settings


@dataclass(frozen=True)
class ChannelSpec:
	name: str  # for logging/diagnostics only — never used to address the channel
	settings_field: str  # attribute name on AppSettings holding the channel ID
	interactive: bool  # does this channel host action cards (Approve/Reject/...)?


CHANNEL_REGISTRY: dict[str, ChannelSpec] = {
	"command": ChannelSpec(
		name="#blackink-command", settings_field="blackink_command_slack_channel", interactive=True
	),
	"setter": ChannelSpec(
		name="#blackink-setter", settings_field="blackink_setter_slack_channel", interactive=True
	),
	"replies": ChannelSpec(
		name="#sales-replies", settings_field="sales_replies_slack_channel", interactive=True
	),
	"dial": ChannelSpec(
		name="#dial-tasks", settings_field="dial_tasks_slack_channel", interactive=True
	),
	"qa": ChannelSpec(
		name="#blackink-qa", settings_field="blackink_qa_slack_channel", interactive=False
	),
	"economics": ChannelSpec(
		name="#blackink-economics", settings_field="blackink_economics_slack_channel", interactive=False
	),
}


def resolve_channel_id(channel_key: str) -> Optional[str]:
	"""Registry key -> configured Slack channel ID, or None if unconfigured.
	Callers no-op on None rather than raise (FA/relay/slack_post.py:44-56
	pattern) — keeps local/dev usable without every channel wired up."""
	spec = CHANNEL_REGISTRY.get(channel_key)
	if spec is None:
		return None
	return getattr(get_settings(), spec.settings_field, None)
