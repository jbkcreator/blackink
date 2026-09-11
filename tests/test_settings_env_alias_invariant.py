"""Structural proof for Group D / D-5 — env= is inert under pydantic v2.

config/settings.py uses `Field(..., env="X")` throughout, which pydantic v2
accepts only as inert extra metadata (a deprecation warning, nothing more —
it does NOT set the environment variable name). Every field in this file
currently works only because its declared env= name happens to equal its
Python field name uppercased; the moment a future field's name and its
intended env var diverge, it would silently read nothing, with no error.

Rather than rewrite all ~100 fields to validation_alias (unwarranted churn
for something that isn't broken today — see the Group D remediation plan),
this converts the invariant that currently holds by accident into one CI
enforces: every field must EITHER already use validation_alias (the correct
mechanism) OR have its `env=` value equal to its field name uppercased. A
future PR that adds a field breaking this must fail here, not read silently
from nothing in production.

Same structural-test technique as tests/test_billing_structural.py and
tests/test_no_upfront_charge_paths.py.
"""
from config.settings import AppSettings


def test_every_field_env_name_matches_or_uses_validation_alias():
    offenders = []
    for name, field in AppSettings.model_fields.items():
        if field.validation_alias:
            # The correct mechanism under pydantic v2 — trusted as-is.
            continue
        extra = field.json_schema_extra
        env_name = extra.get("env") if isinstance(extra, dict) else None
        if env_name is None:
            # No env= declared at all — not this invariant's concern.
            continue
        if env_name != name.upper():
            offenders.append(f"{name} declares env={env_name!r}, which env= cannot "
                              f"actually honor under pydantic v2 (it silently reads "
                              f"{name.upper()!r} instead) — use validation_alias={env_name!r}")
    assert offenders == [], (
        "field(s) whose env= name would silently be ignored under pydantic v2:\n  - "
        + "\n  - ".join(offenders)
    )
