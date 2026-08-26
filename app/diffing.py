"""Diff a fresh snapshot against the stored baseline and describe what changed
in words a traveller cares about."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .db import Flight
from .providers.base import FlightSnapshot


@dataclass
class Change:
    field: str
    label: str
    old: str
    new: str
    # True for things worth waking a phone over.
    significant: bool = True

    def as_line(self) -> str:
        old = self.old or "—"
        new = self.new or "—"
        return f"{self.label}: {old} → {new}"


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite gives back naive datetimes; providers give aware ones. Normalise
    so the two are never subtracted from each other."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fmt_dt(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d %H:%M") + "Z" if value else ""


def _minutes_between(a: Optional[datetime], b: Optional[datetime]) -> Optional[int]:
    a, b = _as_utc(a), _as_utc(b)
    if not a or not b:
        return None
    return int(round((a - b).total_seconds() / 60.0))


def delay_minutes(snapshot: FlightSnapshot) -> Optional[int]:
    """Positive = late. Uses departure first, arrival as fallback."""
    dep = _minutes_between(snapshot.dep_actual_utc, snapshot.dep_scheduled_utc)
    if dep is not None:
        return dep
    return _minutes_between(snapshot.arr_actual_utc, snapshot.arr_scheduled_utc)


# (attribute on Flight, attribute on FlightSnapshot, human label, significant)
_TRACKED_FIELDS: list[tuple[str, str, str, bool]] = [
    ("status", "status", "Status", True),
    ("dep_gate", "dep_gate", "Departure gate", True),
    ("dep_terminal", "dep_terminal", "Departure terminal", True),
    ("arr_gate", "arr_gate", "Arrival gate", False),
    ("arr_terminal", "arr_terminal", "Arrival terminal", False),
    ("arr_baggage_belt", "arr_baggage_belt", "Baggage belt", False),
    ("aircraft_reg", "aircraft_reg", "Aircraft", False),
    ("callsign", "callsign", "Callsign", False),
]

_TRACKED_TIMES: list[tuple[str, str, str]] = [
    ("dep_scheduled_utc", "dep_scheduled_utc", "Scheduled departure"),
    ("dep_actual_utc", "dep_actual_utc", "Expected/actual departure"),
    ("arr_scheduled_utc", "arr_scheduled_utc", "Scheduled arrival"),
    ("arr_actual_utc", "arr_actual_utc", "Expected/actual arrival"),
]


def diff_snapshot(
    flight: Flight, snapshot: FlightSnapshot, time_tolerance_minutes: int = 2
) -> list[Change]:
    """Changes worth recording. Time fields ignore sub-tolerance jitter."""
    changes: list[Change] = []

    for db_attr, snap_attr, label, significant in _TRACKED_FIELDS:
        old = (getattr(flight, db_attr) or "").strip()
        new = (getattr(snapshot, snap_attr) or "").strip()
        # Never report a *loss* of data as a change — providers drop fields.
        if new and new != old:
            changes.append(Change(db_attr, label, old, new, significant))

    for db_attr, snap_attr, label in _TRACKED_TIMES:
        old_dt = _as_utc(getattr(flight, db_attr))
        new_dt = _as_utc(getattr(snapshot, snap_attr))
        if new_dt is None:
            continue
        if old_dt is not None:
            drift = _minutes_between(new_dt, old_dt)
            if drift is not None and abs(drift) <= time_tolerance_minutes:
                continue
        elif db_attr.endswith("actual_utc"):
            # An expected/actual time appearing for the first time is only news
            # if it actually differs from the schedule.
            scheduled = _as_utc(
                getattr(snapshot, db_attr.replace("actual", "scheduled"))
            ) or _as_utc(getattr(flight, db_attr.replace("actual", "scheduled")))
            drift = _minutes_between(new_dt, scheduled)
            if drift is None or abs(drift) <= time_tolerance_minutes:
                continue
        else:
            # First time we learn a scheduled time is not a "change".
            continue
        changes.append(
            Change(db_attr, label, _fmt_dt(old_dt), _fmt_dt(new_dt), True)
        )

    return changes


def summarise(
    flight: Flight, snapshot: FlightSnapshot, changes: list[Change]
) -> str:
    """A short subject line, e.g. 'KL1234 AMS→LHR DELAYED 45min'."""
    route = ""
    if snapshot.dep_iata and snapshot.arr_iata:
        route = f" {snapshot.dep_iata}→{snapshot.arr_iata}"

    status = (snapshot.status or "").upper() or "UPDATE"
    delay = delay_minutes(snapshot)

    if delay is not None and delay >= 15:
        headline = f"{status} +{delay}min"
    elif delay is not None and delay <= -15:
        headline = f"{status} -{abs(delay)}min early"
    else:
        gate_change = next(
            (c for c in changes if c.field == "dep_gate"), None
        )
        status_change = next((c for c in changes if c.field == "status"), None)
        if status_change:
            headline = status
        elif gate_change:
            headline = f"GATE {gate_change.new}"
        else:
            headline = status

    return f"{flight.flight_number}{route} {headline}"


def apply_snapshot(flight: Flight, snapshot: FlightSnapshot) -> None:
    """Make the snapshot the new baseline. Never overwrite known data with blanks."""
    import json

    for _, snap_attr, _, _ in _TRACKED_FIELDS:
        new = (getattr(snapshot, snap_attr) or "").strip()
        if new:
            setattr(flight, snap_attr, new)

    for text_attr in (
        "airline",
        "airline_icao",
        "aircraft_model",
        "dep_iata",
        "dep_name",
        "arr_iata",
        "arr_name",
        "dep_scheduled_local",
        "dep_actual_local",
        "arr_scheduled_local",
        "arr_actual_local",
    ):
        new = (getattr(snapshot, text_attr) or "").strip()
        if new:
            setattr(flight, text_attr, new)

    for coord_attr in ("dep_lat", "dep_lon", "arr_lat", "arr_lon"):
        new_coord = getattr(snapshot, coord_attr)
        if new_coord is not None:
            setattr(flight, coord_attr, new_coord)

    for dt_attr in (
        "dep_scheduled_utc",
        "dep_actual_utc",
        "arr_scheduled_utc",
        "arr_actual_utc",
    ):
        new_dt = getattr(snapshot, dt_attr)
        if new_dt is not None:
            # Store naive UTC — SQLite has no tz-aware storage.
            setattr(flight, dt_attr, new_dt.replace(tzinfo=None))

    if snapshot.raw:
        flight.raw_json = json.dumps(snapshot.raw, indent=2, default=str)
