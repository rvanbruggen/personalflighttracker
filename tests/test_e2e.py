"""End-to-end exercise of the tracker with a stubbed provider and mailer.
Run: .venv/bin/python -m pytest tests -q   (or just: python tests/test_e2e.py)
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/test.db"
os.environ["NOTIFICATIONS_ENABLED"] = "false"

DB_PATH = "./data/test.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

from fastapi.testclient import TestClient  # noqa: E402

from app import tracker  # noqa: E402
from app.db import Flight, SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.base import FlightSnapshot  # noqa: E402

init_db()

SENT: list[tuple[str, str]] = []


def fake_notify(flight, snapshot, changes, subject):
    SENT.append((subject, "\n".join(c.as_line() for c in changes)))
    return True


tracker._notify = fake_notify

DEPARTURE = datetime.now(timezone.utc) + timedelta(hours=30)


def snap(**overrides) -> FlightSnapshot:
    base = dict(
        flight_number="KL1234",
        status="Expected",
        callsign="KLM1234",
        airline="KLM",
        aircraft_reg="PH-BXA",
        aircraft_model="Boeing 737-800",
        dep_iata="AMS",
        dep_name="Amsterdam Schiphol",
        dep_terminal="2",
        dep_gate="D5",
        dep_scheduled_utc=DEPARTURE,
        dep_scheduled_local=DEPARTURE.strftime("%Y-%m-%d %H:%M"),
        arr_iata="LIS",
        arr_name="Lisbon",
        arr_scheduled_utc=DEPARTURE + timedelta(hours=3),
        arr_scheduled_local=(DEPARTURE + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"),
        raw={"number": "KL 1234"},
    )
    base.update(overrides)
    return FlightSnapshot(**base)


NEXT_SNAPSHOT = snap()


class StubProvider:
    name = "aerodatabox"
    calls = 0

    async def fetch(self, number, date):
        StubProvider.calls += 1
        return NEXT_SNAPSHOT


tracker.status_provider = StubProvider()

client = TestClient(app)
failures = []


def check(label, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    if not condition:
        failures.append(label)


print("\n1. Register a flight (first poll = baseline, no alert)")
date_str = DEPARTURE.date().isoformat()
response = client.post(
    "/flights",
    data={"flight_number": "kl 1234", "flight_date": date_str, "label": "to Lisbon"},
    follow_redirects=False,
)
check("registration redirects to detail page", response.status_code == 303)
with SessionLocal() as s:
    flight = s.query(Flight).filter_by(flight_number="KL1234").one()
    fid = flight.id
    check("flight number normalised", flight.flight_number == "KL1234")
    check("baseline status stored", flight.status == "Expected", flight.status)
    check("gate stored", flight.dep_gate == "D5")
    check("callsign captured for phase 2", flight.callsign == "KLM1234")
    check("no alert on first poll", len(SENT) == 0)
    check("event logged as 'registered'", flight.events[0].kind == "registered")
    next_poll = flight.next_poll_at
    hours_out = (next_poll - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() / 3600
    check("30h out -> next poll ~6h away (at the 24h mark)", 5.5 < hours_out < 6.5,
          f"{hours_out:.1f}h")

print("\n2. Identical snapshot -> no change, no alert")
import asyncio  # noqa: E402
asyncio.run(tracker.poll_flight(fid, force=True))
check("still no alerts", len(SENT) == 0)

print("\n3. Gate change + 45 min delay -> one alert")
NEXT_SNAPSHOT = snap(
    status="Delayed",
    dep_gate="E18",
    dep_actual_utc=DEPARTURE + timedelta(minutes=45),
    dep_actual_local=(DEPARTURE + timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M"),
)
asyncio.run(tracker.poll_flight(fid, force=True))
check("alert sent", len(SENT) == 1)
if SENT:
    subject, detail = SENT[0]
    check("subject names flight + route + delay",
          "KL1234" in subject and "AMS→LIS" in subject and "+45min" in subject, subject)
    check("detail lists the gate change", "E18" in detail, detail.replace("\n", " | "))

print("\n4. Airborne -> 5 minute cadence")
NEXT_SNAPSHOT = snap(
    status="EnRoute",
    dep_gate="E18",
    dep_actual_utc=DEPARTURE + timedelta(minutes=45),
    dep_actual_local=(DEPARTURE + timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M"),
)
asyncio.run(tracker.poll_flight(fid, force=True))
with SessionLocal() as s:
    flight = s.get(Flight, fid)
    minutes = (flight.next_poll_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() / 60
    check("airborne -> ~5 min", 4 < minutes < 6, f"{minutes:.1f} min")
    check("still active", flight.tracking_state == "active")
check("status change alerted", len(SENT) == 2)

print("\n5. Arrived -> tracking completes, polling stops")
NEXT_SNAPSHOT = snap(
    status="Arrived",
    dep_gate="E18",
    dep_actual_utc=DEPARTURE + timedelta(minutes=45),
    arr_actual_utc=DEPARTURE + timedelta(hours=3, minutes=30),
    arr_actual_local=(DEPARTURE + timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d %H:%M"),
    arr_baggage_belt="7",
)
asyncio.run(tracker.poll_flight(fid, force=True))
with SessionLocal() as s:
    flight = s.get(Flight, fid)
    check("tracking_state == completed", flight.tracking_state == "completed")
    check("next_poll_at cleared", flight.next_poll_at is None)
    check("baggage belt recorded", flight.arr_baggage_belt == "7")

print("\n6. Due-flight scheduler tick skips completed flights")
polled = asyncio.run(tracker.tick())
check("tick polls nothing", polled == 0, f"{polled} polled")

print("\n7. Quota accounting")
quota = tracker.quota_status()
expected_units = StubProvider.calls * 2
check("units counted per call", quota["used"] == expected_units,
      f"{quota['used']} used for {StubProvider.calls} calls")
check("remaining computed", quota["remaining"] == 600 - expected_units)

print("\n8. Web pages render")
for path in ("/", f"/flights/{fid}", "/healthz", "/api/flights"):
    r = client.get(path)
    check(f"GET {path} -> 200", r.status_code == 200, f"got {r.status_code}")
check("index lists the flight", "KL1234" in client.get("/").text)
check("detail shows change history", "E18" in client.get(f"/flights/{fid}").text)

print("\n9. Validation rejects junk")
r = client.post("/flights", data={"flight_number": "!!!", "flight_date": date_str},
                follow_redirects=False)
check("bad flight number rejected", "not+a+valid" in r.headers.get("location", "")
      or "not%20a%20valid" in r.headers.get("location", ""), r.headers.get("location", ""))
r = client.post("/flights", data={"flight_number": "KL1234", "flight_date": "1999-01-01"},
                follow_redirects=False)
check("past date rejected", "past" in r.headers.get("location", "").lower())
r = client.post("/flights", data={"flight_number": "KL1234", "flight_date": date_str},
                follow_redirects=False)
check("duplicate detected", "already" in r.headers.get("location", "").lower())

print("\n10. Duplicate flight is not double-registered")
with SessionLocal() as s:
    count = s.query(Flight).filter_by(flight_number="KL1234").count()
    check("exactly one KL1234 row", count == 1, f"{count} rows")

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("✅ all checks passed")
