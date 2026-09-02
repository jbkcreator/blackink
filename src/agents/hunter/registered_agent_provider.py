"""Hunter — registered-agent lookup stub.

No vendor contracted for week 0. The interface is defined here so the
entity_resolution module can call it without needing to change when a real
provider (e.g. OpenCorporates, SunBiz scrape) is wired in later.
"""
from __future__ import annotations

from typing import Optional


def lookup_registered_agent(domain: str) -> Optional[str]:
    """Return the beneficial owner name for a domain, or None if unknown.

    Week 0: always returns None (stub). A real implementation would query
    a state-SOS / OpenCorporates API and return the registered agent's name.
    """
    return None
