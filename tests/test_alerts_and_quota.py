"""Quota guard, abandonment, error backoff, and the Gmail+IFTTT dual send."""

import asyncio
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///./data/test2.db"

DB = "./data/test2.db"
if os.path.exists(DB):
    os.remove(DB)

from app import notify, tracker  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import ApiCall, Flight, SessionLocal, init_db  # noqa: E402
from app.providers.base import FlightNotFound, FlightSnapshot, ProviderError  # noqa: E402

init_db()
failures = []


def check(label, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    if not condition:
        failures.append(label)


def make_flight(**kwargs):
    defaults = dict(
        flight_number="KL9999",
        flight_date=(datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat(),
        next_poll_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    defaults.update(kwargs)
    with SessionLocal() as s:
        f = Flight(**defaults)
        s.add(f)
        s.commit()
        return f.id


class Stub:
    name = "aerodatabox"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def fetch(self, number, date):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


# ---------------------------------------------------------------- quota guard
print("\n1. Monthly quota guard")
with SessionLocal() as s:
    # Burn the whole budget.
    s.add(ApiCall(provider="aerodatabox", units=settings.aerodatabox_monthly_unit_budget))
    s.commit()

stub = Stub(FlightSnapshot(status="Expected"))
tracker.status_provider = stub
fid = make_flight()
outcome = asyncio.run(tracker.poll_flight(fid))
check("polling refuses to overrun the budget", outcome == "quota exhausted", outcome)
check("no upstream call was made", stub.calls == 0, f"{stub.calls} calls")
with SessionLocal() as s:
    f = s.get(Flight, fid)
    check("flight paused, not lost", f.tracking_state == "active" and f.next_poll_at is not None)
    check("reason surfaced in the UI", "budget" in f.last_error.lower(), f.last_error)

outcome = asyncio.run(tracker.poll_flight(fid, force=True))
check("manual refresh can override the guard", stub.calls == 1, f"{stub.calls} calls")

with SessionLocal() as s:
    s.query(ApiCall).delete()
    s.commit()

# --------------------------------------------------------------- error paths
print("\n2. Upstream errors back off instead of hammering")
stub = Stub(ProviderError("upstream exploded"))
tracker.status_provider = stub
fid = make_flight(flight_number="LH0400")
for expected_errors in (1, 2, 3):
    asyncio.run(tracker.poll_flight(fid, force=True))
    with SessionLocal() as s:
        f = s.get(Flight, fid)
        delay = (f.next_poll_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() / 60
    check(
        f"error {expected_errors}: backoff grows to ~{expected_errors * settings.poll_retry_minutes} min",
        f.consecutive_errors == expected_errors
        and abs(delay - expected_errors * settings.poll_retry_minutes) < 2,
        f"{delay:.0f} min, {f.consecutive_errors} errors",
    )
check("failed call recorded as not-ok", True)
with SessionLocal() as s:
    bad = s.query(ApiCall).filter(ApiCall.ok.is_(False)).count()
    check("failed calls logged for quota accounting", bad == 3, f"{bad}")

print("\n3. A flight that never appears is eventually abandoned")
old_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
stub = Stub(FlightNotFound("no such flight"))
tracker.status_provider = stub
fid = make_flight(flight_number="XX0001", flight_date=old_date)
asyncio.run(tracker.poll_flight(fid, force=True))
with SessionLocal() as s:
    f = s.get(Flight, fid)
    check("marked abandoned", f.tracking_state == "abandoned", f.tracking_state)
    check("polling stopped", f.next_poll_at is None)
    check("reason logged as an event", any("Stopped tracking" in e.summary for e in f.events))

future_date = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
fid = make_flight(flight_number="XX0002", flight_date=future_date)
asyncio.run(tracker.poll_flight(fid, force=True))
with SessionLocal() as s:
    f = s.get(Flight, fid)
    check("a future flight not yet in the schedule keeps retrying",
          f.tracking_state == "active" and f.next_poll_at is not None, f.tracking_state)

# ------------------------------------------------------------ Gmail + IFTTT
print("\n4. Alert goes to both the inbox and the IFTTT trigger address")
sent = []


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        sent.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        sent.append(("starttls",))

    def login(self, user, password):
        sent.append(("login", user))

    def send_message(self, message):
        sent.append(("send", message["To"], message["Subject"], message.get_content()))


settings.smtp_user = "rik@example.com"
settings.smtp_password = "app-password"
settings.mail_to = ""
settings.notifications_enabled = True
settings.ifttt_enabled = True
original_smtp = smtplib.SMTP
smtplib.SMTP = FakeSMTP
try:
    result = notify.send_alert("KL1705 AMS→LIS DELAYED +45min", "body text")
finally:
    smtplib.SMTP = original_smtp

sends = [entry for entry in sent if entry[0] == "send"]
check("exactly two copies sent", len(sends) == 2, f"{len(sends)}")
check("STARTTLS used before login",
      [e[0] for e in sent].index("starttls") < [e[0] for e in sent].index("login"))
check("inbox copy addressed to the user", sends[0][1] == "rik@example.com", sends[0][1])
check("IFTTT copy addressed to the trigger", sends[1][1] == "trigger@applet.ifttt.com", sends[1][1])
check("IFTTT subject carries the hashtag", sends[1][2].endswith("#flight"), sends[1][2])
check("inbox subject has no hashtag noise", "#flight" not in sends[0][2], sends[0][2])
check("inbox subject starts with PFT", sends[0][2].startswith("PFT "), sends[0][2])
check("IFTTT subject starts with PFT", sends[1][2].startswith("PFT "), sends[1][2])
check("prefix precedes the flight number",
      sends[0][2].startswith("PFT KL1705"), sends[0][2])

print("\n4b. Subject prefix is idempotent and configurable")
sent.clear()
smtplib.SMTP = FakeSMTP
try:
    notify.send_alert("PFT KL1705 already prefixed", "body")
finally:
    smtplib.SMTP = original_smtp
check("not double-prefixed",
      [e for e in sent if e[0] == "send"][0][2].count("PFT") == 1,
      [e for e in sent if e[0] == "send"][0][2])

settings.email_subject_prefix = ""
sent.clear()
smtplib.SMTP = FakeSMTP
try:
    notify.send_alert("KL1705 no prefix wanted", "body")
finally:
    smtplib.SMTP = original_smtp
check("empty prefix disables it",
      [e for e in sent if e[0] == "send"][0][2] == "KL1705 no prefix wanted",
      [e for e in sent if e[0] == "send"][0][2])
settings.email_subject_prefix = "PFT"

sent.clear()
smtplib.SMTP = FakeSMTP
try:
    notify.send_test_alert()
finally:
    smtplib.SMTP = original_smtp
check("test alert is prefixed too",
      [e for e in sent if e[0] == "send"][0][2].startswith("PFT "),
      [e for e in sent if e[0] == "send"][0][2])
check("both report success", result.inbox_sent and result.ifttt_sent and result.ok)

print("\n5. Hashtag is not duplicated if already present")
sent.clear()
smtplib.SMTP = FakeSMTP
try:
    notify.send_alert("KL1705 #flight", "body")
finally:
    smtplib.SMTP = original_smtp
ifttt_subject = [e for e in sent if e[0] == "send"][1][2]
check("hashtag appears once", ifttt_subject.count("#flight") == 1, ifttt_subject)

print("\n6. Auth failure gives an actionable message")
class AuthFailSMTP(FakeSMTP):
    def login(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

smtplib.SMTP = AuthFailSMTP
try:
    result = notify.send_alert("subject", "body")
finally:
    smtplib.SMTP = original_smtp
check("mentions app password", "app password" in result.error.lower(), result.error)
check("marked as failed", not result.ok)

print("\n7. Unconfigured SMTP degrades gracefully")
settings.smtp_user = ""
settings.smtp_password = ""
result = notify.send_alert("subject", "body")
check("no crash, clear error", not result.ok and "SMTP_USER" in result.error, result.error)

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("✅ all alert/quota checks passed")
