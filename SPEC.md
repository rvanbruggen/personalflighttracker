# Personal Flight Tracker — Feasibility & Proposal

*Prepared for Rik Van Bruggen — 26 August 2026*

## Verdict

Yes, this is feasible at essentially zero cost. The one nuance: **no single free API gives you both flight status and live position**, so the design combines two free sources. Volume matters — free tiers comfortably cover a personal handful of flights per month, not hundreds.

## The data problem, in one paragraph

"Flight status" (scheduled/delayed/cancelled, gates, times, keyed by flight number like KL1234) and "aircraft position" (lat/lon/altitude, keyed by transponder callsign/ICAO hex) come from different worlds. ADS-B community networks give you position for free but know nothing about delays or gates; airline-schedule APIs give you status but charge for volume. The free sweet spot:

| Source | Gives you | Free tier | Notes |
|---|---|---|---|
| [AeroDataBox](https://rapidapi.com/aedbx-aedbx/api/aerodatabox/pricing) (via RapidAPI) | Status by flight number + date: times, delays, gates, aircraft, callsign | ~600 API units/month (status call = 2 units), 1 req/sec | The workhorse for status. Free tier ≈ 250–300 status calls/month |
| [OpenSky Network](https://openskynetwork.github.io/opensky-api/rest.html) | Live position by callsign/ICAO24 | 4,000 credits/day registered (8,000 if you feed data); OAuth2 client-credentials since March 2026 | Non-commercial use; no schedule/delay data |
| [adsb.lol](https://www.adsb.lol/) | Live position by callsign, no API key needed (today) | Unlimited-ish, community-run | ADSB-Exchange-compatible API; key requirement planned for the future |
| aviationstack | Status by flight number | ~100 requests/month | Too small; backup only |
| SkyLink, FlightAware AeroAPI | Both, higher quality | Paid ($) | Upgrade path if free tiers ever pinch |

Sources: [OpenSky API docs](https://openskynetwork.github.io/opensky-api/rest.html), [AeroDataBox pricing](https://aerodatabox.com/pricing), [adsb.lol API on GitHub](https://github.com/adsblol/api), [free flight API comparison 2026](https://skylinkapi.com/blog/free-flight-tracking-apis-2026/), [Thunderbit flight API free tiers](https://thunderbit.com/blog/best-flight-api-with-free-tiers).

## Notifications: Gmail + IFTTT (your choice)

Chosen approach: the app sends email via Gmail SMTP (free, using a Gmail app password), and IFTTT turns matching emails into phone notifications.

One important detail: IFTTT retired its Gmail *triggers* years ago, so "watch my inbox for a subject" is not reliable. The supported pattern is IFTTT's own **Email service**: the app sends a second copy of each alert to `trigger@applet.ifttt.com` (from your registered Gmail address) with a hashtag in the subject, e.g. `KL1234 DELAYED 45min #flight` — that fires the applet, which sends a rich notification to your phone. ([IFTTT Email service](https://ifttt.com/connect/email/if_notifications), [IFTTT email changes](https://help.ifttt.com/hc/en-us/articles/12386174466331-Important-Changes-to-the-SMS-Phone-Call-and-Email-Services))

So each alert = two emails from the app: one to you (readable record in your inbox) and one to the IFTTT trigger address (phone notification). IFTTT's free plan allows 2 applets ([IFTTT plans](https://help.ifttt.com/hc/en-us/articles/360053706813-IFTTT-Plans-at-a-glance)) — you only need one. Free-tier rate limits (30/day on some email actions) are irrelevant at flight-alert volume.

WhatsApp stays dropped: Meta Business API only, no sane free path. If IFTTT ever squeezes its free tier further, ntfy (a free open-source push app, one extra container) is the drop-in replacement.

## Proposed architecture

Target machine: your old MacBook running **Linux Mint + Docker** on your local network — an ideal always-on host. One `docker compose up`, a single container:

```
┌────────── Mint MacBook (LAN, always on) ──────────┐
│  ┌─────────────────────────┐                      │
│  │  app (Python/FastAPI)   │──▶ Gmail SMTP ──▶ your inbox
│  │  • web UI (register     │        └──▶ trigger@applet.ifttt.com
│  │    flight no + date)    │                  └──▶ IFTTT ──▶ phone
│  │  • scheduler (APSchedu- │──▶ AeroDataBox  (status, sparse)
│  │    ler, adaptive poll)  │──▶ adsb.lol / OpenSky (position)
│  │  • SQLite state         │                      │
│  │  • Leaflet + OSM map    │                      │
│  └─────────────────────────┘                      │
└───────────────────────────────────────────────────┘
        ▲ browse from any device on your LAN:
          http://<mint-macbook-ip>:8080
```

How it works: from any machine on your network you open `http://<mint-macbook-ip>:8080` and register a flight (number + date). The scheduler polls AeroDataBox adaptively — once when registered, hourly from 24h before departure, every 10 min from 2h before, every 5 min while airborne. Each status response includes the callsign, which unlocks free position polling against adsb.lol (OpenSky as fallback) for a live Leaflet map. Any change (delay, gate, departed, diverted, landed) is diffed against SQLite and emailed via Gmail SMTP — to you, and to the IFTTT trigger address for the phone notification.

**Quota math:** that schedule is roughly 30–50 status calls per flight → the free AeroDataBox tier covers **5–8 flights/month** with margin; position polling is free and effectively unmetered. Track more and it's $10–20/month for the next tier — still far below any commercial alerting product.

## Build plan

- **Phase 1 — MVP (a weekend):** Docker Compose, FastAPI app, flight registration form, adaptive status poller, Gmail alerts + IFTTT applet (Email trigger → phone notification). This alone replaces the FlightRadar24-with-ads experience.
- **Phase 2 — Live map (a few evenings):** callsign→adsb.lol position polling, Leaflet map with OpenStreetMap tiles, flight trail.
- **Phase 3 — Comfort:** email digest, flight history, auto-purge landed flights, OpenSky OAuth2 fallback source.

## Caveats

- The Mint MacBook must stay powered on with lid-close sleep disabled (`/etc/systemd/logind.conf` → `HandleLidSwitch=ignore`); as an always-on LAN server this is a one-time setting.
- Gmail SMTP needs an app password (requires 2FA on the Google account); IFTTT's Email trigger fires only on mail sent from the address registered with IFTTT.
- Community ADS-B coverage has gaps over oceans and parts of Africa/Asia — position may drop out mid-ocean even though status updates continue.
- Free tiers change; the architecture keeps sources behind one interface so swapping providers is a one-file change.

## Estimated cost

Software: €0 (all open source). APIs: €0 at your volume. Hardware: your MacBook. Total: **€0/month**.
