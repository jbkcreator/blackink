"""Google Places signal provider — 5 signals totalling 58 pts.

Signal breakdown:
  google_rating          20 pts — average star rating (linear map 1–5 → 0–20)
  google_review_volume   16 pts — log-scaled review count (0 → 0, 100+ → 16)
  google_review_recency   8 pts — fraction of reviews from the last 12 months
  google_biz_completeness 8 pts — hours set (3) + photos ≥ 3 (3) + description (2)
  google_response_rate    6 pts — owner response rate to reviews

Auto-selects the stub when GOOGLE_PLACES_API_KEY is absent from settings.
The stub returns MISSING_DATA for every signal so no Google points accrue
until a live key is configured — maximum score without Google API: 42/100.

place_id resolution:
  The real provider does a Places text-search on the first call per company
  and stores the place_id in the ``company`` dict under key "google_place_id".
  The caller (owner_visibility_sweep.py) persists this back to the DB so
  subsequent months skip the text-search and go straight to Place Details —
  Google's recommended pattern for caching place identifiers.
"""

import logging
import math
from typing import Any

from config.settings import get_settings
from src.services.owner_visibility.signals.base import (
    SignalResult,
    SignalProvider,
    SCORED,
    MISSING_DATA,
)

logger = logging.getLogger(__name__)

_SIGNAL_NAMES = [
    "google_rating",
    "google_review_volume",
    "google_review_recency",
    "google_biz_completeness",
    "google_response_rate",
]
_SIGNAL_MAX = {
    "google_rating": 20,
    "google_review_volume": 16,
    "google_review_recency": 8,
    "google_biz_completeness": 8,
    "google_response_rate": 6,
}


def _missing(reason: str) -> list[SignalResult]:
    return [
        SignalResult(name, 0, _SIGNAL_MAX[name], MISSING_DATA, reason)
        for name in _SIGNAL_NAMES
    ]


def _score_rating(rating: float | None) -> SignalResult:
    if rating is None:
        return SignalResult("google_rating", 0, 20, MISSING_DATA, "no rating data")
    # Map 1–5 stars linearly to 0–20 pts; below 1 treated as 0.
    pts = max(0, round((rating - 1) / 4 * 20))
    return SignalResult("google_rating", pts, 20, SCORED, f"rating={rating:.1f}")


def _score_volume(count: int | None) -> SignalResult:
    if count is None:
        return SignalResult("google_review_volume", 0, 16, MISSING_DATA, "no review count")
    if count == 0:
        return SignalResult("google_review_volume", 0, 16, SCORED, "0 reviews")
    # log2 scale: 1 review → ~0 pts, 100 reviews → 11 pts, 500+ → 16 pts
    pts = min(16, round(math.log2(count + 1) * 2))
    return SignalResult("google_review_volume", pts, 16, SCORED, f"review_count={count}")


def _score_recency(recent_fraction: float | None) -> SignalResult:
    """recent_fraction = reviews_in_last_12m / total_reviews, 0.0–1.0."""
    if recent_fraction is None:
        return SignalResult("google_review_recency", 0, 8, MISSING_DATA, "no recency data")
    pts = round(recent_fraction * 8)
    return SignalResult(
        "google_review_recency", pts, 8, SCORED,
        f"{round(recent_fraction * 100)}% of reviews within last 12 months",
    )


def _score_completeness(hours_set: bool, photo_count: int, has_description: bool) -> SignalResult:
    pts = (3 if hours_set else 0) + (3 if photo_count >= 3 else 0) + (2 if has_description else 0)
    detail = (
        f"hours={'yes' if hours_set else 'no'}, "
        f"photos={photo_count}, "
        f"description={'yes' if has_description else 'no'}"
    )
    return SignalResult("google_biz_completeness", pts, 8, SCORED, detail)


