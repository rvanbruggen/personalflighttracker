"""Provider-neutral shapes. Every status source normalises into FlightSnapshot,
so swapping AeroDataBox for something else stays a one-file change."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol


class ProviderError(RuntimeError):
    """Upstream call failed in a way worth retrying later."""


class FlightNotFound(ProviderError):
    """The provider has no record of this flight number on this date (yet)."""


class QuotaExceeded(ProviderError):
    """We would blow the configured free-tier budget; skip rather than spend."""


# Statuses that mean the aircraft is in the air (poll fastest).
AIRBORNE_STATUSES = {"departed", "enroute", "en route", "approaching", "diverted"}
# Statuses that mean we can stop polling.
TERMINAL_STATUSES = {"arrived", "canceled", "cancelled", "landed"}


def parse_api_time(value: Optional[str]) -> Optional[datetime]:
    """AeroDataBox emits '2026-08-26 10:00Z' / '2026-08-26 12:00+02:00'."""
    if not value:
        return None
    text = value.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_LOCAL_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})")


def pretty_local(value: Optional[str]) -> str:
    """'2026-08-26 12:00+02:00' -> '2026-08-26 12:00' (airport-local wall time)."""
    if not value:
        return ""
    match = _LOCAL_RE.search(value)
    return f"{match.group(1)} {match.group(2)}" if match else value.strip()


@dataclass
class FlightSnapshot:
    """One provider's view of a flight at a point in time."""

    flight_number: str = ""
    status: str = ""
    callsign: str = ""
    airline: str = ""
    aircraft_reg: str = ""
    aircraft_model: str = ""

    dep_iata: str = ""
    dep_name: str = ""
    dep_terminal: str = ""
    dep_gate: str = ""
    dep_scheduled_utc: Optional[datetime] = None
    dep_scheduled_local: str = ""
    dep_actual_utc: Optional[datetime] = None
    dep_actual_local: str = ""

    arr_iata: str = ""
    arr_name: str = ""
    arr_terminal: str = ""
    arr_gate: str = ""
    arr_baggage_belt: str = ""
    arr_scheduled_utc: Optional[datetime] = None
    arr_scheduled_local: str = ""
    arr_actual_utc: Optional[datetime] = None
    arr_actual_local: str = ""

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_airborne(self) -> bool:
        return self.status.strip().lower() in AIRBORNE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status.strip().lower() in TERMINAL_STATUSES


class StatusProvider(Protocol):
    name: str

    async def fetch(self, flight_number: str, flight_date: str) -> FlightSnapshot:
        """Return the current status, or raise a ProviderError subclass."""
        ...
