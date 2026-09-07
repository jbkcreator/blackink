"""Business-hours logic for Speed-to-Lead SLA (Task 4.2.1).

Fixed ET window (08:00–18:00 Mon–Fri). Single config constant; seam to
go per-client later by adding a client_id → tz/hours lookup.

SPEC-4.2.1.md §3: send_at = now if within business hours, else next
business-open (08:00 ET next weekday).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytz

_ET = pytz.timezone("America/New_York")
_BH_START = time(8, 0)
_BH_END = time(18, 0)
_BUSINESS_DAYS = {0, 1, 2, 3, 4}   # Mon–Fri


def _is_business_hours(dt_et: datetime) -> bool:
    return dt_et.weekday() in _BUSINESS_DAYS and _BH_START <= dt_et.time() < _BH_END


def _next_business_open(dt_et: datetime) -> datetime:
    """Returns next 08:00 ET on a weekday, starting from the day after dt_et
    if dt_et is already past 08:00, or today if before 08:00 and a weekday."""
    candidate = dt_et.replace(hour=8, minute=0, second=0, microsecond=0)
    if dt_et.time() >= _BH_START:
        candidate += timedelta(days=1)
    # Advance past weekends
    while candidate.weekday() not in _BUSINESS_DAYS:
        candidate += timedelta(days=1)
    return candidate


def compute_send_at(received_at_utc: datetime) -> datetime:
    """Returns send_at as a UTC datetime.
    If received during business hours → now (same as received_at_utc).
    Else → next business-open at 08:00 ET, converted to UTC."""
    dt_et = received_at_utc.astimezone(_ET)
    if _is_business_hours(dt_et):
        return received_at_utc
    next_open_et = _next_business_open(dt_et)
    return _ET.localize(next_open_et.replace(tzinfo=None)).astimezone(timezone.utc)
