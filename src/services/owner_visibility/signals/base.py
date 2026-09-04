"""Signal result type and provider interface for the Owner Visibility Score engine."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

# Signal status values — match convention from compliance_gate.py (PASS/FAIL/ABSTAIN)
# but named for scoring semantics.
SCORED = "SCORED"
MISSING_DATA = "MISSING_DATA"  # provider ran but the data needed is absent
SKIPPED = "SKIPPED"            # provider deliberately did not run this signal


@dataclass(frozen=True)
class SignalResult:
    signal_name: str
    points_awarded: int
    points_possible: int
    status: str  # SCORED | MISSING_DATA | SKIPPED
    detail: str

    def __post_init__(self) -> None:
        if self.status not in (SCORED, MISSING_DATA, SKIPPED):
            raise ValueError(f"Invalid signal status {self.status!r} for {self.signal_name!r}")
        if self.status == SCORED and not (0 <= self.points_awarded <= self.points_possible):
            raise ValueError(
                f"{self.signal_name}: points_awarded={self.points_awarded} out of range "
                f"[0, {self.points_possible}]"
            )
        if self.status != SCORED and self.points_awarded != 0:
            raise ValueError(
                f"{self.signal_name}: points_awarded must be 0 when status is {self.status!r}"
            )

    @property
    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_name": self.signal_name,
            "points_awarded": self.points_awarded,
            "points_possible": self.points_possible,
            "status": self.status,
            "detail": self.detail,
        }


class SignalProvider(ABC):
    """Collect one or more signals for a single company.

    ``company`` is a plain dict with at minimum:
        company_id (str), domain (str), company_name (str),
        county_slug (str), website (str | None),
        google_place_id (str | None).

    Implementations must never raise — return MISSING_DATA / SKIPPED
    results for any failure path so the score calculator always gets a
    complete list.
    """

    @abstractmethod
    def collect(self, company: dict[str, Any]) -> list[SignalResult]:
        """Return one SignalResult per signal this provider covers."""
