"""AeroDataBox (via RapidAPI) — flight status by number + date.

Free tier: ~600 units/month, a status call costs 2 units, 1 request/second.
We serialise calls and refuse to spend beyond the configured monthly budget.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from ..config import settings
from .base import (
    FlightNotFound,
    FlightSnapshot,
    ProviderError,
    parse_api_time,
    pretty_local,
)

log = logging.getLogger(__name__)

_BASE_URL = "https://{host}/flights/number/{number}/{date}"


def _airport_location(airport: dict[str, Any] | None) -> tuple[Optional[float], Optional[float]]:
    """AeroDataBox nests coordinates under airport.location when requested."""
    if not airport:
        return None, None
    location = airport.get("location")
    if not isinstance(location, dict):
        return None, None
    lat, lon = location.get("lat"), location.get("lon")
    try:
        return (float(lat), float(lon)) if lat is not None and lon is not None else (None, None)
    except (TypeError, ValueError):
        return None, None


def _time_pair(block: dict[str, Any] | None, *keys: str) -> tuple[Optional[str], str]:
    """Pick the first present time block (e.g. revised before scheduled)."""
    if not block:
        return None, ""
    for key in keys:
        node = block.get(key)
        if isinstance(node, dict) and (node.get("utc") or node.get("local")):
            return node.get("utc"), node.get("local", "")
    return None, ""


class AeroDataBoxProvider:
    name = "aerodatabox"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_call_monotonic = 0.0

    @property
    def configured(self) -> bool:
        return settings.aerodatabox_configured

    @property
    def units_per_call(self) -> int:
        return settings.aerodatabox_units_per_status_call

    async def _throttle(self) -> None:
        """Respect the 1 req/sec free-tier limit."""
        elapsed = time.monotonic() - self._last_call_monotonic
        wait = settings.aerodatabox_min_seconds_between_calls - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call_monotonic = time.monotonic()

    async def fetch(self, flight_number: str, flight_date: str) -> FlightSnapshot:
        if not self.configured:
            raise ProviderError(
                "AERODATABOX_API_KEY is not set — add it to your .env file."
            )

        url = _BASE_URL.format(
            host=settings.aerodatabox_host,
            number=flight_number,
            date=flight_date,
        )
        headers = {
            "x-rapidapi-key": settings.aerodatabox_api_key,
            "x-rapidapi-host": settings.aerodatabox_host,
        }
        params = {
            "withAircraftImage": "false",
            # Airport coordinates, for the Phase 2 map's origin/destination
            # markers and route line. Same endpoint, same unit cost.
            "withLocation": "true",
            "dateLocalRole": "Departure",
        }

        async with self._lock:
            await self._throttle()
            try:
                async with httpx.AsyncClient(timeout=25.0) as client:
                    response = await client.get(url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                raise ProviderError(f"network error talking to AeroDataBox: {exc}")

        if response.status_code == 204 or response.status_code == 404:
            raise FlightNotFound(
                f"AeroDataBox has no record of {flight_number} on {flight_date} yet."
            )
        if response.status_code == 429:
            raise ProviderError("AeroDataBox rate limit / quota hit (HTTP 429).")
        if response.status_code in (401, 403):
            raise ProviderError(
                f"AeroDataBox rejected the API key (HTTP {response.status_code}). "
                "Check AERODATABOX_API_KEY and that you are subscribed to the plan."
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"AeroDataBox returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            payload = response.json()
        except ValueError:
            raise ProviderError("AeroDataBox returned a non-JSON body.")

        # The endpoint returns a list; some plans wrap it in {"flights": [...]}.
        if isinstance(payload, dict):
            payload = payload.get("flights") or payload.get("data") or []
        if not isinstance(payload, list) or not payload:
            raise FlightNotFound(
                f"AeroDataBox has no record of {flight_number} on {flight_date} yet."
            )

        # Prefer the operating carrier's leg over codeshares.
        record = next(
            (
                item
                for item in payload
                if str(item.get("codeshareStatus", "")).lower() == "isoperator"
            ),
            payload[0],
        )
        return self._to_snapshot(record, flight_number)

    @staticmethod
    def _to_snapshot(record: dict[str, Any], flight_number: str) -> FlightSnapshot:
        departure = record.get("departure") or {}
        arrival = record.get("arrival") or {}
        dep_airport = departure.get("airport") or {}
        arr_airport = arrival.get("airport") or {}
        aircraft = record.get("aircraft") or {}
        airline = record.get("airline") or {}

        dep_lat, dep_lon = _airport_location(dep_airport)
        arr_lat, arr_lon = _airport_location(arr_airport)

        dep_sched_utc, dep_sched_local = _time_pair(departure, "scheduledTime")
        # An actual/revised departure: runway time is truth, then revised, then predicted.
        dep_act_utc, dep_act_local = _time_pair(
            departure, "runwayTime", "revisedTime", "predictedTime"
        )
        arr_sched_utc, arr_sched_local = _time_pair(arrival, "scheduledTime")
        arr_act_utc, arr_act_local = _time_pair(
            arrival, "runwayTime", "revisedTime", "predictedTime"
        )

        return FlightSnapshot(
            flight_number=str(record.get("number") or flight_number).replace(" ", ""),
            status=str(record.get("status") or ""),
            callsign=str(record.get("callSign") or ""),
            airline=str(airline.get("name") or ""),
            airline_icao=str(airline.get("icao") or ""),
            aircraft_reg=str(aircraft.get("reg") or ""),
            aircraft_model=str(aircraft.get("model") or ""),
            dep_iata=str(dep_airport.get("iata") or dep_airport.get("icao") or ""),
            dep_name=str(
                dep_airport.get("shortName") or dep_airport.get("name") or ""
            ),
            dep_terminal=str(departure.get("terminal") or ""),
            dep_gate=str(departure.get("gate") or ""),
            dep_scheduled_utc=parse_api_time(dep_sched_utc),
            dep_scheduled_local=pretty_local(dep_sched_local),
            dep_actual_utc=parse_api_time(dep_act_utc),
            dep_actual_local=pretty_local(dep_act_local),
            dep_lat=dep_lat,
            dep_lon=dep_lon,
            arr_iata=str(arr_airport.get("iata") or arr_airport.get("icao") or ""),
            arr_name=str(
                arr_airport.get("shortName") or arr_airport.get("name") or ""
            ),
            arr_terminal=str(arrival.get("terminal") or ""),
            arr_gate=str(arrival.get("gate") or ""),
            arr_baggage_belt=str(arrival.get("baggageBelt") or ""),
            arr_scheduled_utc=parse_api_time(arr_sched_utc),
            arr_scheduled_local=pretty_local(arr_sched_local),
            arr_actual_utc=parse_api_time(arr_act_utc),
            arr_actual_local=pretty_local(arr_act_local),
            arr_lat=arr_lat,
            arr_lon=arr_lon,
            raw=record,
        )


provider = AeroDataBoxProvider()
