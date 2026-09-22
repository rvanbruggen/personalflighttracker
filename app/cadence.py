"""The polling phases, described once so the tracker's schedule and the
email's "when will I hear next?" explanation can never disagree."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import settings

AIRBORNE = {"departed", "enroute", "en route", "approaching", "diverted"}


@dataclass
class NextStep:
    kind: str  # "scheduled" | "completed" | "abandoned"
    at: Optional[datetime] = None  # naive UTC; when the next check runs
    phase: str = "unknown"  # far | day | close | airborne | unknown | final
    final_status: str = ""


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def phase_for(status: str, dep_scheduled_utc: Optional[datetime], now: datetime) -> str:
    """Which cadence applies. Mirrors tracker.next_poll_delay exactly."""
    if (status or "").strip().lower() in AIRBORNE:
        return "airborne"
    departure = _aware(dep_scheduled_utc)
    if departure is None:
        return "unknown"
    until = departure - _aware(now)
    if until > timedelta(hours=24):
        return "far"
    if until > timedelta(hours=2):
        return "day"
    return "close"


def interval_for(phase: str) -> Optional[timedelta]:
    return {
        "airborne": timedelta(minutes=settings.poll_airborne_minutes),
        "unknown": timedelta(minutes=settings.poll_retry_minutes),
        "far": timedelta(hours=settings.poll_far_out_hours),
        "day": timedelta(minutes=settings.poll_within_24h_minutes),
        "close": timedelta(minutes=settings.poll_within_2h_minutes),
    }.get(phase)


def describe_interval(delta: Optional[timedelta]) -> str:
    """timedelta -> 'every 10 minutes' / 'every hour' / 'about once a week'."""
    if delta is None:
        return ""
    minutes = int(delta.total_seconds() // 60)
    if minutes % (60 * 24 * 7) == 0:
        weeks = minutes // (60 * 24 * 7)
        return "about once a week" if weeks == 1 else f"every {weeks} weeks"
    if minutes % (60 * 24) == 0:
        days = minutes // (60 * 24)
        return "once a day" if days == 1 else f"every {days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "every hour" if hours == 1 else f"every {hours} hours"
    return "every minute" if minutes == 1 else f"every {minutes} minutes"
