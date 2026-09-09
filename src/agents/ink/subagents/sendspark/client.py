"""Sendspark API client -- creates personalised dynamic videos from a template.

Usage
-----
    from src.agents.ink.subagents.sendspark.client import SendsparkClient, SendsparkSkipped

    client = SendsparkClient(api_key="sk-...", template_id="tpl_...")
    try:
        result = client.render(company_name="Acme PM", response_time="14 hr 23 min", loss_estimate="$23,344")
        video_id   = result.video_id
        landing_url = result.landing_url
    except SendsparkSkipped as e:
        # API key / template not configured -- campaign continues without video
        logger.warning("sendspark skipped: %s", e)

Verification checklist (run once after account is created)
------------------------------------------------------------
Before going live, verify these against https://docs.sendspark.com/api:
  [ ] BASE_URL is correct
  [ ] POST /v1/dynamic-videos is the correct render endpoint
  [ ] Request body keys match (template_id, variables, title)
  [ ] Response keys for video_id and landing page URL
  [ ] Auth header format is "Bearer <api_key>"

Update _RENDER_ENDPOINT and _parse_response() below if anything differs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_BASE_URL       = "https://api.sendspark.com"
_RENDER_ENDPOINT = "/v1/dynamic-videos"   # VERIFY against docs.sendspark.com/api
_TIMEOUT_SEC    = 30


class SendsparkError(Exception):
    """Sendspark API returned an error response."""


class SendsparkSkipped(Exception):
    """Integration not configured -- caller should treat video as absent."""


@dataclass(frozen=True)
class SendsparkResult:
    video_id:    str
    landing_url: str


class SendsparkClient:
    def __init__(self, api_key: str, template_id: str) -> None:
        if not api_key or not template_id:
            raise SendsparkSkipped("SENDSPARK_API_KEY or SENDSPARK_TEMPLATE_ID not set")
        self._api_key    = api_key
        self._template_id = template_id

    def render(
        self,
        company_name:  str,
        response_time: str,
        loss_estimate: str,
    ) -> SendsparkResult:
        """Render one personalised video and return its id + landing URL.

        Variables must match the placeholders defined in the Sendspark template
        (see docs/sendspark_setup.md for the exact names to use when recording).

        Raises SendsparkError on a non-2xx response.
        """
        payload = {
            "template_id": self._template_id,
            "title": f"Response Time Audit -- {company_name}",
            "variables": {
                "company_name":  company_name,
                "response_time": response_time,
                "loss_estimate": loss_estimate,
            },
        }

        try:
            response = httpx.post(
                f"{_BASE_URL}{_RENDER_ENDPOINT}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type":  "application/json",
                },
                timeout=_TIMEOUT_SEC,
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            raise SendsparkError(f"network error: {exc}") from exc

        if not response.is_success:
            raise SendsparkError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )

        return _parse_response(response.json())


def _parse_response(body: dict) -> SendsparkResult:
    """Extract video_id and landing_url from the API response.

    VERIFY these key names against the actual Sendspark API response.
    Common shapes seen in video personalization APIs:
        { "id": "...", "url": "..." }
        { "video_id": "...", "share_url": "..." }
        { "data": { "id": "...", "landing_page_url": "..." } }

    Update the key lookups below to match what Sendspark actually returns.
    """
    # Unwrap nested "data" envelope if present
    data = body.get("data", body)

    video_id = (
        data.get("id")
        or data.get("video_id")
        or data.get("videoId")
    )
    landing_url = (
        data.get("url")
        or data.get("share_url")
        or data.get("shareUrl")
        or data.get("landing_page_url")
        or data.get("landingPageUrl")
    )

    if not video_id or not landing_url:
        raise SendsparkError(
            f"unexpected response shape -- could not find video_id/url. "
            f"Raw: {str(body)[:300]}. "
            f"Update _parse_response() to match the actual Sendspark response."
        )

    return SendsparkResult(video_id=str(video_id), landing_url=str(landing_url))


def get_client() -> SendsparkClient:
    """Return a configured SendsparkClient, or raise SendsparkSkipped if unconfigured."""
    from config.settings import get_settings
    s = get_settings()
    api_key     = s.sendspark_api_key.get_secret_value() if s.sendspark_api_key else ""
    template_id = s.sendspark_template_id or ""
    return SendsparkClient(api_key=api_key, template_id=template_id)
