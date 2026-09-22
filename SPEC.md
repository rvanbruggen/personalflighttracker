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

## Notifications: Gmail + IFTTT Webhooks

Each alert goes out over two independent channels: an email via Gmail SMTP (free, using a Gmail app password) as the readable record, and a phone push via an [IFTTT Webhooks](https://ifttt.com/maker_webhooks) call: an HTTPS POST of `value1/value2/value3` (title, what changed, link to the flight page) to `maker.ifttt.com`. An applet (*Receive a web request* → *Send a rich notification from the IFTTT app*) turns it into a notification.

Webhooks is an IFTTT Pro service ([Webhooks FAQ](https://help.ifttt.com/hc/en-us/articles/115010230347-Webhooks-service-FAQ)); Pro is already paid for, and a direct call is faster and more robust than the earlier design, which emailed a hashtagged copy to IFTTT's Email trigger (now removed). Neither channel blocks the other. Tracking confirmations stay email-only.

WhatsApp stays dropped: Meta Business API only, no sane free path. If IFTTT ever stops being worth paying for, ntfy (a free open-source push app) is the drop-in replacement: the push channel lives in one module, `app/push.py`.

## Proposed architecture

Target machine: your old MacBook running **Linux Mint + Docker** on your local network — an ideal always-on host. One `docker compose up`, a single container:

```
┌────────── Mint MacBook (LAN, always on) ──────────┐
│  ┌─────────────────────────┐                      │
│  │  app (Python/FastAPI)   │──▶ Gmail SMTP ──▶ your inbox
│  │  • web UI (register     │──▶ IFTTT webhook ──▶ phone
│  │    flight no + date)    │                      │
│  │  • scheduler (APSchedu- │──▶ AeroDataBox  (status, sparse)
│  │    ler, adaptive poll)  │──▶ adsb.lol / OpenSky (position)
│  │  • SQLite state         │                      │
│  │  • Leaflet + OSM map    │                      │
│  └─────────────────────────┘                      │
└───────────────────────────────────────────────────┘
        ▲ browse from any device on your LAN:
          http://<mint-macbook-ip>:8080
```

How it works: from any machine on your network you open `http://<mint-macbook-ip>:8080` and register a flight (number + date). The scheduler polls AeroDataBox adaptively — once when registered, hourly from 24h before departure, every 10 min from 2h before, every 5 min while airborne. Each status response includes the callsign, which unlocks free position polling against adsb.lol (OpenSky as fallback) for a live Leaflet map. Any change (delay, gate, departed, diverted, landed) is diffed against SQLite and emailed via Gmail SMTP and pushed to your phone through an IFTTT webhook.

**Quota math:** that schedule is roughly 30–50 status calls per flight → the free AeroDataBox tier covers **5–8 flights/month** with margin; position polling is free and effectively unmetered. Track more and it's $10–20/month for the next tier — still far below any commercial alerting product.

## Build plan

- **Phase 1 — MVP (a weekend):** Docker Compose, FastAPI app, flight registration form, adaptive status poller, Gmail alerts + IFTTT applet (originally the Email trigger; now Webhooks → phone notification). This alone replaces the FlightRadar24-with-ads experience.
- **Phase 2 — Live map (a few evenings):** callsign→adsb.lol position polling, Leaflet map with OpenStreetMap tiles, flight trail.
- **Phase 3 — Comfort:** email digest, flight history, auto-purge landed flights, OpenSky OAuth2 fallback source.

## Caveats

- The Mint MacBook must stay powered on with lid-close sleep disabled (`/etc/systemd/logind.conf` → `HandleLidSwitch=ignore`); as an always-on LAN server this is a one-time setting.
- Gmail SMTP needs an app password (requires 2FA on the Google account); IFTTT Webhooks needs a Pro subscription, and its key must stay out of the repo.
- Community ADS-B coverage has gaps over oceans and parts of Africa/Asia — position may drop out mid-ocean even though status updates continue.
- Free tiers change; the architecture keeps sources behind one interface so swapping providers is a one-file change.

## Estimated cost

Software: €0 (all open source). APIs: €0 at your volume. Hardware: your MacBook. Total: **€0/month**.
