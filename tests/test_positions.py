"""Phase 2: adsb.lol parsing, callsign resolution, trail building, map endpoint."""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/test3.db"
os.environ["NOTIFICATIONS_ENABLED"] = "false"

DB = "./data/test3.db"
if os.path.exists(DB):
    os.remove(DB)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import tracker  # noqa: E402
from app.callsign import derive_callsign, resolve_callsign  # noqa: E402
from app.db import Flight, FlightPosition, SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.adsblol import AdsbLolProvider  # noqa: E402
from app.providers.base import PositionFix, PositionNotFound, ProviderError  # noqa: E402

init_db()
failures = []


def check(label, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    if not condition:
        failures.append(label)


# Real adsb.lol /v2/callsign shape (readsb/tar1090 field names).
SAMPLE = {
    "ac": [
        {
            "hex": "406b3f", "type": "adsb_icao", "flight": "BAW117  ",
            "r": "G-STBA", "t": "B77W",
            "alt_baro": 37000, "alt_geom": 38050, "gs": 512.3, "track": 291.4,
            "baro_rate": 0, "lat": 51.8823, "lon": -14.2291,
            "seen_pos": 3.1, "seen": 0.4, "messages": 48213, "rssi": -21.3,
        }
    ],
    "total": 1, "now": 1756213800000,
}


def run_with(response: httpx.Response, callsign="BAW117"):
    provider = AdsbLolProvider()
    transport = httpx.MockTransport(lambda request: response)
    original = httpx.AsyncClient

    class Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    httpx.AsyncClient = Patched
    try:
        return asyncio.run(provider.fetch_position(callsign))
    finally:
        httpx.AsyncClient = original


print("\n1. Parsing a real adsb.lol response")
fix = run_with(httpx.Response(200, json=SAMPLE))
check("latitude", abs(fix.lat - 51.8823) < 1e-6, str(fix.lat))
check("longitude", abs(fix.lon - (-14.2291)) < 1e-6, str(fix.lon))
check("altitude", fix.altitude_ft == 37000, str(fix.altitude_ft))
check("ground speed rounded to int", fix.ground_speed_kt == 512, str(fix.ground_speed_kt))
check("track", fix.track_deg == 291, str(fix.track_deg))
check("callsign trimmed of padding", fix.callsign == "BAW117", repr(fix.callsign))
check("registration", fix.registration == "G-STBA")
check("icao24 hex", fix.icao24 == "406b3f")
check("fix age", fix.age_seconds == 3.1)
check("not on ground", fix.on_ground is False)
check("source labelled", fix.source == "adsb.lol")

print("\n2. Awkward real-world values")
ground = run_with(httpx.Response(200, json={"ac": [
    {"flight": "BAW117", "lat": 51.47, "lon": -0.45, "alt_baro": "ground", "gs": "0", "seen_pos": 1}
]}))
check("alt_baro 'ground' handled, not crashed", ground.altitude_ft is None, str(ground.altitude_ft))
check("on_ground flag set", ground.on_ground is True)
check("string ground speed coerced", ground.ground_speed_kt == 0)

fallback = run_with(httpx.Response(200, json={"ac": [
    {"flight": "X", "lat": 1.0, "lon": 2.0, "alt_geom": 12000, "seen_pos": 1}
]}))
check("falls back to alt_geom when alt_baro missing", fallback.altitude_ft == 12000)

print("\n3. Coverage gaps and errors are distinguishable")
for payload, label in [
    ({"ac": [], "total": 0}, "empty ac list -> PositionNotFound"),
    ({"ac": [{"flight": "BAW117", "seen": 2}]}, "aircraft without lat/lon -> PositionNotFound"),
]:
    try:
        run_with(httpx.Response(200, json=payload))
        check(label, False, "no exception")
    except PositionNotFound:
        check(label, True)
    except Exception as exc:  # noqa: BLE001
        check(label, False, type(exc).__name__)

for code, exc_type, label in [
    (404, PositionNotFound, "404 -> PositionNotFound"),
    (429, ProviderError, "429 -> ProviderError"),
    (503, ProviderError, "503 -> ProviderError"),
]:
    try:
        run_with(httpx.Response(code, json={}))
        check(label, False, "no exception")
    except exc_type:
        check(label, True)
    except Exception as exc:  # noqa: BLE001
        check(label, False, type(exc).__name__)

print("\n4. Callsign resolution")
check("provider value wins", resolve_callsign("KLM1705", "KL1705", "KLM").derived is False)
check("derives from airline ICAO", derive_callsign("BA117", "BAW") == "BAW117")
check("drops leading zeros", derive_callsign("BA0117", "BAW") == "BAW117")
check("falls back to IATA table", derive_callsign("SN2103", "") == "BEL2103")
check("unknown airline -> no guess", resolve_callsign("", "ZZ4321", "") is None)

print("\n5. Trail building through the tracker")
now = datetime.now(timezone.utc).replace(tzinfo=None)
with SessionLocal() as s:
    f = Flight(flight_number="BA117", flight_date="2026-08-26", status="EnRoute",
               callsign="BAW117", airline_icao="BAW", dep_iata="LHR", arr_iata="JFK",
               dep_lat=51.47, dep_lon=-0.4543, arr_lat=40.6398, arr_lon=-73.7789,
               next_poll_at=now)
    s.add(f); s.commit(); fid = f.id


class StubPositions:
    name = "adsb.lol"

    def __init__(self, fixes):
        self.fixes = list(fixes)
        self.calls = 0

    async def fetch_position(self, callsign):
        self.calls += 1
        item = self.fixes.pop(0) if self.fixes else PositionNotFound("gap")
        if isinstance(item, Exception):
            raise item
        return item


def fix_at(lat, lon, alt=37000, age=2.0):
    return PositionFix(lat=lat, lon=lon, altitude_ft=alt, ground_speed_kt=500,
                       track_deg=290, source="adsb.lol", age_seconds=age,
                       callsign="BAW117", registration="G-STBA")


tracker.position_provider = StubPositions([
    fix_at(51.4, -1.0),
    fix_at(51.4, -1.0),           # same spot -> should not extend the trail
    fix_at(52.0, -20.0),          # moved -> should extend
    PositionNotFound("no receiver coverage mid-Atlantic"),
    fix_at(45.0, -50.0, age=900), # stale -> must be rejected
])

out = asyncio.run(tracker.poll_position(fid, force=True))
check("first fix recorded", "fix recorded" in out, out)
out = asyncio.run(tracker.poll_position(fid, force=True))
check("stationary fix does not duplicate a trail point", "unchanged" in out, out)
out = asyncio.run(tracker.poll_position(fid, force=True))
check("moved fix extends the trail", "fix recorded" in out, out)
out = asyncio.run(tracker.poll_position(fid, force=True))
check("coverage gap reported, not fatal", "coverage gap" in out, out)
out = asyncio.run(tracker.poll_position(fid, force=True))
check("stale fix rejected", "stale" in out, out)

with SessionLocal() as s:
    f = s.get(Flight, fid)
    check("trail has exactly 2 points", len(f.positions) == 2, f"{len(f.positions)}")
    check("registration backfilled from ADS-B", f.aircraft_reg == "G-STBA", f.aircraft_reg)
    check("position source recorded", f.position_source == "adsb.lol")
    check("trail is time-ordered",
          [p.lon for p in f.positions] == sorted([p.lon for p in f.positions], reverse=True)
          or f.positions[0].recorded_at <= f.positions[1].recorded_at)

print("\n6. Position polling only targets airborne flights")
with SessionLocal() as s:
    # A non-airborne flight that is otherwise due...
    s.add(Flight(flight_number="KL1705", flight_date="2026-08-26", status="Expected",
                 callsign="KLM1705", next_poll_at=now, next_position_poll_at=now))
    # ...and make the airborne one due again (it was throttled by the polls above).
    s.get(Flight, fid).next_position_poll_at = now - timedelta(seconds=1)
    s.commit()
tracker.position_provider = StubPositions([fix_at(50.0, 4.0)])
polled = asyncio.run(tracker.position_tick())
check("only the EnRoute flight was due", polled == 1, f"{polled} flights polled")

print("\n7. A flight with no resolvable callsign degrades gracefully")
with SessionLocal() as s:
    f = Flight(flight_number="ZZ4321", flight_date="2026-08-26", status="EnRoute",
               next_poll_at=now)
    s.add(f); s.commit(); nofid = f.id
out = asyncio.run(tracker.poll_position(nofid, force=True))
check("reports 'no callsign' instead of crashing", out == "no callsign", out)
with SessionLocal() as s:
    check("reason stored for the UI", "callsign" in s.get(Flight, nofid).position_error.lower())

print("\n8. Map endpoint")
client = TestClient(app)
r = client.get(f"/api/flights/{fid}/track")
check("GET /track -> 200", r.status_code == 200)
data = r.json()
with SessionLocal() as s:
    stored = len(s.get(Flight, fid).positions)
check("exposes the full stored trail", len(data["trail"]) == stored,
      f"api {len(data['trail'])} vs db {stored}")
check("trail has more than one point", len(data["trail"]) > 1, str(len(data["trail"])))
check("exposes latest fix", data["latest"] is not None)
check("marks flight airborne", data["airborne"] is True)
check("includes both airport coordinates",
      data["departure"]["lat"] == 51.47 and data["arrival"]["lat"] == 40.6398)
check("callsign not flagged as derived", data["callsign_derived"] is False)
check("404 for unknown flight", client.get("/api/flights/99999/track").status_code == 404)

print("\n9. Detail page renders the map")
html = client.get(f"/flights/{fid}").text
check("map container present", 'id="map"' in html)
check("leaflet css loaded", "leaflet@1.9.4/dist/leaflet.css" in html)
check("map.js loaded", "/static/map.js" in html)
check("integrity hash on leaflet js", 'integrity="sha256-' in html)

print("\n10. Derived callsign is disclosed in the UI")
with SessionLocal() as s:
    f = Flight(flight_number="BA999", flight_date="2026-08-26", status="EnRoute",
               callsign="", airline_icao="BAW", next_poll_at=now)
    s.add(f); s.commit(); dfid = f.id
html = client.get(f"/flights/{dfid}").text
check("page warns the callsign was derived", "derived from the flight number" in html)
check("names the derived callsign", "BAW999" in html)
track = client.get(f"/api/flights/{dfid}/track").json()
check("api flags callsign_derived", track["callsign_derived"] is True)

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("✅ all position checks passed")
