"""Instantly.ai API client — trimmed core.

Adapted from Forced Action's src/services/instantly_service.py: auth,
throttle/backoff, and the warmup/deliverability analytics endpoints
(get_warmup_analytics, get_daily_analytics) are ported now since the
Deliverability Sentinel needs them; campaign-CRUD methods (create/pause/
add_leads) are deferred to whenever the actual Campaign Agent dispatch
workstream lands — no need to build them before there's a sender to call
them from. instantly_enabled defaults False so an unconfigured environment
never attempts a live call.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

from config.settings import get_settings

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2


class InstantlyDisabledError(RuntimeError):
	"""Raised when a call is attempted but instantly_enabled is False or no API key is set."""


class InstantlyService:
	def __init__(self):
		self.settings = get_settings()

	def _enabled(self) -> bool:
		return bool(self.settings.instantly_enabled and self.settings.instantly_api_key)

	def _request(self, method: str, path: str, **kwargs) -> Optional[Dict[str, Any]]:
		if not self._enabled():
			raise InstantlyDisabledError("Instantly is not enabled or INSTANTLY_API_KEY is unset")

		url = f"{self.settings.instantly_base_url.rstrip('/')}/{path.lstrip('/')}"
		headers = kwargs.pop("headers", {})
		headers["Authorization"] = f"Bearer {self.settings.instantly_api_key.get_secret_value()}"

		for attempt in range(1, _MAX_RETRIES + 1):
			try:
				response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
			except requests.RequestException as e:
				logger.warning("Instantly request failed (attempt %d/%d): %s", attempt, _MAX_RETRIES, e)
				time.sleep(_BACKOFF_BASE_SECONDS**attempt)
				continue

			if response.status_code == 429:
				retry_after = int(response.headers.get("Retry-After", _BACKOFF_BASE_SECONDS**attempt))
				logger.warning("Instantly rate-limited, retrying in %ds", retry_after)
				time.sleep(retry_after)
				continue

			if response.status_code >= 500:
				logger.warning("Instantly server error %d (attempt %d/%d)", response.status_code, attempt, _MAX_RETRIES)
				time.sleep(_BACKOFF_BASE_SECONDS**attempt)
				continue

			response.raise_for_status()
			return response.json() if response.content else None

		logger.error("Instantly request exhausted retries: %s %s", method, path)
		return None

	def get_warmup_analytics(self, account_email: str) -> Optional[Dict[str, Any]]:
		"""Warmup health score + related signal for one mailbox."""
		return self._request("GET", f"/api/v2/accounts/{account_email}/warmup-analytics")

	def get_daily_analytics(self, campaign_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
		"""Bounce/complaint/open/click signal, optionally scoped to one campaign."""
		params = {"campaign_id": campaign_id} if campaign_id else {}
		return self._request("GET", "/api/v2/campaigns/analytics/daily", params=params)

	def list_accounts(self) -> Optional[Dict[str, Any]]:
		return self._request("GET", "/api/v2/accounts")

	# ── Campaign dispatch — called by the Relay worker ────────────────────────
	#
	# NOTE: The endpoint paths and request/response shapes below are based on
	# the Instantly v2 API docs (api.instantly.ai). Verify against
	# app.instantly.ai/api-docs before deploying. The _request() method
	# handles auth and retries; update endpoint paths here if they differ.

	def create_campaign(
		self,
		name:           str,
		sequence_steps: List[Dict[str, Any]],
		account_emails: Optional[List[str]] = None,
	) -> Optional[str]:
		"""Create a campaign with the given sequence steps.

		sequence_steps: list of step dicts from build_instantly_sequence()
		  [{"type": "email", "delay": N, "variants": [{"subject": ..., "body": ...}]}, ...]

		Returns the campaign_id string, or None on failure.

		VERIFY: POST /api/v2/campaigns request/response shape.
		"""
		body: Dict[str, Any] = {
			"name": name,
			"sequences": [{"steps": sequence_steps}],
		}
		if account_emails:
			body["email_account"] = account_emails[0]  # VERIFY: key name and cardinality
		result = self._request("POST", "/api/v2/campaigns", json=body)
		if not result:
			return None
		# VERIFY: exact key name in response (id vs campaign_id)
		return result.get("id") or result.get("campaign_id")

	def add_leads(
		self,
		campaign_id: str,
		leads:       List[Dict[str, Any]],
	) -> bool:
		"""Add leads to a campaign.

		Each lead dict: {email, first_name, last_name, company_name, ...}
		Custom variables should be nested under the key Instantly expects.

		VERIFY: POST /api/v2/leads endpoint and request shape.
		"""
		body = {"campaign_id": campaign_id, "leads": leads}
		result = self._request("POST", "/api/v2/leads", json=body)
		return result is not None

	def activate_campaign(self, campaign_id: str) -> bool:
		"""Start a campaign so Instantly begins sending.

		VERIFY: endpoint path (may be PATCH /api/v2/campaigns/{id} with
		{status: "active"} rather than a dedicated /activate route).
		"""
		result = self._request("POST", f"/api/v2/campaigns/{campaign_id}/activate")
		return result is not None
