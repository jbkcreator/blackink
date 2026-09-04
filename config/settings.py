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
from typing import Optional, Tuple

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
	# App-Level Token (xapp-...) — Socket Mode connection auth, distinct from
	# the bot token above. Week 0 runs Socket Mode (no public HTTPS endpoint
	# yet for DNS/TLS reasons); switching back to HTTP webhooks later drops
	# this field's use but doesn't require removing it.
	slack_app_token: Optional[SecretStr] = Field(default=None, env="SLACK_APP_TOKEN")

	# ── Relay halt / resume (src/agents/relay/) ─────────────────────────────
	# HMAC-SHA256 signing key for cryptographic resume tokens
	# (src/agents/relay/resume_auth.py). Must be set before any halt can be
	# issued or resumed. Recommended: 32+ bytes of entropy.
	relay_resume_secret: Optional[SecretStr] = Field(default=None, env="RELAY_RESUME_SECRET")

	blackink_qa_slack_channel: Optional[str] = Field(default=None, env="BLACKINK_QA_SLACK_CHANNEL")
	blackink_command_slack_channel: Optional[str] = Field(default=None, env="BLACKINK_COMMAND_SLACK_CHANNEL")
	blackink_setter_slack_channel: Optional[str] = Field(default=None, env="BLACKINK_SETTER_SLACK_CHANNEL")
	sales_replies_slack_channel: Optional[str] = Field(default=None, env="SALES_REPLIES_SLACK_CHANNEL")
	dial_tasks_slack_channel: Optional[str] = Field(default=None, env="DIAL_TASKS_SLACK_CHANNEL")
	blackink_economics_slack_channel: Optional[str] = Field(default=None, env="BLACKINK_ECONOMICS_SLACK_CHANNEL")
	# Fail-closed workspace-wide approver allowlist — Slack user IDs,
	# comma-separated (e.g. "U012ABC,U034DEF"). src.services.slack.auth.
	# approver_authorized() treats an empty/unset list as "nobody
	# authorized", never "everybody". Per-tenant
	# approvers are Week 1 (need a clients column that doesn't exist yet);
	# this is global-only for Week 0.
	#
	# Typed str, NOT Tuple[str, ...], deliberately: pydantic-settings'
	# EnvSettingsSource treats any list/tuple/set-typed field as "complex"
	# and unconditionally json.loads()s its raw env value BEFORE any
	# field_validator runs — a plain comma-separated string (the natural
	# .env format, and what this field's own name/docstring implies) is
	# not valid JSON, so declaring this as Tuple[str, ...] crashes the
	# entire app at import time (settings = get_settings() runs at module
	# load) the moment BLACKINK_GLOBAL_APPROVERS is set to anything but a
	# JSON array. Confirmed by reproducing the SettingsError directly
	# against this pydantic-settings version before landing this fix —
	# a field_validator(mode="before") on the tuple field does NOT
	# intercept it, since the crash happens in the settings SOURCE, before
	# pydantic's own validation pipeline ever sees the value.
	#
	# validation_alias, NOT env= : every other field in this file uses
	# Field(..., env="X") to name its env var, but pydantic v2 does not
	# support that kwarg — it is silently accepted as inert "extra"
	# metadata (Pyright flags this file-wide as PydanticDeprecatedSince20,
	# "Extra keys: 'env'"). Every pre-existing field here only reads its
	# intended env var by ACCIDENT: pydantic-settings' default behavior is
	# to derive the env var name from the FIELD NAME itself
	# (case-insensitive, per this model's case_sensitive=False), and every
	# existing `env="..."` value happens to equal its own field name
	# uppercased. That coincidence breaks the moment a field's Python name
	# differs from its env var name — exactly this field, since
	# `blackink_global_approvers_raw` must read `BLACKINK_GLOBAL_APPROVERS`
	# (no `_raw` suffix) to stay the name callers actually set. Confirmed
	# empirically: env= silently does nothing here; validation_alias does.
	# Worth a file-wide fix (every field's binding is one rename away from
	# breaking the same way), flagged separately — out of scope to change
	# every other field in this pass.
	blackink_global_approvers_raw: str = Field(default="", validation_alias="BLACKINK_GLOBAL_APPROVERS")

	@property
	def blackink_global_approvers(self) -> Tuple[str, ...]:
		return tuple(v.strip() for v in self.blackink_global_approvers_raw.split(",") if v.strip())

	# ── Relay halt / resume ──────────────────────────────────────────────────
	# HMAC-SHA256 signing key for cryptographic resume tokens. Must be set
	# before any halt can be issued or resumed. Recommended: 32+ bytes of entropy.
	relay_resume_secret: Optional[SecretStr] = Field(default=None, env="RELAY_RESUME_SECRET")

	# ── Akrash ingestion ─────────────────────────────────────────────────────
	akrash_ingest_jwt_secret: Optional[SecretStr] = Field(default=None, env="AKRASH_INGEST_JWT_SECRET")

	# ── Oxylabs residential proxy ────────────────────────────────────────────
	oxylabs_username: Optional[str] = Field(default=None, env="OXYLABS_USERNAME")
	oxylabs_password: Optional[SecretStr] = Field(default=None, env="OXYLABS_PASSWORD")

	# ── Owner Visibility Score ───────────────────────────────────────────────
	# When absent the stub provider is used — max achievable score is 42/100
	# (38 website + 4 DBPR). Set to enable live Google Places API calls.
	google_places_api_key: Optional[SecretStr] = Field(default=None, env="GOOGLE_PLACES_API_KEY")

	# ── Calendar OAuth (Subtask 3.2.1 — Inbound Booking Engine) ─────────────
	# No Calendly per client comment W1-8 (Blackink_Source_of_Truth.md line
	# 577) — Google Calendar + Microsoft Graph only. Unlike the DNC/SMS/
	# RentCast vendors, no third party here is genuinely absent — these are
	# OAuth apps this project registers itself; unset means "not registered
	# yet", a required completion gate, not a permanently-deferred provider.
	google_oauth_client_id: Optional[str] = Field(default=None, env="GOOGLE_OAUTH_CLIENT_ID")
	google_oauth_client_secret: Optional[SecretStr] = Field(default=None, env="GOOGLE_OAUTH_CLIENT_SECRET")
	microsoft_oauth_client_id: Optional[str] = Field(default=None, env="MICROSOFT_OAUTH_CLIENT_ID")
	microsoft_oauth_client_secret: Optional[SecretStr] = Field(default=None, env="MICROSOFT_OAUTH_CLIENT_SECRET")
	# Fernet key (urlsafe base64, 32 bytes) — encrypts OAuth tokens and SMTP
	# passwords at rest. See src/core/token_crypto.py.
	token_encryption_key: Optional[SecretStr] = Field(default=None, env="TOKEN_ENCRYPTION_KEY")
	# Signs connect-link and OAuth `state` tokens (src/services/calendar_oauth.py).
	# Deliberately its own secret, not a reuse of akrash_ingest_jwt_secret —
	# these two token families protect unrelated systems and must be able to
	# rotate independently.
	calendar_oauth_state_secret: Optional[SecretStr] = Field(default=None, env="CALENDAR_OAUTH_STATE_SECRET")
	calendar_webhook_base_url: str = Field(
		default="http://localhost:8000", env="CALENDAR_WEBHOOK_BASE_URL",
		description="Public base URL the providers POST notifications to — must be internet-reachable in prod.",
	)
	# Local-testing-only escape hatch: Google/Microsoft's watch()/subscription
	# registration calls reject a non-public, non-domain-verified callback
	# URL (localhost) at registration time — this lets the OAuth callback
	# complete anyway (real token exchange + real baseline sync still run),
	# just without a live push subscription. Never set True outside local
	# dev — a connection created this way never receives real-time webhook
	# notifications, only whatever calendar_sync_worker's periodic safety
	# sweep picks up.
	skip_calendar_watch_registration: bool = Field(default=False, env="SKIP_CALENDAR_WATCH_REGISTRATION")

	# ── Booking confirmation email (Subtask 3.2.1) ──────────────────────────
	# Default False: per explicit instruction, a missing/disabled real
	# email provider must be a visible launch blocker (booking_confirmation_
	# blocked event), never a silent no-op or a stub quietly satisfying a test.
	email_sending_enabled: bool = Field(default=False, env="EMAIL_SENDING_ENABLED")

	# ── OVS PDF fetch (Subtask 3.2.2) ────────────────────────────────────────
	# Comma-separated exact hostnames the pre-demo reminder is allowed to
	# fetch contacts.ovs_pdf_url from (e.g. an S3/GCS bucket's public host,
	# once Dev 2's storage step exists — see src/services/show_rate_reminders.py's
	# _fetch_ovs_pdf()). Default empty means fail-closed: nothing is an
	# approved host until this is explicitly configured, same posture as
	# email_sending_enabled defaulting False — a missing/misconfigured value
	# is a visible BLOCKED reminder job, never a silent fetch-anything.
	ovs_pdf_allowed_hosts_raw: str = Field(default="", validation_alias="OVS_PDF_ALLOWED_HOSTS")

	@property
	def ovs_pdf_allowed_hosts(self) -> Tuple[str, ...]:
		return tuple(v.strip().lower() for v in self.ovs_pdf_allowed_hosts_raw.split(",") if v.strip())

	# ── No-Show Handler / Self-Serve Landing Page (Subtask 3.2.3) ───────────
	# Same fail-closed posture as ovs_pdf_allowed_hosts: empty means no host
	# is approved, so resolve_booking_link() returns None (no redirect)
	# rather than trusting an unvetted stored URL.
	booking_redirect_allowed_hosts_raw: str = Field(default="", validation_alias="BOOKING_REDIRECT_ALLOWED_HOSTS")

	@property
	def booking_redirect_allowed_hosts(self) -> Tuple[str, ...]:
		return tuple(v.strip().lower() for v in self.booking_redirect_allowed_hosts_raw.split(",") if v.strip())

	# Optional — the /audit landing page renders no pixel <script> at all
	# when unset (see src/api/public_landing_router.py), never a broken tag.
	meta_pixel_id: Optional[str] = Field(default=None, env="META_PIXEL_ID")
	google_tag_id: Optional[str] = Field(default=None, env="GOOGLE_TAG_ID")
	self_serve_rate_limit_per_10min: int = Field(default=5, env="SELF_SERVE_RATE_LIMIT_PER_10MIN")
	# Deliberately its own secret, not a reuse of relay_resume_secret — same
	# rationale as calendar_oauth_state_secret above: unrelated token
	# families must be able to rotate independently.
	no_show_token_secret: Optional[SecretStr] = Field(default=None, env="NO_SHOW_TOKEN_SECRET")


@lru_cache
def get_settings() -> AppSettings:
	"""Load and cache settings so expensive validation runs once."""
	return AppSettings()


settings = get_settings()
