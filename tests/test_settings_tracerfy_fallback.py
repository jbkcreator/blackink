"""Tests for config.settings.AppSettings.tracerfy_api_key's fallback to the
deprecated DNC_VENDOR_API_KEY env var name (PR review finding: a hard
rename with no fallback would silently disable DNC/skip-trace processing
in any deployment this codebase doesn't control that still only sets the
old name).

Also the regression test for the bug this fallback's own implementation
surfaced: Field(env=...) is a pydantic-v1-only kwarg that pydantic-settings
2.x silently ignores (falls back to matching the env var by the Python
field name itself) -- validation_alias is the real mechanism. Every other
env=... field in config/settings.py happens to have a field name that
matches its env var 1:1 case-insensitively, so this was never caught
until a field needed a name genuinely different from its env var."""

import config.settings as s


def _settings(monkeypatch, tracerfy=None, legacy=None):
	if tracerfy is None:
		monkeypatch.delenv("TRACERFY_API_KEY", raising=False)
	else:
		monkeypatch.setenv("TRACERFY_API_KEY", tracerfy)
	if legacy is None:
		monkeypatch.delenv("DNC_VENDOR_API_KEY", raising=False)
	else:
		monkeypatch.setenv("DNC_VENDOR_API_KEY", legacy)
	# Isolate from whatever this environment's real .env file happens to
	# contain -- explicit os.environ (set above by monkeypatch) already
	# takes priority over the dotenv file per pydantic-settings' own
	# precedence, but skip the file entirely for a clean, deterministic test.
	return s.AppSettings(_env_file=None)


def test_prefers_new_name_when_only_new_name_set(monkeypatch):
	settings = _settings(monkeypatch, tracerfy="new-value")
	assert settings.tracerfy_api_key.get_secret_value() == "new-value"


def test_falls_back_to_legacy_name_when_only_legacy_set(monkeypatch):
	settings = _settings(monkeypatch, legacy="legacy-value")
	assert settings.tracerfy_api_key.get_secret_value() == "legacy-value"


def test_prefers_new_name_when_both_are_set(monkeypatch):
	settings = _settings(monkeypatch, tracerfy="new-value", legacy="legacy-value")
	assert settings.tracerfy_api_key.get_secret_value() == "new-value"


def test_none_when_neither_is_set(monkeypatch):
	settings = _settings(monkeypatch)
	assert settings.tracerfy_api_key is None


def test_validation_alias_not_env_kwarg_regression():
	"""Field(env=...) is silently ignored by pydantic-settings 2.x -- this
	would pass even if someone "simplified" the fallback fields back to
	env=... by accident, UNLESS the field's Python name differs from its
	env var name, which is exactly the case here. Asserting the field
	definition itself uses validation_alias, not env, catches that
	regression directly rather than relying on the env-var tests above to
	fail in a way that points at the right cause."""
	field = s.AppSettings.model_fields["tracerfy_api_key_current"]
	assert field.validation_alias == "TRACERFY_API_KEY"
	field = s.AppSettings.model_fields["dnc_vendor_api_key_legacy"]
	assert field.validation_alias == "DNC_VENDOR_API_KEY"
