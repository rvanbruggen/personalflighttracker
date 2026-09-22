"""Designed HTML emails: MIME structure, headlines, next-update wording,
localised times, the static map, and the preview page. No network."""

import asyncio
import html as html_lib
import io
import os
import smtplib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/test6.db"
os.environ["MAIL_TO"] = "rik@example.com"
os.environ["SMTP_USER"] = "rik@example.com"
os.environ["SMTP_PASSWORD"] = "app-password"
os.environ["NOTIFICATIONS_ENABLED"] = "true"
os.environ["IFTTT_ENABLED"] = "true"
os.environ["TIMEZONE"] = "Europe/Brussels"
os.environ["MAP_TILE_CACHE_DIR"] = tempfile.mkdtemp(prefix="pft-tiles-")

DB = "./data/test6.db"
if os.path.exists(DB):
    os.remove(DB)

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from app import notify, staticmap, tracker  # noqa: E402
from app.cadence import NextStep, interval_for, phase_for  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Flight, FlightPosition, SessionLocal, init_db  # noqa: E402
from app.email_render import (  # noqa: E402
    FlightView, build_email, fmt_change, headline, next_update, pretty_status,
)
from app.providers.base import FlightSnapshot  # noqa: E402

init_db()
failures = []


def check(label, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    if not condition:
        failures.append(label)


# Never touch real tile servers: serve a flat tile, count requests.
TILE_CALLS = []
real_fetch_tile = staticmap.fetch_tile


def fake_tile(z, x, y, client=None):
    TILE_CALLS.append((z, x, y))
    return Image.new("RGB", (256, 256), (170, 211, 223))


staticmap.fetch_tile = fake_tile

SENT = []


class FakeSMTP:
    def __init__(self, host, port, timeout=None): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def starttls(self, context=None): pass
    def login(self, u, p): pass
    def send_message(self, m): SENT.append(m)


smtplib.SMTP = FakeSMTP
NOW = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)  # 12:00 in Brussels

# ---------------------------------------------------------------- MIME
print("\n1. MIME structure")
SENT.clear()
rendered = build_email("test")
notify.send_alert("KL1 test", "plain body", html=rendered.html, images=rendered.images,
                  extra_recipients=["anna@example.com"])
inbox = next(m for m in SENT if m["To"] == "rik@example.com")
check("top level is multipart/alternative", inbox.get_content_type() == "multipart/alternative")
parts = inbox.get_payload()
check("plain text first, then related HTML",
      parts[0].get_content_type() == "text/plain" and parts[1].get_content_type() == "multipart/related")
related = parts[1].get_payload()
check("HTML part inside related", related[0].get_content_type() == "text/html")
cids = {p["Content-ID"] for p in related[1:]}
check("logo embedded inline with a Content-ID", "<logo>" in cids, str(cids))
check("inline, not an attachment", all(p.get_content_disposition() == "inline" for p in related[1:]))
check("HTML references the logo by cid", "cid:logo" in related[0].get_content())
check("plain text preserved", parts[0].get_content().strip() == "plain body")
check("HTML under Gmail's 102 KB clipping limit", len(rendered.html) < 100_000, f"{len(rendered.html)} bytes")
extra_html = next(m for m in SENT if m["To"] == "anna@example.com").get_body(("html",)).get_content()
inbox_html = inbox.get_body(("html",)).get_content()
check("extra recipient's HTML has the 'why am I receiving this' footer", "added as a recipient" in extra_html)
check("default recipient's HTML does not", "added as a recipient" not in inbox_html)
check("no email copy goes to IFTTT (the phone push is a webhook now)",
      all(m["To"] != "trigger@applet.ifttt.com" for m in SENT))

# ------------------------------------------------------------ headlines
print("\n2. Headlines")
base = dict(flight_number="SN3811", arr_name="Lisbon", arr_iata="LIS",
            dep_scheduled_utc=datetime(2026, 9, 22, 10, 0), arr_scheduled_utc=datetime(2026, 9, 22, 12, 30))
