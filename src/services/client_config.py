"""Deny-by-default tenant config resolution.

Structurally identical shape to Forced Action's src/utils/venture_config.py
(frozen dataclass, in-process cache, invalidate_cache()), inverted at the
two decision points that made FA's version wrong for a compliance-sensitive
multi-tenant product: a DB error or a missing row there falls back to
shared global settings; here, both resolve to the same single denied state.
There is no shared identity for an unknown/inactive client to inherit from.

See the Dev 1 plan's "get_client_config(client_id)" section.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300


@dataclass(frozen=True)
class ClientConfig:
	client_id: str
	is_active: bool
	display_name: str = ""
	plan_tier: str = ""
	daily_send_ceiling: int = 0
	contract_start_date: Optional[date] = None
	contract_end_date: Optional[date] = None


_DENIED_TEMPLATE = ClientConfig(client_id="", is_active=False)

# (client_id) -> (ClientConfig, cached_at_monotonic)
_cache: dict[str, Tuple[ClientConfig, float]] = {}


def _denied(client_id: str) -> ClientConfig:
	return ClientConfig(client_id=client_id, is_active=False)


def _load_from_db(session: Session, client_id: str) -> ClientConfig:
	try:
		row = session.execute(
			text(
				"SELECT client_id, display_name, is_active, plan_tier, "
				"daily_send_ceiling, contract_start_date, contract_end_date, suspended_at "
				"FROM clients WHERE client_id = :client_id"
			),
			{"client_id": client_id},
		).fetchone()
	except Exception:
		# DB error resolving this client — deny, never fall back to a shared
		# default. There is no safe "assume active" behavior here.
		logger.error("[client_config] DB error resolving %r — denying access, no fallback", client_id, exc_info=True)
		return _denied(client_id)

	if row is None:
		# Unknown client = no access. Not "synthesize from env globals."
		return _denied(client_id)

	if not row.is_active or row.suspended_at is not None:
		return _denied(client_id)

	return ClientConfig(
		client_id=row.client_id,
		is_active=True,
		display_name=row.display_name,
		plan_tier=row.plan_tier,
		daily_send_ceiling=row.daily_send_ceiling,
		contract_start_date=row.contract_start_date,
		contract_end_date=row.contract_end_date,
	)


def get_client_config(session: Session, client_id: str) -> ClientConfig:
	"""Resolve a client's config, deny-by-default, with a short in-process
	cache. Callers that need an immediate deny after a suspend action must
	call invalidate_cache(client_id) synchronously in the same
	request/transaction as the suspend — a suspended client's ability to
	send should stop in seconds, not up to the cache TTL later."""
	if not client_id:
		return _denied("")

	cached = _cache.get(client_id)
	if cached is not None:
		config, cached_at = cached
		if time.monotonic() - cached_at < _CACHE_TTL_SECONDS:
			return config

	config = _load_from_db(session, client_id)
	_cache[client_id] = (config, time.monotonic())
	return config


def invalidate_cache(client_id: Optional[str] = None) -> None:
	"""Invalidate one client's cached config, or all of them if client_id is None."""
	if client_id is None:
		_cache.clear()
	else:
		_cache.pop(client_id, None)
