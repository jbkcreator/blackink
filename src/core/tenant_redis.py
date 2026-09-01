"""Tenant-mandatory Redis wrappers.

client_id is a required positional argument on every function here —
structurally impossible to build/touch a key without one, which is a
stronger guarantee than a naming convention (a convention can be typo'd or
forgotten; a missing required argument raises immediately). See
config/redis_keys.py and the Dev 1 plan's "Tenant isolation chassis"
section.
"""

from typing import Optional

from config.redis_keys import client_key
from src.core.redis_client import rget, rset, rdelete, rincr, rdecr


def tget(client_id: str, *parts: str) -> Optional[str]:
	return rget(client_key(client_id, *parts))


def tset(client_id: str, *parts: str, value: str, ttl_seconds: Optional[int] = None) -> bool:
	return rset(client_key(client_id, *parts), value, ttl_seconds=ttl_seconds)


def tdelete(client_id: str, *parts: str) -> int:
	return rdelete(client_key(client_id, *parts))


def tincr(client_id: str, *parts: str, amount: int = 1, ttl_seconds: Optional[int] = None) -> int:
	return rincr(client_key(client_id, *parts), amount=amount, ttl_seconds=ttl_seconds)


def tdecr(client_id: str, *parts: str, amount: int = 1) -> int:
	return rdecr(client_key(client_id, *parts), amount=amount)