cases = [
    (dict(status="Canceled"), [("Status", "Expected", "Canceled")], "SN3811 has been cancelled", "bad"),
    (dict(status="Diverted"), [("Status", "EnRoute", "Diverted")], "SN3811 has been diverted", "bad"),
    (dict(status="Arrived", arr_actual_utc=datetime(2026, 9, 22, 13, 5)),
     [("Status", "Approaching", "Arrived")], "SN3811 has landed in Lisbon, 35 min late", "good"),
    (dict(status="Departed", dep_actual_utc=datetime(2026, 9, 22, 11, 10)),
     [("Status", "Boarding", "Departed")], "SN3811 has departed, 1 h 10 min late", "moving"),
    (dict(status="Delayed", dep_actual_utc=datetime(2026, 9, 22, 10, 45)),
     [("Expected/actual departure", "", "x")], "SN3811 is running 45 min late", "delay"),
    (dict(status="Expected"), [("Departure gate", "D5", "E18")], "SN3811 now departs from gate E18", "gate"),
    (dict(status="Expected"), [("Departure terminal", "1", "2")], "SN3811 now departs from terminal 2", "gate"),
    (dict(status="Boarding"), [("Status", "Expected", "Boarding")], "SN3811 is boarding", "moving"),
    (dict(status="Expected"), [("Scheduled departure", "a", "b")], "SN3811 has new times", "info"),
]
for fields, changes, expected, palette in cases:
    got = headline("alert", FlightView(**{**base, **fields}), changes)
    check(f"{expected!r}", got[1] == expected and got[2] == palette, f"{got[1]!r} [{got[2]}]")
check("started", headline("started", FlightView(**base), [])[1] == "We're now tracking SN3811")
check("welcome", headline("welcome", FlightView(**base), [])[1] == "You'll get updates for SN3811")
check("status words", pretty_status("EnRoute") == "En route" and pretty_status("GateClosed") == "Gate closed")

# --------------------------------------------------------- next update
print("\n3. 'When you'll hear from us next'")
dep = datetime(2026, 9, 27, 14, 0)
view = FlightView(flight_number="KL1705", dep_scheduled_utc=dep)
def text(step):
    title, paras = next_update(step, view, NOW)
    return title + " | " + " ".join(paras)

t = text(NextStep("scheduled", datetime(2026, 9, 22, 10, 5), "airborne"))
check("airborne: every 5 minutes until it lands", "every 5 minutes until it lands" in t)
check("next check in local time with zone", "today at 12:05</strong> (Brussels time)" in t, t[:160])
check("always says no email means no change", "No email means no change" in t)
t = text(NextStep("scheduled", datetime(2026, 9, 22, 10, 10), "close"))
check("close: every 10 minutes", "Departure is close, so we check every 10 minutes" in t)
view.dep_scheduled_utc = datetime(2026, 9, 22, 20, 0)
t = text(NextStep("scheduled", datetime(2026, 9, 22, 11, 0), "day"))
check("day: hourly, then 10-minutely from 2h before",
      "every hour" in t and "From today at 20:00 we'll check every 10 minutes" in t, t[:220])
view.dep_scheduled_utc = dep
t = text(NextStep("scheduled", dep - timedelta(hours=24), "far"))
check("far: next check is the 24h mark -> said once",
      "24 hours before departure" in t and t.count("Sat 26 Sep at 16:00") == 1, t[:260])
t = text(NextStep("scheduled", datetime(2026, 9, 23, 10, 0), "far"))
check("far: weekly check before the 24h mark mentions both",
      "tomorrow at 12:00" in t and "From Sat 26 Sep at 16:00" in t, t[:260])
t = text(NextStep("scheduled", datetime(2026, 9, 22, 10, 30), "unknown"))
check("unknown departure time explained", "doesn't have a departure time" in t)
t = text(NextStep("completed", None, "final", "Arrived"))
check("landed: final update", "final update" in t and "has landed" in t and "No email means" not in t)
t = text(NextStep("completed", None, "final", "Canceled"))
check("cancelled: final update", "has been cancelled" in t)
t = text(NextStep("abandoned", None, "final"))
check("abandoned explained", "Tracking has stopped" in t)

