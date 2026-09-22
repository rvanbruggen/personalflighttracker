"""Auto-refresh: the fingerprint changes exactly when a page's content does."""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/test7.db"
os.environ["NOTIFICATIONS_ENABLED"] = "false"
os.environ["EMAIL_MAP_ENABLED"] = "false"

DB = "./data/test7.db"
if os.path.exists(DB):
    os.remove(DB)

from fastapi.testclient import TestClient  # noqa: E402

from app import tracker  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Flight, FlightPosition, SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.base import FlightSnapshot  # noqa: E402

init_db()
failures = []


def check(label, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    if not condition:
        failures.append(label)


client = TestClient(app)
now = datetime.now(timezone.utc).replace(tzinfo=None)
with SessionLocal() as s:
    a = Flight(flight_number="RO373", flight_date=now.date().isoformat(), status="Boarding",
               dep_iata="OTP", arr_iata="BRU", dep_gate="12", last_polled_at=now,
               next_poll_at=now + timedelta(minutes=10), dep_scheduled_utc=now + timedelta(minutes=30))
    b = Flight(flight_number="KL1705", flight_date=now.date().isoformat(), status="Expected",
               dep_iata="AMS", arr_iata="LIS", next_poll_at=now + timedelta(hours=5))
    s.add_all([a, b]); s.commit(); aid, bid = a.id, b.id


def fp(flight_id=None):
    q = f"?flight_id={flight_id}" if flight_id else ""
    r = client.get(f"/api/fingerprint{q}")
    return r.json()["fingerprint"], r


print("\n1. Stable when nothing changes")
v1, r = fp(); v2, _ = fp()
check("same value on repeat calls", v1 == v2 and len(v1) == 16, v1)
check("never cached", r.headers.get("cache-control") == "no-store")
d1, _ = fp(aid)
check("flight-scoped value differs from the list's", d1 != v1)

print("\n2. Changes when the page's content changes")
class Stub:
    name = "aerodatabox"
    async def fetch(self, n, d):
        return FlightSnapshot(status="Departed", dep_iata="OTP", arr_iata="BRU", dep_gate="12",
                              dep_scheduled_utc=now + timedelta(minutes=30))
tracker.status_provider = Stub()
asyncio.run(tracker.poll_flight(aid, force=True))
check("a poll that changes status changes the flight page", fp(aid)[0] != d1)
check("...and the list page", fp()[0] != v1)

d2 = fp(aid)[0]
with SessionLocal() as s:
    f = s.get(Flight, aid); f.last_polled_at = f.last_polled_at + timedelta(minutes=1); s.commit()
check("a poll with no changes still updates 'last checked'", fp(aid)[0] != d2)

d3 = fp(aid)[0]
client.post(f"/flights/{aid}/recipients", data={"notify_emails": "anna@example.com"})
check("editing recipients changes it", fp(aid)[0] != d3)

print("\n3. Doesn't change when it shouldn't")
d4, v4 = fp(aid)[0], fp()[0]
with SessionLocal() as s:
    s.add(FlightPosition(flight_id=aid, lat=45.0, lon=20.0, altitude_ft=30000))
    s.commit()
check("a live position fix does NOT reload the flight page (the map handles it)", fp(aid)[0] == d4)
with SessionLocal() as s:
    s.get(Flight, bid).label = "other flight"; s.commit()
check("another flight's change does NOT reload this flight's page", fp(aid)[0] == d4)
check("...but does reload the list", fp()[0] != v4)

print("\n4. Deleted flight")
client.post(f"/flights/{bid}/delete")
check("deleted flight reports 'gone' so its page can go home", fp(bid)[0] == "gone")

print("\n5. Pages wire it up")
html = client.get(f"/flights/{aid}").text
check("flight page embeds its current fingerprint", f'data-fingerprint="{fp(aid)[0]}"' in html)
check("flight page polls the flight-scoped URL", f'data-fingerprint-url="/api/fingerprint?flight_id={aid}"' in html)
index = client.get("/").text
check("list page embeds its fingerprint", f'data-fingerprint="{fp()[0]}"' in index)
check("script included", '/static/autorefresh.js' in index)
check("status note placeholder present", 'id="refresh-note"' in index)
check("interval passed through", f'data-refresh-seconds="{settings.auto_refresh_seconds}"' in index)
check("script is served", client.get("/static/autorefresh.js").status_code == 200)
preview = client.get(f"/flights/{aid}/email-preview?kind=started").text
check("email previews don't auto-refresh", "autorefresh.js" not in preview)

print("\n6. Times shown in the viewer's time zone")
import re  # noqa: E402
from app.main import _localtime  # noqa: E402
out = str(_localtime(datetime(2026, 9, 22, 13, 49)))
check("renders a <time> with the exact UTC moment",
      'datetime="2026-09-22T13:49:00Z"' in out and 'class="local-time"' in out, out)
check("no-JS fallback says UTC explicitly", ">22 Sep 13:49 UTC</time>" in out)
check("missing value renders a dash", str(_localtime(None)) == "—")
page = client.get(f"/flights/{aid}").text
check("flight page uses <time> for last/next check",
      len(re.findall(r'<time class="local-time" datetime="[0-9T:-]+Z">', page)) >= 2)
check("no bare 'Z' timestamps left on the pages",
      not re.search(r"\d{2}:\d{2}Z<", page) and not re.search(r"\d{2}:\d{2}Z<", client.get("/").text))
check("localtime.js included and served",
      "/static/localtime.js" in page and client.get("/static/localtime.js").status_code == 200)

print("\n7. Can be switched off")
settings.auto_refresh_seconds = 0
check("AUTO_REFRESH_SECONDS=0 -> no fingerprint on the page",
      "data-fingerprint=" not in client.get("/").text)
settings.auto_refresh_seconds = 30

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("✅ all auto-refresh checks passed")
