from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional
from src.config import CALENDAR_TIMEZONE

# Centralised timezone utilities — internal storage remains UTC, display in configured TZ
TZ = ZoneInfo(CALENDAR_TIMEZONE or "Asia/Kolkata")


def now_ist() -> datetime:
    """Return current time in the configured display timezone (default Asia/Kolkata)."""
    return datetime.now(TZ)


def utc_to_ist(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a UTC-aware datetime to the configured display timezone. If dt is naive, assume UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def format_datetime_ist(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    """Format a datetime for display in the configured timezone. Accepts naive or tz-aware datetimes."""
    if dt is None:
        return ""
    ist = utc_to_ist(dt)
    return ist.strftime(fmt)
