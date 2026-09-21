"""Per-flight recipients: parsing, storage, migration, routes, and delivery."""

import asyncio
import os
import smtplib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/test4.db"
os.environ["MAIL_TO"] = "rik@example.com"
os.environ["SMTP_USER"] = "rik@example.com"
os.environ["SMTP_PASSWORD"] = "app-password"
os.environ["NOTIFICATIONS_ENABLED"] = "true"
os.environ["IFTTT_ENABLED"] = "false"

DB = "./data/test4.db"
if os.path.exists(DB):
    os.remove(DB)

from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod, notify, tracker  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Flight, FlightEvent, SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.base import FlightSnapshot  # noqa: E402
from app.recipients import all_recipients, parse_recipients  # noqa: E402

init_db()
failures = []


def check(label, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    if not condition:
        failures.append(label)


SENT = []


class FakeSMTP:
    reject: set = set()

    def __init__(self, host, port, timeout=None): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def starttls(self, context=None): pass
    def login(self, u, p): pass

    def send_message(self, m):
        if m["To"] in FakeSMTP.reject:
            raise smtplib.SMTPRecipientsRefused({m["To"]: (550, b"no such user")})
        SENT.append({"to": m["To"], "subject": m["Subject"], "body": m.get_content()})


smtplib.SMTP = FakeSMTP

# ------------------------------------------------------------------ parsing
print("\n1. Parsing")
p = parse_recipients("Anna@Example.com; bob@example.com anna@example.com")
check("splits on , ; and whitespace, lowercases, dedupes",
      p.emails == ["anna@example.com", "bob@example.com"], str(p.emails))
p = parse_recipients("rik@example.com, anna@example.com")
check("default address is never stored as an extra",
      p.emails == ["anna@example.com"] and p.dropped_default)
check("invalid address reported", parse_recipients("anna@, bob@example.com").invalid == ["anna@"])
check("more than 10 is an error", bool(parse_recipients(
    ",".join(f"p{i}@example.com" for i in range(11))).error))
check("default always listed first",
      all_recipients("anna@example.com")[0] == "rik@example.com")

# ---------------------------------------------------------------- migration
print("\n2. Migration adds the column to an existing database")
conn = sqlite3.connect(DB)
conn.execute("ALTER TABLE flights DROP COLUMN notify_emails")
conn.commit()
had = "notify_emails" in [r[1] for r in conn.execute("PRAGMA table_info(flights)")]
conn.close()
check("column removed to simulate a v0.2.x database", not had)
# Pooled connections predate the raw DROP; a real upgrade starts a new process.
dbmod.engine.dispose()
added = dbmod.init_db()
check("migration re-adds notify_emails", "flights.notify_emails" in added, str(added))

# ------------------------------------------------------------- registration
client = TestClient(app)
dep = datetime.now(timezone.utc) + timedelta(hours=30)


class StubStatus:
    name = "aerodatabox"
    snapshot = FlightSnapshot(status="Expected", dep_iata="BRU", arr_iata="LIS",
                              dep_gate="A1", dep_scheduled_utc=dep)

    async def fetch(self, number, date):
        return StubStatus.snapshot


tracker.status_provider = StubStatus()
date_str = dep.date().isoformat()

print("\n3. Registration")
r = client.post("/flights", data={"flight_number": "SN2103", "flight_date": date_str,
                                  "notify_emails": "anna@example.com, Bob@Example.com"},
                follow_redirects=False)
check("registers with recipients", r.status_code == 303)
with SessionLocal() as s:
    f = s.query(Flight).filter_by(flight_number="SN2103").one()
    fid = f.id
    check("recipients stored normalised",
          f.notify_emails == "anna@example.com,bob@example.com", f.notify_emails)

r = client.post("/flights", data={"flight_number": "SN2104", "flight_date": date_str,
                                  "notify_emails": "anna@example.com, not-an-email"},
                follow_redirects=False)
with SessionLocal() as s:
    created = s.query(Flight).filter_by(flight_number="SN2104").count()
check("an invalid address rejects the registration instead of dropping it silently",
      created == 0 and "valid" in r.headers["location"].lower())

r = client.post("/flights", data={"flight_number": "SN2105", "flight_date": date_str},
                follow_redirects=False)
with SessionLocal() as s:
    check("no recipients field -> default only",
          s.query(Flight).filter_by(flight_number="SN2105").one().notify_emails == "")

# ------------------------------------------------------------ edit / remove
print("\n4. Editing recipients later")
client.post(f"/flights/{fid}/recipients",
            data={"notify_emails": "anna@example.com, carol@example.org"})
with SessionLocal() as s:
    f = s.get(Flight, fid)
    check("list replaced", f.notify_emails == "anna@example.com,carol@example.org", f.notify_emails)
    ev = s.query(FlightEvent).filter_by(flight_id=fid, summary="Recipients updated").first()
    check("change logged in history", ev is not None and "carol" in ev.detail
          and "bob" in ev.detail, ev.detail if ev else "")

r = client.post(f"/flights/{fid}/recipients", data={"notify_emails": "carol@, x"},
                follow_redirects=False)
with SessionLocal() as s:
    check("invalid edit leaves the list untouched",
          s.get(Flight, fid).notify_emails == "anna@example.com,carol@example.org")

client.post(f"/flights/{fid}/recipients/remove", data={"email": "anna@example.com"})
with SessionLocal() as s:
    check("single remove works", s.get(Flight, fid).notify_emails == "carol@example.org")

client.post(f"/flights/{fid}/recipients", data={"notify_emails": "anna@example.com, carol@example.org"})

# ----------------------------------------------------------------- delivery
print("\n5. Delivery: one individual copy per recipient")
SENT.clear()
StubStatus.snapshot = FlightSnapshot(status="Delayed", dep_iata="BRU", arr_iata="LIS",
                                     dep_gate="B7", dep_scheduled_utc=dep)
asyncio.run(tracker.poll_flight(fid, force=True))
to = sorted(m["to"] for m in SENT)
check("default + two extras each get a copy",
      to == ["anna@example.com", "carol@example.org", "rik@example.com"], str(to))
check("every message has exactly one address in To",
      all("," not in m["to"] for m in SENT))
check("all copies carry the PFT subject", all(m["subject"].startswith("PFT ") for m in SENT))
by = {m["to"]: m for m in SENT}
check("extras get the 'why am I receiving this' footer",
      "added as a recipient" in by["anna@example.com"]["body"])
check("default copy has no footer", "added as a recipient" not in by["rik@example.com"]["body"])
check("no copy reveals another recipient's address",
      "carol@example.org" not in by["anna@example.com"]["body"]
      and "anna@example.com" not in by["carol@example.org"]["body"])
with SessionLocal() as s:
    ev = (s.query(FlightEvent).filter_by(flight_id=fid, kind="change")
          .order_by(FlightEvent.id.desc()).first())
    check("history records who received it",
          "Sent to: rik@example.com, anna@example.com, carol@example.org" in ev.detail, ev.detail)
    check("event marked notified", ev.notified)

print("\n6. A bad extra address doesn't block anyone else")
SENT.clear()
FakeSMTP.reject = {"anna@example.com"}
res = notify.send_alert("KL1 test", "body", extra_recipients=["anna@example.com", "carol@example.org"])
FakeSMTP.reject = set()
check("default still delivered", res.inbox_sent)
check("other extra still delivered", res.extra_sent == ["carol@example.org"], str(res.extra_sent))
check("failure reported per address", res.extra_failed == ["anna@example.com"])
check("not treated as a whole-alert failure", res.ok, res.error)

print("\n7. Default address listed as an extra is not sent twice")
SENT.clear()
notify.send_alert("KL1 test", "body", extra_recipients=["RIK@example.com"])
check("one copy only", [m["to"] for m in SENT] == ["rik@example.com"], str([m["to"] for m in SENT]))

print("\n8. Test alert goes only to the default address")
SENT.clear()
notify.send_test_alert()
check("test alert: default only", [m["to"] for m in SENT] == ["rik@example.com"])

# ----------------------------------------------------------------------- UI
print("\n9. UI and API")
html = client.get(f"/flights/{fid}").text
check("detail page shows the Notifications panel", "<h2>Notifications</h2>" in html)
check("default shown as always notified", "rik@example.com" in html and "always notified" in html)
check("extras listed with remove buttons", html.count("/recipients/remove") == 2)
index = client.get("/").text
check("registration form has the Also notify field", 'name="notify_emails"' in index)
check("list shows recipient count", "+2 recipients" in index)
api = {f["flight_number"]: f for f in client.get("/api/flights").json()}
check("API lists all recipients, default first",
      api["SN2103"]["recipients"] == ["rik@example.com", "anna@example.com", "carol@example.org"],
      str(api["SN2103"]["recipients"]))

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("✅ all recipient checks passed")
