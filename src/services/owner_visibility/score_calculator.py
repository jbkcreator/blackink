"""Pure score calculator — no I/O, no DB, fully unit-testable.

Aggregates SignalResult lists from all providers into a ScoreBreakdown.
MISSING_DATA signals contribute 0 points and their names go into data_gaps.
SKIPPED signals are excluded from both the score and the gap list.

Provider-to-bucket mapping is hard-coded here so the calculator is the
single place that knows the total-point architecture:
  Website signals:  38 pts max (4 signals from WebsiteSignalProvider)
  DBPR signal:       4 pts max (1 signal from DbprLicenceSignalProvider)
  Google signals:   58 pts max (5 signals from GooglePlacesSignalProvider)
  Total:           100 pts
"""

from dataclasses import dataclass, field
from typing import Any

from src.services.owner_visibility.signals.base import SignalResult, SCORED, MISSING_DATA

# Signal name prefixes used to assign each result to its bucket.
_WEBSITE_PREFIX = "website_"
_DBPR_PREFIX = "dbpr_"
_GOOGLE_PREFIX = "google_"


@dataclass(frozen=True)
class ScoreBreakdown:
    score_total: int
    score_website: int
    score_dbpr: int
    score_google: int
    signal_detail: dict[str, Any]   # keyed by signal_name — stored as JSONB
    data_gaps: list[str]            # signal names that returned MISSING_DATA


def calculate_score(signals: list[SignalResult]) -> ScoreBreakdown:
    """Aggregate a flat list of SignalResults into a ScoreBreakdown.

    Raises ValueError if the same signal_name appears more than once
    (indicates a misconfigured provider list, not a recoverable runtime
    condition).
    """
    seen: set[str] = set()
    duplicates = [s.signal_name for s in signals if s.signal_name in seen or seen.add(s.signal_name)]
    if duplicates:
        raise ValueError(f"Duplicate signal names detected: {duplicates}")

    website_pts = 0
    dbpr_pts = 0
    google_pts = 0
    other_pts = 0  # future providers that haven't adopted the naming convention yet
    data_gaps: list[str] = []
    detail: dict[str, Any] = {}

    for s in signals:
        detail[s.signal_name] = s.to_dict
        if s.status == MISSING_DATA:
            data_gaps.append(s.signal_name)
        if s.status != SCORED:
            continue
        if s.signal_name.startswith(_WEBSITE_PREFIX):
            website_pts += s.points_awarded
        elif s.signal_name.startswith(_DBPR_PREFIX):
            dbpr_pts += s.points_awarded
        elif s.signal_name.startswith(_GOOGLE_PREFIX):
            google_pts += s.points_awarded
        else:
            other_pts += s.points_awarded

    total = website_pts + dbpr_pts + google_pts + other_pts
    return ScoreBreakdown(
        score_total=min(total, 100),
        score_website=website_pts,
        score_dbpr=dbpr_pts,
        score_google=google_pts,
        signal_detail=detail,
        data_gaps=data_gaps,
    )
