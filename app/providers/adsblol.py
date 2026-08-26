"""adsb.lol — live aircraft position by callsign.

Community-run ADS-B aggregator, ADSB-Exchange-compatible, no API key needed
today (the project has signalled a key may be required in future — which is why
it sits behind the PositionProvider interface like every other source).

Free and effectively unmetered, so position polling never touches the
AeroDataBox budget. We still throttle to stay a polite client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from ..config import settings
from .base import PositionFix, PositionNotFound, ProviderError

log = logging.getLogger(__name__)


def _as_int(value: Any) -> Optional[int]:
    """ADS-B feeds send numbers as strings, and alt_baro as 'ground'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text or text.lower() == "ground":
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


class AdsbLolProvider:
    name = "adsb.lol"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_call_monotonic = 0.0

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_monotonic
        wait = settings.adsblol_min_seconds_between_calls - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call_monotonic = time.monotonic()

    async def fetch_position(self, callsign: str) -> PositionFix:
        clean = (callsign or "").strip().upper()
        if not clean:
            raise ProviderError("no callsign to look up")

        url = f"{settings.adsblol_base_url.rstrip('/')}/callsign/{clean}"

        async with self._lock:
            await self._throttle()
            try:
                async with httpx.AsyncClient(
                    timeout=settings.adsblol_timeout_seconds,
                    headers={
                        "Accept": "application/json",
                        # Required: adsb.lol 403s generic agents.
                        "User-Agent": settings.adsblol_user_agent,
                    },
                ) as client:
                    response = await client.get(url)
            except httpx.HTTPError as exc:
                raise ProviderError(f"network error talking to adsb.lol: {exc}")

        if response.status_code == 404:
            raise PositionNotFound(f"adsb.lol has no aircraft for {clean}")
        if response.status_code == 429:
            raise ProviderError("adsb.lol rate limit hit (HTTP 429)")
        if response.status_code == 403:
            raise ProviderError(
                f"adsb.lol rejected the request (HTTP 403): {response.text[:120]} "
                "— set ADSBLOL_CONTACT in .env to a URL or email they can reach you at."
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"adsb.lol returned HTTP {response.status_code}: {response.text[:160]}"
            )

        try:
            payload = response.json()
        except ValueError:
            raise ProviderError("adsb.lol returned a non-JSON body")

        aircraft = payload.get("ac") or payload.get("aircraft") or []
        if not isinstance(aircraft, list) or not aircraft:
            raise PositionNotFound(
                f"no receiver is currently seeing {clean} — normal over oceans "
                "and in areas with sparse ADS-B coverage"
            )

        # Prefer an entry that actually carries a position fix.
        record = next(
            (a for a in aircraft if a.get("lat") is not None and a.get("lon") is not None),
            None,
        )
        if record is None:
            raise PositionNotFound(f"{clean} is being tracked but reports no position")

        lat, lon = _as_float(record.get("lat")), _as_float(record.get("lon"))
        if lat is None or lon is None:
            raise PositionNotFound(f"{clean} returned an unusable position")

        altitude = _as_int(record.get("alt_baro"))
        if altitude is None:
            altitude = _as_int(record.get("alt_geom"))

        return PositionFix(
            lat=lat,
            lon=lon,
            altitude_ft=altitude,
            ground_speed_kt=_as_int(record.get("gs")),
            track_deg=_as_int(record.get("track")),
            on_ground=str(record.get("alt_baro", "")).lower() == "ground",
            callsign=str(record.get("flight") or clean).strip(),
            registration=str(record.get("r") or "").strip(),
            icao24=str(record.get("hex") or "").strip(),
            source=self.name,
            age_seconds=_as_float(record.get("seen_pos")),
            raw=record,
        )


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


provider = AdsbLolProvider()
