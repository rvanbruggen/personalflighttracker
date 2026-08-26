"""Parsing and error handling for the AeroDataBox provider — no network calls."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AERODATABOX_API_KEY", "test-key")

import httpx  # noqa: E402

from app.providers.aerodatabox import AeroDataBoxProvider  # noqa: E402
from app.providers.base import FlightNotFound, ProviderError  # noqa: E402

# Shape per AeroDataBox /flights/number/{number}/{date}
SAMPLE = [
    {
        "number": "KL 1705",
        "callSign": "KLM1705",
        "status": "Delayed",
        "codeshareStatus": "IsCodeshared",
        "airline": {"name": "Delta", "iata": "DL"},
        "departure": {"airport": {"iata": "AMS"}, "scheduledTime": {"utc": "2026-08-27 14:30Z"}},
        "arrival": {"airport": {"iata": "LIS"}},
    },
    {
        "number": "KL 1705",
        "callSign": "KLM1705",
        "status": "Delayed",
        "codeshareStatus": "IsOperator",
        "isCargo": False,
        "aircraft": {"reg": "PH-BXA", "modeS": "484144", "model": "Boeing 737-800"},
        "airline": {"name": "KLM", "iata": "KL", "icao": "KLM"},
        "departure": {
            "airport": {"iata": "AMS", "icao": "EHAM", "shortName": "Schiphol",
                        "name": "Amsterdam Schiphol"},
            "scheduledTime": {"utc": "2026-08-27 14:30Z", "local": "2026-08-27 16:30+02:00"},
            "revisedTime": {"utc": "2026-08-27 15:15Z", "local": "2026-08-27 17:15+02:00"},
            "terminal": "2",
            "gate": "E18",
        },
        "arrival": {
            "airport": {"iata": "LIS", "icao": "LPPT", "shortName": "Humberto Delgado"},
            "scheduledTime": {"utc": "2026-08-27 17:00Z", "local": "2026-08-27 18:00+01:00"},
            "predictedTime": {"utc": "2026-08-27 17:40Z", "local": "2026-08-27 18:40+01:00"},
            "terminal": "1",
            "baggageBelt": "7",
        },
    },
]

failures = []


def check(label, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    if not condition:
        failures.append(label)


def run_with_response(response: httpx.Response):
    """Drive provider.fetch against a stubbed HTTP transport."""
    provider = AeroDataBoxProvider()
    transport = httpx.MockTransport(lambda request: response)
    original = httpx.AsyncClient

    class PatchedClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = PatchedClient
    try:
        return asyncio.run(provider.fetch("KL1705", "2026-08-27"))
    finally:
        httpx.AsyncClient = original


print("\n1. Parsing a realistic response")
snapshot = run_with_response(httpx.Response(200, json=SAMPLE))
check("picks the operating carrier over the codeshare", snapshot.airline == "KLM", snapshot.airline)
check("flight number stripped of spaces", snapshot.flight_number == "KL1705", snapshot.flight_number)
check("callsign extracted (feeds phase 2 position lookup)", snapshot.callsign == "KLM1705")
check("status", snapshot.status == "Delayed")
check("aircraft", snapshot.aircraft_model == "Boeing 737-800" and snapshot.aircraft_reg == "PH-BXA")
check("departure airport", snapshot.dep_iata == "AMS" and snapshot.dep_name == "Schiphol")
check("terminal/gate", snapshot.dep_terminal == "2" and snapshot.dep_gate == "E18")
check("scheduled dep parsed to UTC",
      snapshot.dep_scheduled_utc.isoformat() == "2026-08-27T14:30:00+00:00",
      str(snapshot.dep_scheduled_utc))
check("revisedTime wins as expected departure",
      snapshot.dep_actual_utc.isoformat() == "2026-08-27T15:15:00+00:00",
      str(snapshot.dep_actual_utc))
check("local time rendered without offset",
      snapshot.dep_scheduled_local == "2026-08-27 16:30", snapshot.dep_scheduled_local)
check("arrival predictedTime used as expected arrival",
      snapshot.arr_actual_utc.isoformat() == "2026-08-27T17:40:00+00:00")
check("baggage belt", snapshot.arr_baggage_belt == "7")
check("not airborne when Delayed", snapshot.is_airborne is False)
check("not terminal when Delayed", snapshot.is_terminal is False)

print("\n2. Airborne / terminal status detection")
for status, airborne, terminal in [
    ("EnRoute", True, False), ("Departed", True, False), ("Approaching", True, False),
    ("Arrived", False, True), ("Canceled", False, True), ("Expected", False, False),
]:
    snap = run_with_response(httpx.Response(200, json=[{**SAMPLE[1], "status": status}]))
    check(f"{status}: airborne={airborne} terminal={terminal}",
          snap.is_airborne == airborne and snap.is_terminal == terminal)

print("\n3. Error handling")
for code, exc_type, label in [
    (204, FlightNotFound, "204 no content -> FlightNotFound"),
    (404, FlightNotFound, "404 -> FlightNotFound"),
    (401, ProviderError, "401 -> ProviderError (bad key)"),
    (429, ProviderError, "429 -> ProviderError (quota)"),
    (500, ProviderError, "500 -> ProviderError"),
]:
    try:
        run_with_response(httpx.Response(code, json=[] if code == 204 else {}))
        check(label, False, "no exception raised")
    except exc_type as exc:
        check(label, True, str(exc)[:60])
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"wrong type {type(exc).__name__}")

try:
    run_with_response(httpx.Response(200, json=[]))
    check("empty list -> FlightNotFound", False, "no exception")
except FlightNotFound:
    check("empty list -> FlightNotFound", True)

try:
    run_with_response(httpx.Response(200, json={"flights": SAMPLE}))
    check("dict-wrapped payload handled", True)
except Exception as exc:  # noqa: BLE001
    check("dict-wrapped payload handled", False, str(exc))

print("\n4. Missing key is a clear error, not a crash")
from app.config import settings  # noqa: E402
original_key = settings.aerodatabox_api_key
settings.aerodatabox_api_key = ""
try:
    asyncio.run(AeroDataBoxProvider().fetch("KL1705", "2026-08-27"))
    check("unconfigured -> ProviderError", False)
except ProviderError as exc:
    check("unconfigured -> ProviderError", "AERODATABOX_API_KEY" in str(exc), str(exc))
settings.aerodatabox_api_key = original_key

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("✅ all provider checks passed")