def _score_response_rate(rate: float | None) -> SignalResult:
    """rate = fraction of reviews with an owner response, 0.0–1.0."""
    if rate is None:
        return SignalResult("google_response_rate", 0, 6, MISSING_DATA, "no response rate data")
    pts = round(rate * 6)
    return SignalResult(
        "google_response_rate", pts, 6, SCORED,
        f"owner responded to {round(rate * 100)}% of reviews",
    )


class StubGooglePlacesProvider(SignalProvider):
    """Returns MISSING_DATA for all 5 Google signals.

    Active when GOOGLE_PLACES_API_KEY is not configured. Safe to use in
    production — no Google API calls are made, no points are awarded,
    and data_gaps will list all five signal names.
    """

    def collect(self, company: dict[str, Any]) -> list[SignalResult]:
        return _missing("GOOGLE_PLACES_API_KEY not configured — stub provider active")


class GooglePlacesProvider(SignalProvider):
    """Live Google Places provider — requires GOOGLE_PLACES_API_KEY in settings."""

    def __init__(self, api_key: str) -> None:
        import googlemaps  # imported here so the module loads without the dep when stub is used
        self._client = googlemaps.Client(key=api_key)

    def _resolve_place_id(self, company: dict[str, Any]) -> str | None:
        """Return cached place_id or run a text-search to find it."""
        place_id: str | None = company.get("google_place_id")
        if place_id:
            return place_id

        query = f"{company.get('company_name', '')} property management"
        try:
            result = self._client.find_place(
                query,
                input_type="textquery",
                fields=["place_id"],
            )
            candidates = result.get("candidates", [])
            if candidates:
                found_id: str = candidates[0]["place_id"]
                # Mutate dict so the caller can persist the resolved id.
                company["google_place_id"] = found_id
                return found_id
        except Exception as exc:
            logger.warning("google_places: place search failed for %r: %s", company.get("company_name"), exc)
        return None

    def collect(self, company: dict[str, Any]) -> list[SignalResult]:
        place_id = self._resolve_place_id(company)
        if not place_id:
            return _missing("place_id could not be resolved via Places text-search")

        fields = [
            "rating", "user_ratings_total", "reviews",
            "opening_hours", "photos", "editorial_summary",
        ]
        try:
            details = self._client.place(place_id, fields=fields).get("result", {})
        except Exception as exc:
            logger.warning("google_places: place details failed for %s: %s", place_id, exc)
            return _missing(f"Place Details API error: {exc}")

        rating: float | None = details.get("rating")
        total_reviews: int | None = details.get("user_ratings_total")
        reviews: list[dict] = details.get("reviews", [])

        # Recent-fraction: Google returns up to 5 most recent reviews with timestamps.
        # We approximate: if all returned reviews are recent and the total is low, rate is high.
        recent_fraction: float | None = None
        if reviews and total_reviews:
            import time
            cutoff = time.time() - 365 * 24 * 3600
            recent = sum(1 for r in reviews if r.get("time", 0) >= cutoff)
            # Scale by ratio of sample to population — this underestimates for large counts
            # but it's the best we can do with the Places API's 5-review cap.
            recent_fraction = min(1.0, recent / len(reviews))

        hours_set = bool(details.get("opening_hours", {}).get("periods"))
        photo_count = len(details.get("photos", []))
        has_description = bool(details.get("editorial_summary", {}).get("overview"))

        # Response rate: count reviews that have an owner reply.
        owner_replies = sum(1 for r in reviews if r.get("author_name") and r.get("text") and "reply" in r)
        response_rate: float | None = (owner_replies / len(reviews)) if reviews else None

        return [
            _score_rating(rating),
            _score_volume(total_reviews),
            _score_recency(recent_fraction),
            _score_completeness(hours_set, photo_count, has_description),
            _score_response_rate(response_rate),
        ]


def build_google_places_provider() -> SignalProvider:
    """Return the real provider if a key is configured, otherwise the stub."""
    api_key_field = get_settings().google_places_api_key
    if api_key_field is None:
        return StubGooglePlacesProvider()
    key = api_key_field.get_secret_value()
    if not key:
        return StubGooglePlacesProvider()
    return GooglePlacesProvider(api_key=key)
