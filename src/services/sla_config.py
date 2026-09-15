"""Resolve a client's SLA windows, override-or-platform-default.

The Reply-Triage first-response / escalation timers and the Speed-to-Lead
first-response window are configurable per client via nullable
`clients.sla_*` / `clients.speed_to_lead_sla_minutes` columns
(`migrations/apply_client_sla_windows.py`). A NULL column means "no
override" and resolves to the matching platform default in
`config/settings.py`. This module is the single place that resolution
happens, so no caller reimplements the COALESCE-with-default rule.

The batch escalation sweep (`src/tasks/respond_sla_sweep.py`) does the same
resolution in SQL (`COALESCE(c.sla_tier2_minutes, :default)`), joining
`clients` per row — the defaults bound there come from the same settings
fields, so the two paths never disagree.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

from config.settings import get_settings


@dataclass(frozen=True)
class ClientSlaWindows:
	"""Resolved SLA windows for one client, in minutes."""

	hot_lead_minutes: int
	standard_minutes: int
	tier2_minutes: int
	tier3_minutes: int
	speed_to_lead_minutes: int


def resolve_sla_windows(db: Any, client_id: Optional[str]) -> ClientSlaWindows:
	"""Return the effective SLA windows for `client_id`.

	Each field is the client's own override when set, else the platform
	default. An unknown/NULL `client_id` (e.g. an unrouted message with no
	tenant yet) resolves entirely to platform defaults rather than raising —
	the SLA still needs a concrete window to compute a due time.
	"""
	settings = get_settings()
	row = None
	if client_id:
		row = db.execute(
			text(
				"SELECT sla_hot_lead_minutes, sla_standard_minutes, "
				"       sla_tier2_minutes, sla_tier3_minutes, "
				"       speed_to_lead_sla_minutes "
				"FROM clients WHERE client_id = :cid"
			),
			{"cid": client_id},
		).mappings().first()

	def pick(column: str, default: int) -> int:
		value = row[column] if row and row[column] is not None else None
		return int(value) if value is not None else default

	return ClientSlaWindows(
		hot_lead_minutes=pick("sla_hot_lead_minutes", settings.respond_sla_hot_lead_minutes),
		standard_minutes=pick("sla_standard_minutes", settings.respond_sla_standard_minutes),
		tier2_minutes=pick("sla_tier2_minutes", settings.respond_sla_tier2_minutes),
		tier3_minutes=pick("sla_tier3_minutes", settings.respond_sla_tier3_minutes),
		speed_to_lead_minutes=pick("speed_to_lead_sla_minutes", settings.speed_to_lead_sla_minutes),
	)
