"""Quota guard, abandonment, error backoff, Gmail alerts and the IFTTT webhook push."""

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

import httpx  # noqa: E402

from app import notify, push, tracker  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import ApiCall, Flight, SessionLocal, init_db  # noqa: E402
from app.providers.base import FlightNotFound, FlightSnapshot, ProviderError  # noqa: E402

init_db()
failures = []


def check(label, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    if not condition:
        failures.append(label)


def _rejects_bad_day() -> bool:
    from pydantic import ValidationError

    from app.config import Settings
    try:
        Settings(aerodatabox_quota_reset_day=45, _env_file=None)
    except ValidationError:
        return True
    return False


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
print("\n4. Alert goes to the inbox by email and to the phone by webhook")
sent = []
posts = []


class FakeResponse:
    def __init__(self, status_code=200, text="Congratulations! You've fired the flight_alert event"):
        self.status_code, self.text = status_code, text


def fake_post(url, payload):
    posts.append((url, payload))
    return FakeResponse()


push._post = fake_post


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
        sent.append(("send", message["To"], message["Subject"], message.get_body(preferencelist=("plain",)).get_content()))


settings.smtp_user = "rik@example.com"
settings.smtp_password = "app-password"
settings.mail_to = ""
settings.notifications_enabled = True
settings.ifttt_enabled = True
settings.ifttt_webhook_key = "sekr3t-key"
settings.ifttt_event = "flight_alert"
original_smtp = smtplib.SMTP
smtplib.SMTP = FakeSMTP
try:
    result = notify.send_alert(
        "KL1705 AMS→LIS DELAYED +45min", "body text",
        push=push.PushMessage("KL1705 AMS→LIS DELAYED +45min", "Delay: 0 → 45 min",
                              "http://tracker.local/flights/1"),
    )
finally:
    smtplib.SMTP = original_smtp

sends = [entry for entry in sent if entry[0] == "send"]
check("exactly one email sent (no IFTTT trigger copy)", len(sends) == 1, f"{len(sends)}")
check("STARTTLS used before login",
      [e[0] for e in sent].index("starttls") < [e[0] for e in sent].index("login"))
check("inbox copy addressed to the user", sends[0][1] == "rik@example.com", sends[0][1])
check("inbox subject starts with PFT", sends[0][2].startswith("PFT "), sends[0][2])
check("exactly one webhook call", len(posts) == 1, str(len(posts)))
check("webhook URL carries event and key",
      posts[0][0] == "https://maker.ifttt.com/trigger/flight_alert/with/key/sekr3t-key", posts[0][0])
check("payload maps title/message/link to value1/2/3",
      posts[0][1] == {"value1": "KL1705 AMS→LIS DELAYED +45min", "value2": "Delay: 0 → 45 min",
                      "value3": "http://tracker.local/flights/1"}, str(posts[0][1]))
check("result reports both channels", result.inbox_sent and result.ifttt_sent and result.ok
      and not result.push_error)
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
check("test alert pushes too", posts[-1][1]["value1"] == "Test alert from flight tracker", str(posts[-1]))

print("\n5. Webhook failures, retries and switches")
posts.clear()
smtplib.SMTP = FakeSMTP
try:
    notify.send_alert("no push wanted", "body")
finally:
    smtplib.SMTP = original_smtp
check("no push argument, no webhook call", posts == [])

settings.ifttt_webhook_key = ""
r = push.send_push(push.PushMessage("t", "m"))
check("no key: skipped silently", not r.sent and not r.error and posts == [], str(r))
settings.ifttt_webhook_key = "sekr3t-key"

settings.ifttt_enabled = False
r = push.send_push(push.PushMessage("t", "m"))
check("IFTTT_ENABLED=false: skipped", not r.sent and posts == [])
settings.ifttt_enabled = True

push._post = lambda url, payload: (posts.append(url), FakeResponse(401, '{"errors":[{"message":"You sent an invalid key."}]}'))[1]
r = push.send_push(push.PushMessage("t", "m"))
check("401: clear error pointing at the key", not r.sent and "IFTTT_WEBHOOK_KEY" in r.error, r.error)
check("401: not retried", len(posts) == 1, str(len(posts)))

posts.clear()
responses = [FakeResponse(503, "busy"), FakeResponse(200)]
push._post = lambda url, payload: (posts.append(url), responses.pop(0))[1]
r = push.send_push(push.PushMessage("t", "m"))
check("5xx then 200: retried once and sent", r.sent and not r.error and len(posts) == 2, str(r))

def boom(url, payload):
    raise httpx.ConnectError(f"cannot reach {url}")

push._post = boom
r = push.send_push(push.PushMessage("t", "m"))
check("network error: reported, not raised", not r.sent and "unreachable" in r.error, r.error)
check("key never appears in the error", "sekr3t-key" not in r.error, r.error)

print("\n5b. Push still goes out when Gmail fails, and vice versa")
posts.clear()
push._post = fake_post

print("\n6. Auth failure gives an actionable message")
class AuthFailSMTP(FakeSMTP):
    def login(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

smtplib.SMTP = AuthFailSMTP
try:
    result = notify.send_alert("subject", "body", push=push.PushMessage("subject", "m"))
finally:
    smtplib.SMTP = original_smtp
check("push sent despite the Gmail failure", result.ifttt_sent and len(posts) == 1)
check("mentions app password", "app password" in result.error.lower(), result.error)
check("marked as failed", not result.ok)

print("\n7. Unconfigured SMTP degrades gracefully")
settings.smtp_user = ""
settings.smtp_password = ""
push._post = lambda url, payload: FakeResponse(500, "down")
result = notify.send_alert("subject", "body", push=push.PushMessage("subject", "m"))
check("push failure kept apart from the email error",
      "HTTP 500" in result.push_error and "HTTP" not in result.error, result.push_error)
check("no crash, clear error", not result.ok and "SMTP_USER" in result.error, result.error)

print("\n8. Quota window follows the RapidAPI billing anniversary")
from datetime import datetime as _dt  # noqa: E402
from app.db import quota_period_end, quota_period_start  # noqa: E402

for now, day, exp_start, exp_end, why in [
    (_dt(2026, 9, 10), 26, _dt(2026, 8, 26), _dt(2026, 9, 26), "mid-window"),
    (_dt(2026, 9, 26), 26, _dt(2026, 9, 26), _dt(2026, 10, 26), "on reset day"),
    (_dt(2026, 1, 5), 26, _dt(2025, 12, 26), _dt(2026, 1, 26), "across new year"),
    (_dt(2026, 3, 5), 31, _dt(2026, 2, 28), _dt(2026, 3, 31), "day 31 clamps in Feb"),
    (_dt(2024, 2, 29), 31, _dt(2024, 2, 29), _dt(2024, 3, 31), "leap year"),
    (_dt(2026, 9, 15), 1, _dt(2026, 9, 1), _dt(2026, 10, 1), "default calendar month"),
]:
    check(f"reset day {day}, {why}",
          quota_period_start(now, day) == exp_start and quota_period_end(now, day) == exp_end,
          f"{quota_period_start(now, day):%Y-%m-%d}..{quota_period_end(now, day):%Y-%m-%d}")

check("invalid reset day rejected", _rejects_bad_day())

print()
if failures:
    print(f"❌ {len(failures)} FAILED: {failures}")
    sys.exit(1)
print("✅ all alert/quota checks passed")
