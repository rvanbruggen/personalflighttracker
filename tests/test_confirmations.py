"""'Tracking started' confirmations and welcome emails for late additions."""

import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/test5.db"
os.environ["MAIL_TO"] = "rik@example.com"
os.environ["SMTP_USER"] = "rik@example.com"
os.environ["SMTP_PASSWORD"] = "app-password"
os.environ["NOTIFICATIONS_ENABLED"] = "true"
os.environ["IFTTT_ENABLED"] = "true"  # on, to prove confirmations stay off the phone

DB = "./data/test5.db"
if os.path.exists(DB):
    os.remove(DB)

from fastapi.testclient import TestClient  # noqa: E402

from app import tracker  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import ApiCall, Flight, FlightEvent, SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.base import FlightNotFound, FlightSnapshot  # noqa: E402

init_db()
failures = []


def check(label, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    if not condition:
        failures.append(label)


SENT = []


class FakeSMTP:
    def __init__(self, host, port, timeout=None): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def ehlo(self): pass
    def starttls(self, context=None): pass
    def login(self, u, p): pass
    def send_message(self, m):
        SENT.append({"to": m["To"], "subject": m["Subject"], "body": m.get_content()})


smtplib.SMTP = FakeSMTP
dep = datetime.now(timezone.utc) + timedelta(hours=30)


class Stub:
    name = "aerodatabox"
    result = FlightSnapshot(
        status="Expected", airline="Brussels Airlines", dep_iata="BRU", dep_name="Brussels",
        arr_iata="LIS", arr_name="Lisbon", dep_terminal="A", dep_gate="A42",
        dep_scheduled_utc=dep, dep_scheduled_local="2026-09-22 09:15",
        arr_scheduled_local="2026-09-22 11:05",
    )
    calls = 0

    async def fetch(self, number, date):
        Stub.calls += 1
        if isinstance(Stub.result, Exception):
            raise Stub.result
        return Stub.result


tracker.status_provider = Stub()
client = TestClient(app)
date_str = dep.date().isoformat()


def events(fid, summary):
    with SessionLocal() as s:
        return s.query(FlightEvent).filter_by(flight_id=fid, summary=summary).all()


print("\n1. Registration confirms to everyone on the flight")
SENT.clear()
r = client.post("/flights", data={"flight_number": "SN2103", "flight_date": date_str,
                                  "label": "Anna & Bob to Lisbon",
                                  "notify_emails": "anna@example.com, bob@example.org"},
                follow_redirects=False)
with SessionLocal() as s:
    fid = s.query(Flight).filter_by(flight_number="SN2103").one().id
to = sorted(m["to"] for m in SENT)
check("default + both extras receive it",
      to == ["anna@example.com", "bob@example.org", "rik@example.com"], str(to))
check("nothing sent to the IFTTT trigger", "trigger@applet.ifttt.com" not in to)
check("subject says tracking started, with PFT prefix and route",
      all(m["subject"] == "PFT SN2103 BRU→LIS tracking started" for m in SENT), SENT[0]["subject"])
body = SENT[0]["body"]
check("body carries current details",
      "Status: Expected" in body and "A / A42" in body and "2026-09-22 09:15" in body)
check("body says what to expect", "You'll get an email whenever" in body)
check("body includes the note", "Note: Anna & Bob to Lisbon" in body)
extra_body = next(m["body"] for m in SENT if m["to"] == "anna@example.com")
check("extras get the 'why am I receiving this' footer", "added as a recipient" in extra_body)
ev = events(fid, "Tracking confirmation email")
check("confirmation logged in history",
      len(ev) == 1 and "Sent to: rik@example.com, anna@example.com, bob@example.org" in ev[0].detail,
      ev[0].detail if ev else "")
check("flash message reports it", "Confirmation+emailed+to+3" in r.headers["location"]
      or "Confirmation%20emailed%20to%203" in r.headers["location"], r.headers["location"])

print("\n2. No extra API quota")
with SessionLocal() as s:
    calls = s.query(ApiCall).count()
check("registration made exactly one status call (the baseline poll)",
      Stub.calls == 1 and calls == 1, f"stub={Stub.calls} ledger={calls}")

print("\n3. Adding someone later welcomes only them")
SENT.clear()
client.post(f"/flights/{fid}/recipients",
            data={"notify_emails": "anna@example.com, bob@example.org, carol@example.net"})
check("only the new person is emailed", [m["to"] for m in SENT] == ["carol@example.net"],
      str([m["to"] for m in SENT]))
check("welcome subject", SENT and SENT[0]["subject"] == "PFT SN2103 BRU→LIS alerts: you've been added")
check("welcome body wording", SENT and "You've been added to the alerts" in SENT[0]["body"])
check("welcome logged", len(events(fid, "Welcome email to new recipients")) == 1)
check("still no extra API call", Stub.calls == 1)

print("\n4. Removing or re-saving sends nothing")
SENT.clear()
client.post(f"/flights/{fid}/recipients", data={"notify_emails": "anna@example.com, carol@example.net"})
check("removal sends no email", SENT == [], str(SENT))
client.post(f"/flights/{fid}/recipients", data={"notify_emails": "anna@example.com, carol@example.net"})
check("unchanged list sends no email", SENT == [])
client.post(f"/flights/{fid}/recipients/remove", data={"email": "anna@example.com"})
check("single remove sends no email", SENT == [])

print("\n5. Flight not yet known to the provider")
SENT.clear()
Stub.result = FlightNotFound("not yet")
client.post("/flights", data={"flight_number": "KL1705", "flight_date": date_str}, follow_redirects=False)
check("confirmation still sent", [m["to"] for m in SENT] == ["rik@example.com"])
check("says details aren't available yet", SENT and "aren't available from the data provider yet" in SENT[0]["body"])
check("no misleading 'Current state' block", SENT and "Current state" not in SENT[0]["body"])

print("\n6. Switched off")
SENT.clear()
settings.send_tracking_confirmations = False
Stub.result = FlightSnapshot(status="Expected", dep_iata="AMS", arr_iata="LHR", dep_scheduled_utc=dep)
client.post("/flights", data={"flight_number": "KL1001", "flight_date": date_str,
                              "notify_emails": "dave@example.com"}, follow_redirects=False)
with SessionLocal() as s:
    kid = s.query(Flight).filter_by(flight_number="KL1001").one().id
client.post(f"/flights/{kid}/recipients", data={"notify_emails": "dave@example.com, erin@example.com"})
check("no confirmation or welcome emails", SENT == [], str(SENT))
check("nothing logged", events(kid, "Tracking confirmation email") == [])
settings.send_tracking_confirmations = True

print("\n7. Notifications disabled: registration still works, reason logged")
SENT.clear()
settings.notifications_enabled = False
r = client.post("/flights", data={"flight_number": "KL1002", "flight_date": date_str}, follow_redirects=False)
settings.notifications_enabled = True
with SessionLocal() as s:
    nid = s.query(Flight).filter_by(flight_number="KL1002").one().id
ev = events(nid, "Tracking confirmation email")
check("registration succeeded", r.status_code == 303)
check("no email sent", SENT == [])
check("history explains why", ev and "disabled" in ev[0].detail, ev[0].detail if ev else "")

print("\n8. Change alerts are unaffected")
SENT.clear()
Stub.result = FlightSnapshot(status="Delayed", dep_iata="BRU", arr_iata="LIS",
                             dep_gate="B7", dep_scheduled_utc=dep)
import asyncio  # noqa: E402
asyncio.run(tracker.poll_flight(fid, force=True))
to = sorted(m["to"] for m in SENT)
check("change alert reaches default + current extras, and IFTTT",
      to == ["carol@example.net", "rik@example.com", "trigger@applet.ifttt.com"], str(to))

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("✅ all confirmation checks passed")
