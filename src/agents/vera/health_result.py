"""Vera — tri-state health-check result.

Three states, no conflation:

  VALUE   — a real reading was obtained from the source.
             `value` carries a dict or scalar.
             A VALUE(value=0) means "zero was the real answer" —
             a checked, empty result. Not the same as ABSTAIN.

  UNKNOWN — the source is not configured (e.g. Instantly disabled,
             no API key). The check was deliberately skipped.
             `value` is always None.

  ABSTAIN — the source is configured and the check attempted, but
             the call failed or data could not be verified.
             `value` is always None.
             ABSTAIN must always block downstream consumers from
             treating the result as clean — same semantics as
             compliance_gate.ABSTAIN.

The silent-zero invariant:
  A check function MUST NOT return VALUE when its data came from
  swallowing an exception. Doing so would make a failed pull look
  like a verified-clean empty result — the exact bug this type
  exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

VALUE = "VALUE"
UNKNOWN = "UNKNOWN"
ABSTAIN = "ABSTAIN"

VALID_STATES = (VALUE, UNKNOWN, ABSTAIN)


@dataclass(frozen=True)
class HealthResult:
    check_name: str
    state: str           # VALUE | UNKNOWN | ABSTAIN
    value: Optional[Any] # Non-None only when state == VALUE
    detail: str          # Human-readable reason / summary

    def __post_init__(self):
        if self.state not in VALID_STATES:
            raise ValueError(f"state must be one of {VALID_STATES}, got {self.state!r}")
        if self.state != VALUE and self.value is not None:
            raise ValueError(
                f"HealthResult.value must be None when state={self.state!r}; "
                f"got value={self.value!r}. Never fabricate a value for UNKNOWN/ABSTAIN."
            )
