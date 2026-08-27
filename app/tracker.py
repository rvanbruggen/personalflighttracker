"""The adaptive poller: decide when each flight is next due, call the provider,
diff the result, log and alert on changes.

Cadence (per the spec):
  once when registered · hourly from 24h before departure ·
  every 10 min from 2h before · every 5 min while airborne · stop after arrival.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_

from .callsign import resolve_callsign
from .config import settings
from .db import (
    ApiCall,
    Flight,
    FlightEvent,
    FlightPosition,
    SessionLocal,
    quota_period_end,
    units_used_this_period,
    utcnow,
)
from .diffing import apply_snapshot, delay_minutes, diff_snapshot, summarise
from .notify import send_alert
from .providers.adsblol import provider as position_provider
from .providers.aerodatabox import provider as status_provider
from .providers.base import (
    AIRBORNE_STATUSES,
    FlightNotFound,
    FlightSnapshot,
    PositionNotFound,
    ProviderError,
)

# Provider statuses are Title-cased ("EnRoute"); AIRBORNE_STATUSES is lowercase.
AIRBORNE_STATUSES_TITLE = {
    "EnRoute", "En Route", "Departed", "Approaching", "Diverted",
}

log = logging.getLogger(__name__)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _naive(value: datetime) -> datetime:
    """Store naive UTC in SQLite."""
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def next_poll_delay(flight: Flight, snapshot: Optional[FlightSnapshot]) -> timedelta:
    """How long until this flight should be checked again."""
    now = utcnow()

    if snapshot is not None and snapshot.is_airborne:
        return timedelta(minutes=settings.poll_airborne_minutes)

    departure = _aware(flight.dep_scheduled_utc)
    if departure is None:
        # We don't know when it leaves yet — check back periodically.
        return timedelta(minutes=settings.poll_retry_minutes)

    until_departure = departure - now

    if until_departure > timedelta(hours=24):
        # Sleep until the 24h mark, but wake occasionally to catch early
        # cancellations without burning the monthly quota.
        wait = until_departure - timedelta(hours=24)
        return min(wait, timedelta(hours=settings.poll_far_out_hours))

    if until_departure > timedelta(hours=2):
        return timedelta(minutes=settings.poll_within_24h_minutes)

    # Inside 2h, and after the scheduled time while we wait for a departure
    # confirmation, poll at the tight cadence.
    return timedelta(minutes=settings.poll_within_2h_minutes)


def _should_abandon(flight: Flight) -> bool:
    """Give up on a flight that never reported a terminal status."""
    reference = _aware(flight.arr_scheduled_utc) or _aware(flight.dep_scheduled_utc)
    if reference is None:
        # Never resolved at all — fall back to the registered date.
        try:
            reference = datetime.fromisoformat(flight.flight_date).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return False
    return utcnow() > reference + timedelta(hours=settings.poll_abandon_after_hours)


def _record_call(session, endpoint: str, units: int, ok: bool, note: str = "") -> None:
    session.add(
        ApiCall(
            provider=status_provider.name,
            endpoint=endpoint,
            units=units,
            ok=ok,
            note=note[:300],
        )
    )


def quota_status() -> dict:
    with SessionLocal() as session:
        used = units_used_this_period(session, status_provider.name)
    budget = settings.aerodatabox_monthly_unit_budget
    per_call = settings.aerodatabox_units_per_status_call
    resets_at = quota_period_end()
    days_left = max((resets_at - utcnow().replace(tzinfo=None)).days, 0)
    return {
        "used": used,
        "budget": budget,
        "remaining": max(budget - used, 0),
        "calls_remaining": max(budget - used, 0) // max(per_call, 1),
        "percent": round(100 * used / budget, 1) if budget else 0.0,
        "resets_at": resets_at.strftime("%d %b %Y"),
        "days_left": days_left,
        "reset_day": settings.aerodatabox_quota_reset_day,
    }


def _notify(flight: Flight, snapshot: FlightSnapshot, changes: list, subject: str) -> bool:
    """Compose and send the alert. Returns True if anything went out."""
    lines = [
        f"{flight.flight_number}"
        + (f" ({flight.airline})" if flight.airline else "")
        + f"  {flight.flight_date}",
        f"{flight.dep_name or flight.dep_iata} → {flight.arr_name or flight.arr_iata}",
        "",
        "What changed:",
    ]
    lines += [f"  • {change.as_line()}" for change in changes]

    delay = delay_minutes(snapshot)
    lines += ["", "Current state:", f"  Status: {snapshot.status or 'unknown'}"]
    if delay is not None and abs(delay) >= 1:
        word = "late" if delay > 0 else "early"
        lines.append(f"  Delay: {abs(delay)} min {word}")
    if snapshot.dep_scheduled_local:
        lines.append(f"  Departure (local): {snapshot.dep_scheduled_local}")
    if snapshot.dep_actual_local:
        lines.append(f"  Departure expected/actual: {snapshot.dep_actual_local}")
    if snapshot.dep_terminal or snapshot.dep_gate:
        lines.append(
            f"  Departure terminal/gate: {snapshot.dep_terminal or '—'} / "
            f"{snapshot.dep_gate or '—'}"
        )
    if snapshot.arr_scheduled_local:
        lines.append(f"  Arrival (local): {snapshot.arr_scheduled_local}")
    if snapshot.arr_actual_local:
        lines.append(f"  Arrival expected/actual: {snapshot.arr_actual_local}")
    if snapshot.arr_baggage_belt:
        lines.append(f"  Baggage belt: {snapshot.arr_baggage_belt}")
    if snapshot.aircraft_model or snapshot.aircraft_reg:
        lines.append(
            f"  Aircraft: {snapshot.aircraft_model} {snapshot.aircraft_reg}".rstrip()
        )
    if flight.label:
        lines += ["", f"Note: {flight.label}"]

    result = send_alert(subject=subject, body="\n".join(lines), ifttt_subject=subject)
    if result.error:
        log.warning("Alert for %s not fully delivered: %s", flight.flight_number, result.error)
    return result.inbox_sent or result.ifttt_sent


async def poll_flight(flight_id: int, *, force: bool = False) -> str:
    """Poll one flight. Returns a short human-readable outcome."""
    with SessionLocal() as session:
        flight = session.get(Flight, flight_id)
        if flight is None:
            return "flight not found"
        number, date = flight.flight_number, flight.flight_date
        is_first_poll = flight.last_polled_at is None
        used = units_used_this_period(session, status_provider.name)

    budget = settings.aerodatabox_monthly_unit_budget
    cost = settings.aerodatabox_units_per_status_call
    if budget and used + cost > budget and not force:
        log.warning(
            "Monthly AeroDataBox budget reached (%s/%s units) — skipping %s",
            used, budget, number,
        )
        with SessionLocal() as session:
            flight = session.get(Flight, flight_id)
            if flight:
                flight.next_poll_at = _naive(utcnow() + timedelta(hours=6))
                flight.last_error = "monthly API budget reached — polling paused"
                session.commit()
        return "quota exhausted"

    endpoint = f"/flights/number/{number}/{date}"
    try:
        snapshot = await status_provider.fetch(number, date)
    except FlightNotFound as exc:
        with SessionLocal() as session:
            _record_call(session, endpoint, cost, ok=True, note="not found")
            flight = session.get(Flight, flight_id)
            if flight:
                flight.last_polled_at = _naive(utcnow())
                flight.last_error = str(exc)
                flight.consecutive_errors += 1
                if _should_abandon(flight):
                    flight.tracking_state = "abandoned"
                    flight.next_poll_at = None
                    session.add(
                        FlightEvent(
                            flight_id=flight.id,
                            kind="info",
                            summary="Stopped tracking — flight never appeared",
                            detail=str(exc),
                        )
                    )
                else:
                    flight.next_poll_at = _naive(
                        utcnow() + timedelta(minutes=settings.poll_retry_minutes)
                    )
                session.commit()
        return "not found"
    except ProviderError as exc:
        with SessionLocal() as session:
            _record_call(session, endpoint, cost, ok=False, note=str(exc))
            flight = session.get(Flight, flight_id)
            if flight:
                flight.last_polled_at = _naive(utcnow())
                flight.last_error = str(exc)
                flight.consecutive_errors += 1
                # Back off gently on repeated failures.
                backoff = min(flight.consecutive_errors, 6) * settings.poll_retry_minutes
                flight.next_poll_at = _naive(utcnow() + timedelta(minutes=backoff))
                session.commit()
        log.error("Poll failed for %s %s: %s", number, date, exc)
        return f"error: {exc}"

    # --- Success: diff, persist, alert ---
    with SessionLocal() as session:
        _record_call(session, endpoint, cost, ok=True)
        flight = session.get(Flight, flight_id)
        if flight is None:
            session.commit()
            return "flight deleted mid-poll"

        changes = [] if is_first_poll else diff_snapshot(flight, snapshot)
        apply_snapshot(flight, snapshot)
        flight.last_polled_at = _naive(utcnow())
        flight.last_error = ""
        flight.consecutive_errors = 0

        outcome = "no change"
        if is_first_poll:
            session.add(
                FlightEvent(
                    flight_id=flight.id,
                    kind="registered",
                    summary=f"Tracking started — {snapshot.status or 'status unknown'}",
                    detail=(
                        f"{flight.route}  scheduled "
                        f"{snapshot.dep_scheduled_local or '?'} local"
                    ),
                    notified=False,
                )
            )
            outcome = "baseline recorded"
        elif changes:
            subject = summarise(flight, snapshot, changes)
            detail = "\n".join(change.as_line() for change in changes)
            significant = any(change.significant for change in changes)
            notified = False
            if significant:
                notified = await asyncio.to_thread(
                    _notify, flight, snapshot, changes, subject
                )
            session.add(
                FlightEvent(
                    flight_id=flight.id,
                    kind="change",
                    summary=subject,
                    detail=detail,
                    notified=notified,
                )
            )
            outcome = f"{len(changes)} change(s)"

        if snapshot.is_terminal:
            flight.tracking_state = "completed"
            flight.next_poll_at = None
            outcome += " — tracking complete"
        elif _should_abandon(flight):
            flight.tracking_state = "abandoned"
            flight.next_poll_at = None
            outcome += " — abandoned (past arrival, no terminal status)"
        else:
            flight.next_poll_at = _naive(utcnow() + next_poll_delay(flight, snapshot))
            # Airborne: start position polling right away rather than waiting.
            if snapshot.is_airborne and flight.next_position_poll_at is None:
                flight.next_position_poll_at = _naive(utcnow())

        session.commit()
        return outcome


async def tick() -> int:
    """Poll every flight that is due. Called once a minute by the scheduler."""
    now = _naive(utcnow())
    with SessionLocal() as session:
        due_ids = [
            row.id
            for row in session.query(Flight)
            .filter(
                Flight.tracking_state == "active",
                Flight.next_poll_at.isnot(None),
                Flight.next_poll_at <= now,
            )
            .order_by(Flight.next_poll_at)
            .all()
        ]

    for flight_id in due_ids:
        try:
            outcome = await poll_flight(flight_id)
            log.info("Polled flight id=%s: %s", flight_id, outcome)
        except Exception:  # noqa: BLE001 - a bad flight must not kill the loop
            log.exception("Unhandled error polling flight id=%s", flight_id)
    return len(due_ids)


# ------------------------------------------------------------------ positions


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def flight_callsign(flight: Flight):
    """The callsign to look up, plus whether we had to guess it."""
    return resolve_callsign(flight.callsign, flight.flight_number, flight.airline_icao)


async def poll_position(flight_id: int, *, force: bool = False) -> str:
    """Fetch one ADS-B fix for a flight and append it to the trail."""
    if not settings.positions_enabled and not force:
        return "position tracking disabled"

    with SessionLocal() as session:
        flight = session.get(Flight, flight_id)
        if flight is None:
            return "flight not found"
        resolved = flight_callsign(flight)
        last = flight.positions[-1] if flight.positions else None
        last_point = (last.lat, last.lon) if last else None

    if not resolved:
        with SessionLocal() as session:
            flight = session.get(Flight, flight_id)
            if flight:
                flight.position_error = (
                    "no callsign available and none could be derived from the "
                    "flight number — live position unavailable"
                )
                flight.next_position_poll_at = _naive(
                    utcnow() + timedelta(seconds=settings.position_poll_seconds * 5)
                )
                session.commit()
        return "no callsign"

    try:
        fix = await position_provider.fetch_position(resolved.value)
    except PositionNotFound as exc:
        with SessionLocal() as session:
            flight = session.get(Flight, flight_id)
            if flight:
                flight.position_error = str(exc)
                flight.next_position_poll_at = _naive(
                    utcnow() + timedelta(seconds=settings.position_poll_seconds)
                )
                session.commit()
        return "no position (coverage gap)"
    except ProviderError as exc:
        log.warning("Position lookup failed for %s: %s", resolved.value, exc)
        with SessionLocal() as session:
            flight = session.get(Flight, flight_id)
            if flight:
                flight.position_error = str(exc)
                flight.next_position_poll_at = _naive(
                    utcnow() + timedelta(seconds=settings.position_poll_seconds * 3)
                )
                session.commit()
        return f"error: {exc}"

    # Refuse a stale fix rather than drawing the aircraft somewhere it isn't.
    if (
        fix.age_seconds is not None
        and fix.age_seconds > settings.position_max_age_seconds
    ):
        with SessionLocal() as session:
            flight = session.get(Flight, flight_id)
            if flight:
                flight.position_error = (
                    f"last fix is {int(fix.age_seconds)}s old — treating as stale"
                )
                flight.next_position_poll_at = _naive(
                    utcnow() + timedelta(seconds=settings.position_poll_seconds)
                )
                session.commit()
        return "stale fix ignored"

    moved_km = (
        _haversine_km(last_point[0], last_point[1], fix.lat, fix.lon)
        if last_point
        else None
    )

    with SessionLocal() as session:
        flight = session.get(Flight, flight_id)
        if flight is None:
            return "flight deleted mid-poll"

        appended = False
        if moved_km is None or moved_km >= settings.position_min_move_km:
            session.add(
                FlightPosition(
                    flight_id=flight.id,
                    lat=fix.lat,
                    lon=fix.lon,
                    altitude_ft=fix.altitude_ft,
                    ground_speed_kt=fix.ground_speed_kt,
                    track_deg=fix.track_deg,
                    on_ground=fix.on_ground,
                    source=fix.source,
                )
            )
            appended = True

        flight.last_position_at = _naive(utcnow())
        flight.position_source = fix.source
        flight.position_error = ""
        # Record a provider-confirmed callsign so we stop guessing next time.
        if not flight.callsign and not resolved.derived:
            flight.callsign = resolved.value
        if fix.registration and not flight.aircraft_reg:
            flight.aircraft_reg = fix.registration
        flight.next_position_poll_at = _naive(
            utcnow() + timedelta(seconds=settings.position_poll_seconds)
        )
        session.commit()

    if appended:
        return f"fix recorded ({fix.lat:.3f}, {fix.lon:.3f}) at {fix.altitude_ft or '?'}ft"
    return f"unchanged (moved {moved_km:.2f}km, below threshold)"


async def position_tick() -> int:
    """Poll positions for every airborne flight that is due."""
    if not settings.positions_enabled:
        return 0

    now = _naive(utcnow())
    with SessionLocal() as session:
        due_ids = [
            row.id
            for row in session.query(Flight)
            .filter(
                Flight.tracking_state == "active",
                Flight.status.in_(list(AIRBORNE_STATUSES_TITLE)),
                or_(
                    Flight.next_position_poll_at.is_(None),
                    Flight.next_position_poll_at <= now,
                ),
            )
            .all()
        ]

    for flight_id in due_ids:
        try:
            outcome = await poll_position(flight_id)
            log.info("Position poll flight id=%s: %s", flight_id, outcome)
        except Exception:  # noqa: BLE001
            log.exception("Unhandled error polling position for id=%s", flight_id)
    return len(due_ids)
