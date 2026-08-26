# Personal Flight Tracker

A personal flight tracker that you can run for free on your LAN.

Register a flight number + date in a web page on your home network. The app
polls flight status on an adaptive schedule, diffs every response against what
it already knew, and emails you the moment something changes — a delay, a gate
change, a cancellation, wheels-up, wheels-down. A second copy of each alert goes
to IFTTT's Email trigger, which turns it into a push notification on your phone.

Runs as a single container. €0/month at personal volume.

**This is Phase 1** of [SPEC.md](SPEC.md): registration, adaptive status polling,
and alerting. The live Leaflet map (Phase 2) is not built yet — but the callsign
that unlocks it is already captured and stored on every flight.

---

## What you need to set up

Three things, ~15 minutes total. Two of them are free accounts.

### 1. AeroDataBox API key (required — this is the flight data)

1. Create a free account at **[rapidapi.com](https://rapidapi.com/)**.
2. Go to the **[AeroDataBox pricing page](https://rapidapi.com/aedbx-aedbx/api/aerodatabox/pricing)**
   and subscribe to the **Basic (free)** plan. It asks for a credit card to
   guard against overage, but the free tier itself costs nothing — and this app
   hard-stops at the configured budget rather than spilling into paid usage.
3. Copy your key (shown as `X-RapidAPI-Key`) into `.env` as `AERODATABOX_API_KEY`.

The free tier is ~600 units/month; a status check costs 2 units. That's ~300
checks — roughly **5–8 tracked flights per month**. The header bar shows how
much you've used, and polling pauses rather than overrunning it.

### 2. Gmail app password (required — this is how alerts reach you)

Gmail will not accept your normal password over SMTP.

1. Turn on **2-Step Verification** on your Google account (required for the next step).
2. Go to **[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)**
   and generate an app password. You get a 16-character string.
3. Put your Gmail address in `SMTP_USER` and that 16-character password in
   `SMTP_PASSWORD`. Remove the spaces Google displays.

By default alerts go to the same address that sends them. Set `MAIL_TO` to send
them somewhere else.

### 3. IFTTT applet (optional — this is the phone notification)

Without this you still get emails; you just don't get a push notification.

1. Install the IFTTT app and sign in — **with the same Gmail address** you put in
   `SMTP_USER`. IFTTT's Email trigger only fires on mail sent *from* the address
   registered to your IFTTT account. This is the step people get wrong.
2. Create an applet: **[Email → Send a notification from the IFTTT app](https://ifttt.com/connect/email/if_notifications)**.
3. Choose the trigger **"Send IFTTT an email tagged"** and set the tag to
   `flight` (no `#`). The app puts `#flight` in the subject of the trigger copy;
   change `IFTTT_HASHTAG` in `.env` if you pick a different tag.
4. Leave `IFTTT_ENABLED=true`.

The free IFTTT plan allows 2 applets; you need one.

> Each alert sends **two** emails: a readable one to your inbox, and a
> hashtagged one to `trigger@applet.ifttt.com` that fires the applet.

---

## Running it

On the machine that will host it (the always-on Mint MacBook):

```bash
git clone <this-repo> personalflighttracker
cd personalflighttracker
cp .env.example .env
```

Edit `.env` and fill in the three sections above. Then:

```bash
docker compose up -d --build
```

Open **`http://<host-ip>:8080`** from any device on your network. Find the IP
with `hostname -I`.

Check it came up cleanly:

```bash
docker compose logs -f app
```

`/healthz` reports whether each integration is configured:

```bash
curl -s http://localhost:8080/healthz
```

Once it's running, hit **"send test alert"** in the page footer. That verifies
Gmail and IFTTT in one shot: your inbox should get a mail, and your phone
should buzz.

### Keep the laptop awake

Per [SPEC.md](SPEC.md), a MacBook running Mint sleeps on lid close. One-time fix:

```bash
sudo sed -i 's/^#*HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf && sudo systemctl restart systemd-logind
```

---

## How the polling schedule works

The whole point is to spend API quota only when something might actually change:

| When | How often | Why |
|---|---|---|
| At registration | once | Establishes the baseline — never alerts |
| More than 24h out | every 7 days | Catches early cancellations, costs almost nothing |
| 24h → 2h before departure | hourly | Schedule changes surface here |
| Under 2h before departure | every 10 min | Gate assignments, boarding, last-minute delays |
| In the air | every 5 min | Diversions and arrival time |
| Arrived / cancelled | stops | Nothing left to watch |

Roughly 30–50 checks per flight. Every interval is tunable in `.env`.

Two safety rails: calls are throttled to stay under the 1 req/sec limit, and the
app refuses to poll once the monthly unit budget is spent (a manual **Refresh
now** can still override that for one call).

## What triggers an alert

A change is only an alert if it matters. Status, departure gate, departure
terminal, and any time change over 2 minutes will email you. Arrival gate,
baggage belt, aircraft registration, and callsign are recorded in the flight's
history but stay silent.

The app never treats *missing* data as a change — providers drop fields
intermittently, and that shouldn't wake your phone at 4am.

---

## Configuration

Every setting lives in `.env`; see [.env.example](.env.example) for the annotated
list. `.env` is gitignored — **secrets never enter the repo.**

The ones worth knowing:

| Variable | Default | Purpose |
|---|---|---|
| `AERODATABOX_API_KEY` | — | RapidAPI key. Required. |
| `AERODATABOX_MONTHLY_UNIT_BUDGET` | `600` | Hard stop. Lower it if you share the key. |
| `SMTP_USER` / `SMTP_PASSWORD` | — | Gmail address + **app password**. |
| `MAIL_TO` | = `SMTP_USER` | Where readable alerts land. |
| `IFTTT_ENABLED` | `true` | Set `false` to skip the phone-notification copy. |
| `IFTTT_HASHTAG` | `#flight` | Must match your applet's tag. |
| `NOTIFICATIONS_ENABLED` | `true` | `false` records changes silently — handy for testing. |
| `POLL_*` | see table above | Cadence tuning. |

## Endpoints

| Path | What |
|---|---|
| `GET /` | Registration form + tracked flights |
| `GET /flights/{id}` | One flight: current state, change history, raw provider JSON |
| `GET /healthz` | Config + scheduler + quota status |
| `GET /api/flights` | JSON list of tracked flights |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # works with empty keys; polling will just error
.venv/bin/uvicorn app.main:app --reload --port 8080
```

Tests use stubbed providers and a fake mailer — they never call the real API or
send real email, so they cost no quota:

```bash
.venv/bin/python tests/run_all.py
```

### Layout

```
app/
  main.py         FastAPI routes, APScheduler wiring
  tracker.py      Adaptive poll cadence, quota guard, alert dispatch
  diffing.py      Snapshot → list of human-readable changes
  notify.py       Gmail SMTP + IFTTT dual send
  db.py           SQLite models (flights, events, api_calls)
  config.py       .env-backed settings
  providers/
    base.py       FlightSnapshot + the provider interface
    aerodatabox.py  The only file that knows about AeroDataBox
```

Swapping status providers means writing one new file in `providers/` that
returns a `FlightSnapshot` — nothing else changes.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `AeroDataBox rejected the API key (HTTP 401/403)` | Key wrong, or you haven't subscribed to the Basic plan on RapidAPI. |
| `Gmail rejected the login` | Using your account password. It must be a 16-character app password, with 2FA enabled. |
| Emails arrive, phone stays silent | IFTTT applet tag doesn't match `IFTTT_HASHTAG`, or your IFTTT account uses a different email than `SMTP_USER`. |
| `has no record of ... yet` | Normal far in advance. The app retries and gives up ~12h after the scheduled arrival. |
| Header shows quota exhausted | Free tier spent for the month. Resets on the 1st; `Refresh now` still works for one-off checks. |
| Permission errors on `./data` | Container runs as uid 1000. `sudo chown -R 1000:1000 data`. |

## Roadmap

- **Phase 2** — live position from adsb.lol/OpenSky by callsign, Leaflet map, trail.
- **Phase 3** — daily digest, flight history, auto-purge, OpenSky OAuth2 fallback.

## License

See [LICENSE](LICENSE).
