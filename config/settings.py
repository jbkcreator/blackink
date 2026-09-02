"""Application configuration powered by Pydantic settings.

Mirrors Forced Action's config/settings.py convention (single AppSettings,
env_file=".env", Field(..., env="...") per var, @lru_cache singleton) —
see C:\\Users\\HEU-Vishnu\\Forced-action-\\config\\settings.py.

Which file gets loaded is controlled by the ENV_FILE shell environment
variable (not itself read from any .env file — set it before running a
command), defaulting to ".env". Local Docker-Postgres testing should use a
permanent, gitignored ".env.local" instead of overwriting the real ".env" —
see CLAUDE.md's "Local development database" section:

    $env:ENV_FILE=".env.local"
    python migrations/apply_db_roles.py
"""

import os
from functools import lru_cache
from typing import Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
	"""Central place for environment-driven configuration."""

	model_config = SettingsConfigDict(
		env_file=os.environ.get("ENV_FILE", ".env"),
		env_file_encoding="utf-8",
		case_sensitive=False,
		extra="ignore",
	)

	debug: bool = Field(default=True, env="DEBUG")
	app_base_url: str = Field(default="http://localhost:8000", env="APP_BASE_URL")

	# ── Database ─────────────────────────────────────────────────────────────
	# Generic fallback DSN (used by tooling/tests that don't care which role).
	database_url: str = Field(
		default="postgresql://user:password@localhost:5432/blackink",
		env="DATABASE_URL",
		description="Fallback PostgreSQL connection string",
	)
	# App-role DSN — MUST be the blackink_app role (RLS-subject, no BYPASSRLS).
	# See docs/adr/0001-tenant-isolation-rls-plus-app-layer.md.
	database_url_app: Optional[str] = Field(default=None, env="DATABASE_URL_APP")
	# System-role DSN (BYPASSRLS) — internal batch jobs only. Never imported
	# from src/api/ — enforced by a CI grep-lint, not just this comment.
	database_url_system: Optional[str] = Field(default=None, env="DATABASE_URL_SYSTEM")
	# Akrash's restricted DSN — INSERT-only on the two raw_prospect_* tables.
	database_url_akrash: Optional[str] = Field(default=None, env="DATABASE_URL_AKRASH")
	# Passwords for the three Postgres roles created by migrations/apply_db_roles.py.
	# Only read by that migration at provisioning time, not used at runtime —
	# runtime auth for each role lives in its own DSN field above.
	blackink_app_db_password: Optional[SecretStr] = Field(default=None, env="BLACKINK_APP_DB_PASSWORD")
	blackink_system_db_password: Optional[SecretStr] = Field(default=None, env="BLACKINK_SYSTEM_DB_PASSWORD")
	akrash_ingest_db_password: Optional[SecretStr] = Field(default=None, env="AKRASH_INGEST_DB_PASSWORD")
	db_echo: bool = Field(default=False, env="DB_ECHO")
	db_pool_size: int = Field(default=5, env="DB_POOL_SIZE")
	db_max_overflow: int = Field(default=10, env="DB_MAX_OVERFLOW")

	redis_url: Optional[str] = Field(default=None, env="REDIS_URL")

	# ── Compliance gate ──────────────────────────────────────────────────────
	dnc_vendor_api_key: Optional[SecretStr] = Field(default=None, env="DNC_VENDOR_API_KEY")
	# Max age before a cached dnc_clean value is treated as ABSTAIN (stale), not trusted.
	dnc_recheck_days: int = Field(default=30, env="DNC_RECHECK_DAYS")
	email_verification_vendor_api_key: Optional[SecretStr] = Field(
		default=None, env="EMAIL_VERIFICATION_VENDOR_API_KEY"
	)
	# Non-poach lock (client_pm_books-based) is permanent per design decision —
	# this flag exists only as an emergency override switch, default must stay True.
	non_poach_lock_permanent: bool = Field(default=True, env="NON_POACH_LOCK_PERMANENT")
	# County-allocation prospect-pool reassessment window (separate mechanism
	# from the permanent non-poach lock above — see plan doc §Key decision 2).
	county_allocation_reassess_days: int = Field(default=30, env="COUNTY_ALLOCATION_REASSESS_DAYS")

	# ── Instantly (deliverability) ───────────────────────────────────────────
	instantly_api_key: Optional[SecretStr] = Field(default=None, env="INSTANTLY_API_KEY")
	instantly_base_url: str = Field(default="https://api.instantly.ai", env="INSTANTLY_BASE_URL")
	instantly_enabled: bool = Field(default=False, env="INSTANTLY_ENABLED")

	# Deliverability Sentinel thresholds — bounce/complaint rate over a rolling 48h window.
	deliverability_bounce_rate_threshold_pct: float = Field(
		default=3.0, env="DELIVERABILITY_BOUNCE_RATE_THRESHOLD_PCT"
	)
	deliverability_spam_complaint_threshold_pct: float = Field(
		default=0.08, env="DELIVERABILITY_SPAM_COMPLAINT_THRESHOLD_PCT"
	)

	# ── Slack ────────────────────────────────────────────────────────────────
	slack_bot_token: Optional[SecretStr] = Field(default=None, env="SLACK_BOT_TOKEN")
	slack_signing_secret: Optional[SecretStr] = Field(default=None, env="SLACK_SIGNING_SECRET")
	blackink_qa_slack_channel: Optional[str] = Field(default=None, env="BLACKINK_QA_SLACK_CHANNEL")

	# ── Akrash ingestion ─────────────────────────────────────────────────────
	akrash_ingest_jwt_secret: Optional[SecretStr] = Field(default=None, env="AKRASH_INGEST_JWT_SECRET")


@lru_cache
def get_settings() -> AppSettings:
	"""Load and cache settings so expensive validation runs once."""
	return AppSettings()


settings = get_settings()