print("\n4. The explanation matches the real schedule")
flight = Flight(status="Expected")
for hours_out, expected_phase in [(72, "far"), (10, "day"), (1, "close"), (-0.5, "close")]:
    flight.dep_scheduled_utc = (NOW + timedelta(hours=hours_out)).replace(tzinfo=None)
    phase = phase_for("Expected", flight.dep_scheduled_utc, NOW)
    real = tracker.next_poll_delay(flight, FlightSnapshot(status="Expected"))
    if phase == "far":
        agrees = real <= interval_for("far")
    else:
        agrees = real == interval_for(phase)
    check(f"{hours_out:+}h -> {phase}", phase == expected_phase and agrees, f"poller waits {real}")
check("airborne phase", phase_for("EnRoute", None, NOW) == "airborne"
      and tracker.next_poll_delay(flight, FlightSnapshot(status="EnRoute")) == interval_for("airborne"))

# ----------------------------------------------------------- local times
print("\n5. Change times shown in airport-local time")
v = FlightView(dep_scheduled_local="2026-09-22 12:00", dep_scheduled_utc=datetime(2026, 9, 22, 10, 0, 30),
               arr_scheduled_local="2026-09-22 13:30", arr_scheduled_utc=datetime(2026, 9, 22, 12, 30))
check("Brussels (UTC+2), seconds noise rounded away",
      fmt_change("Expected/actual departure", "2026-09-22 10:46Z", v) == "Tue 22 Sep, 12:46 (local)")
check("Lisbon (UTC+1)", fmt_change("Scheduled arrival", "2026-09-22 12:36Z", v) == "Tue 22 Sep, 13:36 (local)")
check("falls back to UTC when offset unknown",
      fmt_change("Expected/actual arrival", "2026-09-22 12:36Z", FlightView()) == "22 Sep, 12:36 UTC")
check("status values in words", fmt_change("Status", "EnRoute", v) == "En route")
check("equator/Greenwich coordinates survive",
      FlightView.from_flight(type("F", (), {"dep_lat": 0.0, "dep_lon": 0.0, "positions": [], "callsign": ""})()).dep_lat == 0.0)

# ------------------------------------------------------------------ map
print("\n6. Static map")
data = staticmap.MapData(dep=(50.90, 4.48, "BRU"), arr=(38.78, -9.14, "LIS"),
                         trail=[(50.9, 4.48), (48.0, 1.0)], latest=(48.0, 1.0, 225))
TILE_CALLS.clear()
png = staticmap.render_map(data)
img = Image.open(io.BytesIO(png))
check("renders a 560x300 PNG", img.size == (560, 300) and img.format == "PNG", str(img.size))
check("used a handful of tiles", 1 <= len(TILE_CALLS) <= 12, f"{len(TILE_CALLS)} tiles")
check("nothing to draw -> no map", staticmap.render_map(staticmap.MapData()) is None)

staticmap.fetch_tile = lambda *a, **k: None
check("tile outage still gives a map (plain background)", staticmap.render_map(data) is not None)
def boom(*a, **k): raise RuntimeError("tile server exploded")
staticmap.fetch_tile = boom
check("a crash inside rendering returns None, never raises", staticmap.render_map(data) is None)
staticmap.fetch_tile = fake_tile

settings.email_map_enabled = False
check("EMAIL_MAP_ENABLED=false -> no map", staticmap.render_map(data) is None)
settings.email_map_enabled = True

route = staticmap._unwrap(staticmap.great_circle(35.76, 140.39, 37.62, -122.38, 60), 140.39)
jumps = max(abs(b[1] - a[1]) for a, b in zip(route, route[1:]))
check("Pacific crossing stays continuous (no wrap across the map)", jumps < 10, f"max step {jumps:.1f}°")
check("great circle bends north of both endpoints", max(p[0] for p in route) > 45)

print("\n7. Tile cache")
cache_dir = settings.map_tile_cache_dir
os.makedirs(os.path.join(cache_dir, "5", "16"), exist_ok=True)
Image.new("RGB", (256, 256), (1, 2, 3)).save(os.path.join(cache_dir, "5", "16", "10.png"))
class NoNetwork:
    def get(self, *a, **k): raise AssertionError("network used despite fresh cache")
tile = real_fetch_tile(5, 16, 10, NoNetwork())
check("fresh cached tile served without the network", tile is not None and tile.getpixel((0, 0)) == (1, 2, 3))
class Refuses:
    def get(self, url):
        return httpx.Response(403, request=httpx.Request("GET", url))
