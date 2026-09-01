"""Single source of truth for tenant-scoped Redis key construction.

client_id is a required positional argument — it is structurally impossible
to build a key without one, which is a stronger guarantee than a naming
convention (a convention can be typo'd or forgotten; a missing required
argument raises immediately). See plan doc §Redis key convention and
docs/adr/0001-tenant-isolation-rls-plus-app-layer.md.
"""


def client_key(client_id: str, *parts: str) -> str:
	"""Build a tenant-namespaced Redis key: '{client_id}:{part1}:{part2}:...'."""
	if not client_id:
		raise ValueError("client_key() requires a non-empty client_id")
	if not parts:
		raise ValueError("client_key() requires at least one key part")
	return ":".join([client_id, *(str(p) for p in parts)])
