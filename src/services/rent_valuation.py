"""Rent valuation adapter interface — the seam a real valuation vendor
plugs into.

SCOPE IS DELIBERATELY NARROW, by client instruction (Tasks/Blackink_Week_0_
Week_1_Open_Items_with_Answers.md, Week 1 Open Item #5): "No contract exists
with either provider, so the Rent Analysis Bot cannot be built — it is
already Q1 for that reason. Define the adapter interface only if close to
free, leave the provider row disabled. Do not start a trial on our behalf."

So this module defines the contract and nothing else: no cache, no fallback
orchestration, no caller. Those arrive in Q1 with the rest of the bot — the
implementation plan's Appendix A specifies them in full, already-audited form.

Same ABC-with-stub-default shape as src/services/compliance_gate.py's
DncProvider / EmailVerificationProvider, which solve the identical problem
(a vendor the blueprint names but nobody has a contract with): the stub
returns None, never a fabricated number, so no caller can mistake "we do
not know" for an answer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from config.settings import get_settings


@dataclass(frozen=True)
class ParsedAddress:
    """A normalized US property address.

    Defined here rather than alongside the (deferred) address parser
    because it is part of THIS interface's contract — a provider is a
    function from an address to a valuation. Q1's parser imports this
    type; it must not declare a second one."""

    street: str
    city: str
    state: str
    zip_code: Optional[str]


@dataclass(frozen=True)
class ValuationResult:
    estimated_rent: int
    range_low: int
    range_high: int
    confidence_score: int  # 0-100
    source: str  # provider identifier, e.g. "rentcast" | "corelogic" | "cache"
    address_display: str


class RentValuationProvider(ABC):
    @abstractmethod
    def get(self, address: ParsedAddress) -> Optional[ValuationResult]:
        """Return a ValuationResult, or None if this provider could not
        determine one. Implementations must NOT raise for an ordinary miss
        or a vendor outage — None is the answer for "unknown", and the
        caller decides what to do about it."""


class StubLiveRentValuationProvider(RentValuationProvider):
    """The disabled provider row. No valuation vendor is under contract
    (client, Wk1 #5), so this always returns None — never a fabricated,
    live-looking number. Replacing this class with a real implementation
    and flipping RENTBOT_LIVE_API_ENABLED is the entire Q1 integration
    surface."""

    def get(self, address: ParsedAddress) -> Optional[ValuationResult]:
        return None


def is_live_provider_enabled() -> bool:
    """The client's 'provider row disabled' switch. Ships False and must
    stay False until a vendor contract actually exists."""
    return get_settings().rentbot_live_api_enabled


def get_live_provider() -> RentValuationProvider:
    """Single construction point, so the Q1 swap is one return statement."""
    return StubLiveRentValuationProvider()