check("HTTP error -> None", real_fetch_tile(5, 17, 10, Refuses()) is None)

# --------------------------------------------------------- end to end
print("\n8. A real alert, end to end")
now = datetime.now(timezone.utc).replace(tzinfo=None)
with SessionLocal() as s:
    f = Flight(flight_number="SN3811", flight_date=now.date().isoformat(), status="Boarding",
               airline="Brussels Airlines", dep_iata="BRU", dep_name="Brussels", dep_lat=50.9, dep_lon=4.48,
               arr_iata="LIS", arr_name="Lisbon", arr_lat=38.78, arr_lon=-9.14,
               dep_scheduled_utc=now - timedelta(minutes=5), dep_scheduled_local="2026-09-22 12:00",
               notify_emails="anna@example.com", last_polled_at=now, next_poll_at=now)
    s.add(f); s.commit(); fid = f.id
    s.add(FlightPosition(flight_id=fid, lat=50.5, lon=3.9, altitude_ft=12000, ground_speed_kt=300, track_deg=225))
    s.commit()

class Stub:
    name = "aerodatabox"
    async def fetch(self, n, d):
        return FlightSnapshot(status="Departed", dep_iata="BRU", arr_iata="LIS", dep_lat=50.9, dep_lon=4.48,
                              arr_lat=38.78, arr_lon=-9.14, dep_scheduled_utc=now - timedelta(minutes=5))
tracker.status_provider = Stub()
SENT.clear()
TILE_CALLS.clear()
asyncio.run(tracker.poll_flight(fid, force=True))
inbox = next(m for m in SENT if m["To"] == "rik@example.com")
html = inbox.get_body(("html",)).get_content()
plain = inbox.get_body(("plain",)).get_content()
check("alert has an HTML part", bool(html))
check("headline in HTML", "SN3811 has departed" in html)
check("map embedded", "cid:map" in html and any(p["Content-ID"] == "<map>" for p in inbox.walk()))
check("tiles fetched for the map", len(TILE_CALLS) > 0)
check("next-update box: airborne cadence", "every 5 minutes until it lands" in html)
check("sources listed", "AeroDataBox" in html and "adsb.lol" in html and "OpenStreetMap" in html)
check("plain text also explains next update and sources",
      "When you'll hear from us next" in plain and "Sources:" in plain and "<strong>" not in plain)
check("original plain-text alert content kept", "What changed:" in plain)
check("subject unchanged (PFT prefix)", inbox["Subject"].startswith("PFT SN3811"))
with SessionLocal() as s:
    f = s.get(Flight, fid)
    delta = (f.next_poll_at - now).total_seconds() / 60
check("schedule matches what the email promised (~5 min)", 4 <= delta <= 6, f"{delta:.1f} min")

print("\n9. Confirmation emails and the test alert are designed too")
SENT.clear()
tracker.send_tracking_confirmation(fid)
m = next(m for m in SENT if m["To"] == "rik@example.com")
# Jinja escapes apostrophes (&#39;), which is correct HTML; compare the text.
check("tracking-started email has HTML with headline",
      "We're now tracking SN3811" in html_lib.unescape(m.get_body(("html",)).get_content()))
SENT.clear()
notify.send_test_alert()
m = next(m for m in SENT if m["To"] == "rik@example.com")
check("test alert has the branded HTML with logo",
      "Your flight alerts are working" in m.get_body(("html",)).get_content()
      and any(p["Content-ID"] == "<logo>" for p in m.walk()))

print("\n10. Preview page")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
client = TestClient(app)
for kind in ("alert", "started", "welcome"):
    r = client.get(f"/flights/{fid}/email-preview?kind={kind}")
    check(f"{kind} preview renders", r.status_code == 200 and "data:image/png;base64," in r.text
          and "cid:" not in r.text)
check("bad kind -> 400", client.get(f"/flights/{fid}/email-preview?kind=nope").status_code == 400)
check("unknown flight -> 404", client.get("/flights/9999/email-preview").status_code == 404)
check("flight page links to previews", "email-preview?kind=started" in client.get(f"/flights/{fid}").text)

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("✅ all email design checks passed")
