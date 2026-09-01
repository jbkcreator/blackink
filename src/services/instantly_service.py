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
from typing import Any, Dict, Optional

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
