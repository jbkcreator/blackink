"""Lazy-singleton Redis client primitives.

Mirrors the shape of Forced Action's src/core/redis_client.py (rget/rset/
rincr/rdelete as thin wrappers over a lazily-created client) but these are
NOT tenant-safe on their own — nothing here prevents a caller from building
an unprefixed key. Tenant-scoped code must go through
src/core/tenant_redis.py's tset/tget/tincr instead, which force a client_id
prefix via config/redis_keys.client_key(). A CI grep-lint flags direct calls
to these raw primitives from anywhere outside tenant_redis.py.
"""

from typing import Optional

import redis

from config.settings import get_settings

_client: Optional[redis.Redis] = None


def get_redis_client() -> redis.Redis:
	global _client
	if _client is None:
		settings = get_settings()
		if not settings.redis_url:
			raise RuntimeError("REDIS_URL is not configured")
		_client = redis.from_url(settings.redis_url, decode_responses=True)
	return _client


def rget(key: str) -> Optional[str]:
	return get_redis_client().get(key)


def rset(key: str, value: str, ttl_seconds: Optional[int] = None) -> bool:
	return bool(get_redis_client().set(key, value, ex=ttl_seconds))


def rdelete(key: str) -> int:
	return get_redis_client().delete(key)


def rincr(key: str, amount: int = 1, ttl_seconds: Optional[int] = None) -> int:
	"""Atomic increment. If this is the first increment (result == amount),
	also set an expiry — mirrors the daily-ceiling pattern Relay uses."""
	client = get_redis_client()
	value = client.incrby(key, amount)
	if value == amount and ttl_seconds is not None:
		client.expire(key, ttl_seconds)
	return value


def rdecr(key: str, amount: int = 1) -> int:
	return get_redis_client().decrby(key, amount)
