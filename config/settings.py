"""Application configuration powered by Pydantic settings.

Single AppSettings class, env_file=".env", Field(..., env="...") per var,
@lru_cache singleton. Which file gets loaded is controlled by the ENV_FILE
shell environment variable (not itself read from any .env file — set it
before running a command), defaulting to ".env".

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
	# Deployment environment. Anything other than "development"/"test" is treated
	# as production for fail-closed guards (e.g. the email sender must not fall
	# back to the transmit-nothing stub in production). Default is development so
	# local dev and CI stay convenient; production must set ENVIRONMENT=production.
	environment: str = Field(default="development", env="ENVIRONMENT")
	app_base_url: str = Field(default="http://localhost:8000", env="APP_BASE_URL")

	@property
	def is_production(self) -> bool:
		return (self.environment or "development").strip().lower() not in {"development", "dev", "test", "testing", "local"}

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
	# Tracerfy account key — serves BOTH the DNC scrub (src/tasks/dnc_refresh.py,
	# src/services/winback_ingest.py) and skip-trace owner enrichment
	# (src/services/owner_enrichment.py, Subtask 3.2.1) — one Tracerfy account,
	# both products (client decision 2026-09-08). Renamed from
	# DNC_VENDOR_API_KEY, which described only the first use; kept as one
	# credential rather than two so there is exactly one place to rotate it.
	# No fallback to the old env var name — this is the only deployment of
	# this codebase, so there is nothing else to keep working (a fallback
	# was added and then deliberately removed once that was confirmed).
	tracerfy_api_key: Optional[SecretStr] = Field(default=None, env="TRACERFY_API_KEY")
	# Max age before a cached dnc_clean value is treated as ABSTAIN (stale), not trusted.
	dnc_recheck_days: int = Field(default=30, ge=1, le=31, env="DNC_RECHECK_DAYS")
	email_verification_vendor_api_key: Optional[SecretStr] = Field(
		default=None, env="EMAIL_VERIFICATION_VENDOR_API_KEY"
	)
	# ── Owner enrichment (Subtask 3.2.1) ────────────────────────────────────
	# Hard bound on per-run vendor spend — src/tasks/enrichment_verification.py's
	# claim query LIMITs to this by default.
	owner_enrichment_max_per_run: int = Field(default=500, env="OWNER_ENRICHMENT_MAX_PER_RUN")
	# A row whose enrichment never gets an answer (vendor outage, poll timeout)
	# is retried up to this many sweep runs before the self-heal step
	# terminally marks it requires_enrichment_review=TRUE — same bounded-retry
	# idiom as src/tasks/self_serve_audit_worker.py's own attempt cap.
	owner_enrichment_max_attempts: int = Field(default=3, env="OWNER_ENRICHMENT_MAX_ATTEMPTS")
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

	# ── Outbound email (SMTP per warmed mailbox — wayfinder ticket 05) ────────
	# When smtp_host + smtp_password are set, build_email_sender() returns a real
	# SmtpEmailSender; otherwise it falls back to the StubEmailSender (mints a
	# Message-ID, transmits nothing). Per blueprint §475/§853 mailboxes are
	# warmed Google Workspace / Outlook (smtp.gmail.com:587 / smtp.office365.com:587).
	# smtp_username defaults to the sending mailbox address at send time; set it
	# only if the SMTP login differs from the From address.
	# Sender selection guard (review finding #2). "auto" (default) uses the real
	# SmtpEmailSender when SMTP is configured, else the StubEmailSender — the
	# convenient dev/test behaviour. "smtp" is fail-closed: build_email_sender()
	# raises if SMTP is not configured, so a mis-deployed production box cannot
	# silently record undelivered mail as SENT. "stub" always uses the stub.
	email_sender_mode: str = Field(default="auto", env="EMAIL_SENDER_MODE")
	smtp_host: Optional[str] = Field(default=None, env="SMTP_HOST")
	smtp_port: int = Field(default=587, env="SMTP_PORT")
	smtp_use_tls: bool = Field(default=True, env="SMTP_USE_TLS")
	smtp_username: Optional[str] = Field(default=None, env="SMTP_USERNAME")
	smtp_password: Optional[SecretStr] = Field(default=None, env="SMTP_PASSWORD")
	# §768: Reply-To points at the client's own inbox; every send is BCC'd.
	email_reply_to: Optional[str] = Field(default=None, env="EMAIL_REPLY_TO")
	email_bcc: Optional[str] = Field(default=None, env="EMAIL_BCC")

	# ── Mailgun inbound (Task 4.2.1 Path B) ───────────────────────────────────
	# Group D / D-2 fix: this used to be a second field
	# (mailgun_webhook_signing_key) holding the same Mailgun account webhook
	# signing key as mailgun_signing_key below, read by a different router
	# (src/api/mailgun_inbound_router.py). Two settings for one secret meant
	# setting only one silently disabled the other endpoint — both fail
	# closed with no startup error, just a 403/406 on every delivery. Both
	# routers now read mailgun_signing_key.
	# Slack channel for Speed-to-Lead closer-alert cards.
	slack_closer_alert_channel: str = Field(default="#blackink-setter", env="SLACK_CLOSER_ALERT_CHANNEL")

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

	# ── Demo sandbox dashboard export (src/tasks/seed_demo_sandbox.py) ──────
	# Service account JSON key path (gitignored `secrets/` dir, never
	# committed) and target Sheet ID for the sandbox dashboard export that
	# powers the free-tier Looker Studio demo dashboard. Chosen over a
	# direct Looker Studio -> Postgres connection specifically to avoid
	# exposing the shared production database's port to the internet.
	google_sheets_credentials_path: Optional[str] = Field(default=None, env="GOOGLE_SHEETS_CREDENTIALS_PATH")
	google_sheets_sandbox_id: Optional[str] = Field(default=None, env="GOOGLE_SHEETS_SANDBOX_ID")

	blackink_qa_slack_channel: Optional[str] = Field(default=None, env="BLACKINK_QA_SLACK_CHANNEL")
	blackink_command_slack_channel: Optional[str] = Field(default=None, env="BLACKINK_COMMAND_SLACK_CHANNEL")
	blackink_setter_slack_channel: Optional[str] = Field(default=None, env="BLACKINK_SETTER_SLACK_CHANNEL")
	sales_replies_slack_channel: Optional[str] = Field(default=None, env="SALES_REPLIES_SLACK_CHANNEL")
	dial_tasks_slack_channel: Optional[str] = Field(default=None, env="DIAL_TASKS_SLACK_CHANNEL")
	blackink_economics_slack_channel: Optional[str] = Field(default=None, env="BLACKINK_ECONOMICS_SLACK_CHANNEL")
	client_growth_slack_channel: Optional[str] = Field(default=None, env="CLIENT_GROWTH_SLACK_CHANNEL")
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

	# ── Inbound email (Task 4.2.1 Path B + 3.1.3 reply bridge) ──────────────
	# Mailgun webhook signing key — used to verify HMAC-SHA256 signatures on
	# BOTH inbound Mailgun webhook routes: the reply bridge
	# (src/api/inbound_email_router.py, /webhooks/inbound-email) and the
	# Speed-to-Lead Path B intake (src/api/mailgun_inbound_router.py,
	# /webhooks/mailgun-inbound). Mailgun issues one webhook signing key per
	# account, so this is genuinely one secret, not two — see Group D / D-2.
	# Without this, both endpoints reject all requests (fail closed).
	mailgun_signing_key: Optional[SecretStr] = Field(default=None, env="MAILGUN_SIGNING_KEY")

	# ── Internal admin API ───────────────────────────────────────────────────
	# HS256 signing secret for admin JWT tokens (internal dashboard auth).
	# Fail-closed: if unset the /auth/login endpoint returns 503.
	# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
	admin_jwt_secret: Optional[SecretStr] = Field(default=None, env="ADMIN_JWT_SECRET")
	admin_jwt_expiry_hours: int = Field(default=8, env="ADMIN_JWT_EXPIRY_HOURS")

	# ── Akrash ingestion ─────────────────────────────────────────────────────
	akrash_ingest_jwt_secret: Optional[SecretStr] = Field(default=None, env="AKRASH_INGEST_JWT_SECRET")

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

	# ── Oxylabs residential proxy ────────────────────────────────────────────
	oxylabs_username: Optional[str] = Field(default=None, env="OXYLABS_USERNAME")
	oxylabs_password: Optional[SecretStr] = Field(default=None, env="OXYLABS_PASSWORD")

	# ── Owner Visibility Score ───────────────────────────────────────────────
	# When absent the stub provider is used — max achievable score is 42/100
	# (38 website + 4 DBPR). Set to enable live Google Places API calls.
	google_places_api_key: Optional[SecretStr] = Field(default=None, env="GOOGLE_PLACES_API_KEY")

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
	# Signs one-click email-unsubscribe tokens (src/services/email_unsubscribe.py).
	# Deliberately its own secret, not a reuse of admin_jwt_secret — same
	# rationale as calendar_oauth_state_secret above: a public-facing token
	# must not share a signing key with an internal-admin-scoped one.
	email_unsubscribe_secret: Optional[SecretStr] = Field(default=None, env="EMAIL_UNSUBSCRIBE_SECRET")
	# S-8 — tracking pixel/click tokens (src/services/email_tracking.py).
	# Deliberately its own secret, not a reuse of email_unsubscribe_secret —
	# same "unrelated token families must rotate independently" rationale
	# CLAUDE.md states for that secret; a pixel/click token compromise must
	# never let an attacker forge unsubscribe tokens or vice versa.
	email_tracking_secret: Optional[SecretStr] = Field(default=None, env="EMAIL_TRACKING_SECRET")
	# ── Rent valuation adapter ───────────────────────────────────────────────
	# The client's "provider row disabled" (Week 1 Open Item #5). MUST ship
	# False: no valuation vendor is under contract, so enabling this would
	# point the adapter at a provider that does not exist. Flipped to True
	# only when a real RentValuationProvider implementation lands in Q1.
	rentbot_live_api_enabled: bool = Field(default=False, env="RENTBOT_LIVE_API_ENABLED")

	# ── Ghost Shopper IMAP listener (src/tasks/imap_listener.py) ───────────────
	# Monitors the audit-bot inbox for PM firm replies to Ghost Shopper form
	# submissions. Fail-closed: if imap_enabled is False the listener logs a
	# warning and exits immediately — active Ghost Shopper campaigns will stay
	# suspended at WAIT_REPLY until manually resumed or until imap_enabled is set.
	ghost_shopper_mock: bool = Field(default=False, env="GHOST_SHOPPER_MOCK")
	imap_enabled: bool = Field(default=False, env="IMAP_ENABLED")
	imap_host: str = Field(default="imap.gmail.com", env="IMAP_HOST")
	imap_port: int = Field(default=993, env="IMAP_PORT")
	imap_user: str = Field(default="audit-bot@audit-blackink.com", env="IMAP_USER")
	imap_password: Optional[SecretStr] = Field(default=None, env="IMAP_PASSWORD")
	# How long (hours) to wait for a PM firm reply before publishing a null resume
	# signal so the campaign continues without audit data.
	ghost_reply_timeout_hours: int = Field(default=24, env="GHOST_REPLY_TIMEOUT_HOURS")
	# ── Stripe — Zero-Deposit Card Auth & ACH Mandate Capture (Subtask 1.2.1) ─
	# Greenfield integration — no Stripe usage existed anywhere in this repo
	# before this subtask. Test-mode keys only until the applicable offers are
	# confirmed with the client (see payment_auth_offer_config — this flow is
	# explicitly NOT a universal zero-upfront rule; self-serve Respond/bundle
	# signups charge at signup via Stripe Checkout, unrelated to this flow).
	stripe_secret_key: Optional[SecretStr] = Field(default=None, env="STRIPE_SECRET_KEY")
	stripe_publishable_key: Optional[str] = Field(default=None, env="STRIPE_PUBLISHABLE_KEY")
	# Signs inbound Stripe webhook payloads (stripe.Webhook.construct_event) —
	# same fail-closed posture as every other secret here: unset means the
	# webhook route rejects everything rather than trusting an unsigned body.
	stripe_webhook_secret: Optional[SecretStr] = Field(default=None, env="STRIPE_WEBHOOK_SECRET")
	# Signs the short-lived onboarding token that stands in for the not-yet-
	# built authenticated onboarding portal (see src/services/payment_auth_token.py).
	# Deliberately its own secret, not a reuse of calendar_oauth_state_secret
	# or no_show_token_secret — same rationale as those: unrelated token
	# families must be able to rotate independently.
	payment_auth_onboarding_token_secret: Optional[SecretStr] = Field(
		default=None, env="PAYMENT_AUTH_ONBOARDING_TOKEN_SECRET"
	)

	# ── Settlement engine — 50/50 split + 60-day clawback (Subtask 1.2.2) ────
	# Which store publishes the Evidence Packet PDF and links it on the
	# Stripe invoice. Unset -> src/services/settlement/store.py falls back to
	# StubEvidencePacketStore, which always returns None — the same
	# fail-closed posture as EMAIL_SENDING_ENABLED / OVS_PDF_ALLOWED_HOSTS: a
	# charge cannot be recorded without a published packet (see
	# ck_settlement_evidence_packet_required / trg_settlement_guard_transition
	# in migrations/apply_settlement_ledger.py), so with no store configured
	# the pipeline compiles packets and bills nothing.
	settlement_evidence_packet_store: Optional[str] = Field(
		default=None, env="SETTLEMENT_EVIDENCE_PACKET_STORE"
	)
	# Only "stripe_files" is implemented today (Stripe Files + FileLink —
	# Invoices have no attachment field of their own, so this is the
	# zero-new-infrastructure option). Any other value is treated as unset.

	# Gates src/api/settlement_router.py's synthetic door_signed ingest —
	# the DoD's own test path, since no nightly PMS sync exists. Unset means
	# every request to that route is rejected (HTTP 503), same fail-closed
	# posture as every other secret-gated route in this file.
	settlement_operator_api_key: Optional[SecretStr] = Field(
		default=None, env="SETTLEMENT_OPERATOR_API_KEY"
	)

	# ── Respond Reply Triage Agent ───────────────────────────────────────────
	# Fail-closed: if ANTHROPIC_API_KEY is unset the classifier returns the
	# NURTURE fallback on every non-deterministic message rather than raising.
	anthropic_api_key: Optional[SecretStr] = Field(default=None, env="ANTHROPIC_API_KEY")
	# Shared secret the inbound parse service adds as X-Blackink-Inbound-Secret.
	# Fail-closed: if unset every inbound POST returns 503.
	inbound_parse_secret: Optional[SecretStr] = Field(default=None, env="INBOUND_PARSE_SECRET")
	# Domain suffix used to construct per-client inbound addresses:
	# replies@{client_id}.{inbound_email_domain}
	inbound_email_domain: str = Field(default="getblackink.com", env="INBOUND_EMAIL_DOMAIN")

	# ── Ink PDF Generator ─────────────────────────────────────────────────────
	# Local directory for campaign audit PDFs (dev/staging only).
	# Set PDF_LOCAL_DIR to a writable path; swap get_pdf_store() for S3PdfStore
	# once AWS credentials exist.
	pdf_local_dir: str = Field(default="tmp/pdf", env="PDF_LOCAL_DIR")

	# ── S-24 (W2 §3.2.4 A) — Assessor roll loader ────────────────────────────
	# A county tax-roll extract is not a stable self-serve HTTP download —
	# Hillsborough's own site sells its full assessment data as a paid,
	# manually-ordered product, and the FL DOR statewide portal's exact
	# current-year download path is unverified. So acquisition is a manual
	# operator step (buy/download the file, place it at this path) — these
	# settings name where the loader looks, not how the file got there.
	# Empty/missing path -> that county is skipped (fail closed, matching
	# EMAIL_SENDING_ENABLED's own "unset means don't guess" posture) rather
	# than the sweep silently doing nothing with no signal.
	assessor_roll_path_hillsborough: str = Field(
		default="data/assessor_rolls/hillsborough_fl.csv", env="ASSESSOR_ROLL_PATH_HILLSBOROUGH"
	)
	assessor_roll_path_pinellas: str = Field(
		default="data/assessor_rolls/pinellas_fl.csv", env="ASSESSOR_ROLL_PATH_PINELLAS"
	)
	# County tax rolls are certified/updated annually — an import older than
	# this is stale and worth a human's attention, not a silent gap.
	assessor_roll_staleness_days: int = Field(default=400, env="ASSESSOR_ROLL_STALENESS_DAYS")

	# ── Ink Sendspark ─────────────────────────────────────────────────────────
	# Fail-closed: if either is unset, node_sendspark logs SENDSPARK_SKIPPED and
	# returns video_id=None / landing_url=None -- campaign continues without video.
	sendspark_api_key:    Optional[SecretStr] = Field(default=None, env="SENDSPARK_API_KEY")
	sendspark_template_id: Optional[str]      = Field(default=None, env="SENDSPARK_TEMPLATE_ID")

	# ── Ink Approval Webhook ──────────────────────────────────────────────────
	# Shared secret for POST /api/v1/webhooks/ink/approve.
	# Fail-closed: if unset, every request returns 503.
	# Normal operator path is the Slack card buttons (Bolt action handlers),
	# which write to ink:resume_signals directly without this secret.
	ink_webhook_secret: Optional[SecretStr] = Field(default=None, env="INK_WEBHOOK_SECRET")

	# ── Six Billing Rules (Subtask 1.2.3) ────────────────────────────────────
	# Rule 1's $50 miss credit is keyed on inbound_messages.acked_at, which
	# nothing in this codebase writes yet (that's the automated first-response
	# sender's job — a separate, not-yet-built path; see
	# src/services/billing/miss_credit.py's module docstring). Until that
	# sender exists and is verified to actually stamp acked_at, EVERY
	# unclassified email older than 60 seconds looks identical to a miss —
	# the sweep must fail closed (a no-op, not a flood of false $50 credits)
	# rather than run on an unmet precondition. Flip to True only once the
	# automated-ack sender is live and acked_at is confirmed being written.
	billing_miss_credit_sweep_enabled: bool = Field(default=False, env="BILLING_MISS_CREDIT_SWEEP_ENABLED")

	# ── Vera health gate (S-1 / W0 §3.0.2 A) ─────────────────────────────────
	# src/tasks/vera_health_sweep.py runs every 5 minutes; a health run older
	# than this is treated as STALE_HEALTH_RUN by src/agents/vera/health_gate.py
	# and halts settlement/billing sweeps — a health job that silently stopped
	# running must not look identical to "everything is fine". Default of 30
	# tolerates 6 missed ticks before halting, which is generous enough to
	# absorb a single deploy/restart without a false-positive halt.
	vera_health_max_age_minutes: int = Field(default=30, env="VERA_HEALTH_MAX_AGE_MINUTES")


@lru_cache
def get_settings() -> AppSettings:
	"""Load and cache settings so expensive validation runs once."""
	return AppSettings()


settings = get_settings()
