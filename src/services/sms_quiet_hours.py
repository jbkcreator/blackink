"""SMS quiet hours enforcement — TCPA compliance.

Ported from Forced Action's sms_compliance.py (_recipient_tz, is_quiet_hours,
_AREA_CODE_TZ). Adapted for Blackink's Florida PM target markets:
Tampa/St. Pete, Orlando, Miami-Dade. All are Eastern Time except 850
(Panhandle) which maps conservatively to CST (over-suppress rather than risk
a TCPA violation on the ET/CST boundary).

Rule: no outbound SMS 9PM–8AM recipient local time (TCPA §64.1200).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

# TCPA quiet window
_QUIET_START_HOUR = 21  # 9 PM — first hour that is quiet
_QUIET_END_HOUR = 8     # 8 AM — first hour that is not quiet

# Florida area codes → IANA timezone.
# 850 = Panhandle (straddles ET and CST) — mapped to CST so we over-suppress
# rather than under-suppress for the CST portion. Same rationale as FA ADR.
_AREA_CODE_TZ: dict[str, str] = {
    "850": "America/Chicago",  # Panhandle conservative mapping
    **{
        ac: "America/New_York"
        for ac in [
            # Tampa / St. Pete
            "813", "727", "941",
            # Orlando
            "407", "321",
            # Miami-Dade / Broward / Palm Beach
            "305", "786", "754", "954", "561",
            # Other FL metros
            "239", "352", "386", "772", "863", "904",
        ]
    },
}


def _recipient_tz(phone: str) -> ZoneInfo:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if digits.startswith("1"):
        digits = digits[1:]
    tz_name = _AREA_CODE_TZ.get(digits[:3], "America/New_York")
    return ZoneInfo(tz_name)


def is_quiet_hours(phone: str) -> bool:
    """Return True if current local time for this number is in TCPA quiet window.

    Quiet window: 9PM–8AM recipient local time.
    Unknown area codes default to Eastern Time (safe for FL market).
    """
    hour = datetime.now(_recipient_tz(phone)).hour
    return hour < _QUIET_END_HOUR or hour >= _QUIET_START_HOUR


def assert_not_quiet_hours(phone: str) -> None:
    """Raise ValueError if currently in quiet hours for this number.

    Hard gate called before any outbound SMS dispatch. Caller logs and
    routes to dead-letter queue on ValueError.
    """
    if is_quiet_hours(phone):
        tz = _recipient_tz(phone)
        local_hour = datetime.now(tz).hour
        raise ValueError(
            f"SMS blocked by quiet hours for {phone}: "
            f"local hour={local_hour} (quiet window is 9PM–8AM). "
            f"Timezone: {tz.key}"
        )
