"""Timezone helpers shared across the GC bot modules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:  # pragma: no cover - pytz is optional but recommended
    import pytz
except ImportError:  # pragma: no cover
    pytz = None

TZ_UTC = timezone.utc
if pytz is not None:
    TZ_JST = pytz.timezone("Asia/Tokyo")
else:
    TZ_JST = timezone(timedelta(hours=9))


def now_jst() -> datetime:
    """Return current time in JST timezone."""
    return datetime.now(TZ_JST)


def floor_to_full_hour_utc(dt_utc: datetime) -> datetime:
    """Floor a UTC datetime to the previous exact hour boundary."""
    return dt_utc.replace(minute=0, second=0, microsecond=0)


__all__ = ["TZ_UTC", "TZ_JST", "now_jst", "floor_to_full_hour_utc"]
