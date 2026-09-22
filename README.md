# Personal Flight Tracker

A personal flight tracker that you can run for free on your LAN.

Register a flight number + date in a web page on your home network. The app
polls flight status on an adaptive schedule, diffs every response against what
it already knew, and emails you the moment something changes — a delay, a gate
change, a cancellation, wheels-up, wheels-down. Each alert also fires an IFTTT
webhook, which turns it into a push notification on your phone.

Runs as a single container. €0/month at personal volume.

**Phases 1 and 2** of [SPEC.md](SPEC.md) are built: registration, adaptive status
polling, alerting, and the live map with flight trail. Phase 3 (digest, history,
auto-purge, OpenSky fallback) is not started.

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

### 3. IFTTT webhook (optional — this is the phone notification)

Without this you still get emails; you just don't get a push notification.
The Webhooks service needs **IFTTT Pro**.

1. Install the IFTTT app on your phone and sign in.
2. Open [ifttt.com/maker_webhooks](https://ifttt.com/maker_webhooks), click
   **Documentation**, and copy your key into `IFTTT_WEBHOOK_KEY` in `.env`.
   Treat it like a password: anyone with it can fire your applets.
3. Create an applet:
   - **If:** Webhooks → *Receive a web request*, event name `flight_notification`
     (or whatever you set in `IFTTT_EVENT`).
   - **Then:** Notifications → *Send a rich notification from the IFTTT app*,
     with Title `{{Value1}}`, Message `{{Value2}}`, Link URL `{{Value3}}`.
4. Set `PUBLIC_BASE_URL` to the address your phone uses for this app (e.g.
   `http://192.168.68.78:8080`) so tapping the notification opens the flight.
   The link only works while your phone is on the home network.

What each push carries:

| Ingredient | Content | Example |
|---|---|---|
| `Value1` | Alert title | `KL1705 AMS→LIS DELAYED +45min` |
| `Value2` | What changed, one per line | `Departure: 09:15 → 10:00` |
| `Value3` | Link to the flight page | `http://192.168.68.78:8080/flights/3` |

The push is sent directly over HTTPS, independent of Gmail: a Gmail failure
doesn't block it, and a push failure doesn't block the email. Each outcome is
noted in the flight's change history.

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
Gmail and the IFTTT webhook in one shot: your inbox should get a mail, and your
phone should buzz. The confirmation banner reports each channel separately.

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
app refuses to poll once the unit budget is spent (a manual **Refresh now** can
still override that for one call).

### When the quota resets

**RapidAPI resets on your subscription's billing anniversary, not on the 1st of
the calendar month.** Subscribe on the 26th and your window runs 26th → 26th.

Set `AERODATABOX_QUOTA_RESET_DAY` to the day you subscribed so the app's counter
tracks the same window. Leave it at `1` only if that's genuinely your billing
day — otherwise the header will reset to zero while RapidAPI is still counting,
and you can hit real HTTP 429s with the bar showing plenty left.

Your RapidAPI dashboard is authoritative: it shows actual usage and the true
reset date. The app's counter only tallies calls *this app* made, so a call from
anywhere else on the same key won't appear.

adsb.lol has no quota to reset — it is free and unmetered.

## What triggers an alert

A change is only an alert if it matters. Status, departure gate, departure
terminal, and any time change over 2 minutes will email you. Arrival gate,
baggage belt, aircraft registration, and callsign are recorded in the flight's
history but stay silent.

The app never treats *missing* data as a change — providers drop fields
intermittently, and that shouldn't wake your phone at 4am.

### Who gets notified

Your default address — `MAIL_TO`, or `SMTP_USER` if that's empty — receives
every alert for every flight. It can't be removed from a flight.

On top of that, each flight can have up to 10 **extra recipients**: fill in
*Also notify* when registering, or edit the list later in the *Notifications*
panel on the flight's page. Addresses are comma-separated, and case and
duplicates don't matter.

- Each person gets **their own copy**, so nobody sees anyone else's address,
  and one bad address can't block delivery to the others.
- Extra recipients' copies end with a short note explaining why they are
  receiving it.
- The flight's change history records exactly who each alert reached, and
  logs whenever recipients are added or removed.
- The test alert only ever goes to the default address.

**Confirmation emails.** When you register a flight, everyone on it gets a
*tracking started* email with the flight's current details and what alerts to
expect — so nobody's first message is an unexplained delay alert. Anyone you
add to a flight later gets a *you've been added* email; people already on the
list don't hear about it again. Removing someone sends nothing.

Confirmations reuse the poll that registration already makes, so they cost no
API quota, and they never go to your phone, which stays reserved for real changes.
Turn them off with `SEND_TRACKING_CONFIRMATIONS=false`.

Every alert subject is prefixed with `PFT`, so they are easy to spot and to
filter on in Gmail:

```
PFT KL1705 AMS→LIS DELAYED +45min
```

### What the emails look like

Every email is designed HTML with a plain-text version alongside, so it still
reads well in clients that block HTML:

- **Header and banner** — the PFT logo, then a coloured banner for the kind of
  event (red cancelled/diverted, amber delayed, blue departed, green landed,
  purple gate change) and a plain-words headline such as
  *"SN3811 has departed, 45 min late"*.
- **What changed** — bullets with the old value struck through. Times are shown
  in the airport's local time, like the rest of the email.
- **Flight details** — status, departure and arrival (local), terminal and gate,
  baggage belt, aircraft.
- **Map** — a picture of the planned route (great circle), the path flown so
  far, and the aircraft's last position, drawn from OpenStreetMap tiles.
- **When you'll hear from us next** — the next check time and the current
  checking rhythm, and a reminder that emails only come when something
  changes. The last email for a flight says tracking has ended.
- **Where this information comes from** — AeroDataBox for status (with when it
  was last checked), adsb.lol for position, OpenStreetMap for the map.

To see a flight's emails without sending anything, use the *Preview the email*
links on its page.

**About the map image.** Email clients can't run the interactive map, and
OpenStreetMap has no official static-map service, so the app stitches a handful
of standard OSM tiles together itself. In line with OSM's tile policy it sends
an identifying User-Agent, caches tiles on disk (`data/tiles`, 14 days), and
prints the OpenStreetMap credit on the image. Set `EMAIL_MAP_ENABLED=false` to
leave maps out; if the tiles can't be fetched the email still goes out, with
a plain background or without the map.

---

## The live map

Each flight's detail page carries a Leaflet map on OpenStreetMap tiles showing
the origin and destination, the planned route, the flown trail, and the aircraft
itself with altitude, ground speed and heading.

Positions come from **[adsb.lol](https://www.adsb.lol/)** — community-run, free,
no API key, and completely separate from your AeroDataBox quota. Polling only
happens while a flight is actually airborne, so an empty tracker costs nothing.

Two things to know:

- **adsb.lol requires a real contact point** in the User-Agent and returns
  HTTP 403 without one. `ADSBLOL_CONTACT` defaults to this project's URL;
  change it to your own URL or email if you like.
- **Coverage has gaps.** Community ADS-B receivers thin out over oceans and
  parts of Africa and Asia, so the aircraft will disappear from the map
  mid-Atlantic while status updates keep arriving. The map says so rather than
  showing a stale position — fixes older than `POSITION_MAX_AGE_SECONDS` are
  discarded.

### Callsigns

Position lookup is keyed on the ADS-B callsign (`BAW117`), not the flight number
(`BA117`). AeroDataBox usually supplies it. When it doesn't, the callsign is
derived from the airline's ICAO code plus the flight number — the convention
most carriers follow.

A derived callsign is **labelled as unconfirmed** in the UI and flagged as
`callsign_derived` in the API, because not every carrier follows the convention.
When neither route yields a callsign, the map says live position is unavailable
rather than showing a possibly-wrong aircraft.

## The web pages

The flight list and each flight's page **keep themselves up to date**. Every
30 seconds they ask the tracker whether anything they show has changed, and
reload only if it has — so an unchanged page never flickers. Reloads wait
while you're typing in a form (the footer says so), keep your scroll
position, and don't bring back one-off messages. A tab in the background
doesn't poll; it checks as soon as you return to it. The live map updates on
its own and doesn't trigger page reloads.

"Last checked" / "next check" and the history timestamps are shown in **your
browser's time zone** (hover for the zone and the UTC time). Departure and
arrival times stay in each airport's local time.

## Configuration

Every setting lives in `.env`; see [.env.example](.env.example) for the annotated
list. `.env` is gitignored — **secrets never enter the repo.**

The ones worth knowing:

| Variable | Default | Purpose |
|---|---|---|
| `AERODATABOX_API_KEY` | — | RapidAPI key. Required. |
| `AERODATABOX_MONTHLY_UNIT_BUDGET` | `600` | Hard stop. Lower it if you share the key. |
| `AERODATABOX_QUOTA_RESET_DAY` | `1` | Day of month your RapidAPI quota resets — see below. |
| `SMTP_USER` / `SMTP_PASSWORD` | — | Gmail address + **app password**. |
| `MAIL_TO` | = `SMTP_USER` | Where readable alerts land. |
| `IFTTT_WEBHOOK_KEY` | — | Webhooks key (IFTTT Pro). Empty = no phone push. |
| `IFTTT_EVENT` | `flight_notification` | Must match the applet's event name. |
| `IFTTT_ENABLED` | `true` | Set `false` to pause phone pushes and keep the key. |
| `PUBLIC_BASE_URL` | — | This app's LAN address, for tap-to-open links in pushes. |
| `NOTIFICATIONS_ENABLED` | `true` | `false` records changes silently — handy for testing. |
| `EMAIL_SUBJECT_PREFIX` | `PFT` | Prepended to every email subject. Empty for none. |
| `SEND_TRACKING_CONFIRMATIONS` | `true` | "Tracking started" / "you've been added" emails. |
| `EMAIL_MAP_ENABLED` | `true` | Include a route map image in emails. |
| `AUTO_REFRESH_SECONDS` | `30` | How often open pages check for updates. `0` turns auto-refresh off. |
| `MAP_TILE_URL` | OSM standard tiles | Tile source for the email map. |
| `POLL_*` | see table above | Status cadence tuning. |
| `POSITIONS_ENABLED` | `true` | `false` disables the map and position polling. |
| `ADSBLOL_CONTACT` | project URL | Contact point adsb.lol requires; 403 without it. |
| `POSITION_POLL_SECONDS` | `60` | How often to fetch a fix while airborne. |
| `HOST_PORT` | `8080` | Port on the host. `80` gives you a bare URL. |

## Endpoints

| Path | What |
|---|---|
| `GET /` | Registration form + tracked flights |
| `GET /flights/{id}` | One flight: current state, change history, raw provider JSON |
| `GET /healthz` | Config + scheduler + quota status |
| `GET /api/fingerprint[?flight_id=]` | Short hash of a page's content; open pages reload when it changes |
| `GET /api/flights` | JSON list of tracked flights, including each one's recipients |
| `GET /api/flights/{id}/track` | Trail, endpoints, and latest fix — what the map consumes |
| `GET /flights/{id}/email-preview?kind=alert\|started\|welcome` | Show that flight's email in the browser; sends nothing |

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

### Upgrading

The app migrates SQLite automatically on startup, so upgrading is always
`git pull && docker compose up -d --build`. Existing flights keep their data;
new columns start empty. The log line `Schema migrated — added: …` confirms it.

### Upgrading from v0.1.0

Phase 2 adds columns to the `flights` table and a new `flight_positions` table.
The app migrates SQLite automatically on startup — existing flights keep their
data, and the new columns start empty. Just `git pull && docker compose up -d --build`;
the log line `Schema migrated — added: …` confirms it ran.

### Layout

```
app/
  main.py         FastAPI routes, APScheduler wiring
  tracker.py      Adaptive poll cadence, quota guard, alert dispatch
  diffing.py      Snapshot → list of human-readable changes
  notify.py       Gmail SMTP alerts; hands the phone copy to push.py
  push.py         IFTTT webhook (phone push)
  db.py           SQLite models (flights, events, api_calls)
  config.py       .env-backed settings
  callsign.py     Callsign resolution, with ICAO-derivation fallback
  recipients.py   Per-flight extra recipients: parsing and validation
  email_render.py HTML emails: headline, details, next update, sources
  cadence.py      Polling phases, shared by the tracker and the emails
  staticmap.py    Map image for emails, from cached OpenStreetMap tiles
  graphics.py     Logo and plane shape (scripts/make_logo.py rebuilds the PNG)
  static/map.js   Leaflet map: trail, route, aircraft marker
  providers/
    base.py       FlightSnapshot / PositionFix + the provider interfaces
    aerodatabox.py  The only file that knows about AeroDataBox
    adsblol.py      The only file that knows about adsb.lol
```

Swapping status providers means writing one new file in `providers/` that
returns a `FlightSnapshot` — nothing else changes.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `AeroDataBox rejected the API key (HTTP 401/403)` | Key wrong, or you haven't subscribed to the Basic plan on RapidAPI. |
| `Gmail rejected the login` | Using your account password. It must be a 16-character app password, with 2FA enabled. |
| Emails arrive, phone stays silent | Check the change history for a `Phone push failed:` line. `HTTP 401` means a wrong `IFTTT_WEBHOOK_KEY`. If the push says sent, the applet's event name doesn't match `IFTTT_EVENT`, or notifications for the IFTTT app are off on your phone. |
| `has no record of ... yet` | Normal far in advance. The app retries and gives up ~12h after the scheduled arrival. |
| Header shows quota exhausted | Free tier spent for this billing period. The header shows the reset date; `Refresh now` still works for one-off checks. |
| Permission errors on `./data` | Container runs as uid 1000. `sudo chown -R 1000:1000 data`. |
| Map shows "no receiver is currently seeing…" | Normal ADS-B coverage gap, especially over water. Status keeps updating. |
| `adsb.lol rejected the request (HTTP 403)` | Set `ADSBLOL_CONTACT` to a URL or email they can reach you at. |
| Map is blank with no aircraft | Flight isn't airborne yet — position polling only runs between departure and arrival. |

## Roadmap

- **Phase 3** — daily digest, flight history, auto-purge landed flights, and an
  OpenSky OAuth2 fallback for when adsb.lol has no coverage.

## License

See [LICENSE](LICENSE).
