"""FastAPI app: flight registration UI + the scheduler that drives polling."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .callsign import resolve_callsign
from .recipients import (
    MAX_EXTRA_RECIPIENTS,
    all_recipients,
    default_recipient,
    extra_recipients,
    parse_recipients,
    serialise,
)
from .db import Flight, FlightEvent, SessionLocal, init_db, utcnow
from .notify import send_test_alert
from .providers.aerodatabox import provider as status_provider
from .tracker import (
    flight_callsign,
    poll_flight,
    poll_position,
    position_tick,
    quota_status,
    send_tracking_confirmation,
    step_from_flight,
    tick,
)
from .email_render import FlightView, build_email

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("flighttracker")

FLIGHT_NUMBER_RE = re.compile(r"^[A-Z0-9]{2,3}\d{1,4}[A-Z]?$")

scheduler = AsyncIOScheduler(timezone="UTC")


def normalise_flight_number(raw: str) -> str:
    """'kl 1234' / 'KL-1234' -> 'KL1234'."""
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()


def validate_date(raw: str) -> str:
    try:
        parsed = date.fromisoformat((raw or "").strip())
    except ValueError:
        raise ValueError("Date must be in YYYY-MM-DD format.")
    today = datetime.now(timezone.utc).date()
    if parsed < today - timedelta(days=2):
        raise ValueError("That date is in the past — nothing left to track.")
    if parsed > today + timedelta(days=365):
        raise ValueError("That date is more than a year out.")
    return parsed.isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrated = init_db()
    log.info("Database ready at %s", settings.database_url)
    if migrated:
        log.info("Schema migrated — added: %s", ", ".join(migrated))

    if not settings.aerodatabox_configured:
        log.warning(
            "AERODATABOX_API_KEY is not set — flights can be registered but "
            "status polling will fail. See README.md."
        )
    if not settings.smtp_configured:
        log.warning(
            "SMTP is not configured — changes will be recorded but no email "
            "will be sent. Set SMTP_USER / SMTP_PASSWORD in .env."
        )

    scheduler.add_job(
        tick,
        "interval",
        minutes=1,
        id="poll-tick",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),
    )
    if settings.positions_enabled:
        scheduler.add_job(
            position_tick,
            "interval",
            seconds=max(settings.position_poll_seconds, 15),
            id="position-tick",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
        )

    scheduler.start()
    log.info(
        "Scheduler started — status every minute, positions every %ss (%s).",
        settings.position_poll_seconds,
        "enabled" if settings.positions_enabled else "disabled",
    )
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        log.info("Scheduler stopped.")


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _flash(request: Request, url: str, message: str, level: str = "ok") -> RedirectResponse:
    separator = "&" if "?" in url else "?"
    return RedirectResponse(
        f"{url}{separator}msg={message}&level={level}", status_code=303
    )


# --------------------------------------------------------------------------- UI


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, msg: str = "", level: str = "ok"):
    with SessionLocal() as session:
        flights = (
            session.query(Flight)
            .order_by(Flight.tracking_state != "active", Flight.dep_scheduled_utc.is_(None), Flight.dep_scheduled_utc)
            .all()
        )
        recent = (
            session.query(FlightEvent)
            .order_by(FlightEvent.created_at.desc())
            .limit(10)
            .all()
        )
        events_by_flight = {
            flight.id: flight for flight in flights
        }

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "flights": flights,
            "recent": recent,
            "flights_by_id": events_by_flight,
            "recipient_counts": {
                flight.id: len(extra_recipients(flight.notify_emails)) for flight in flights
            },
            "default_recipient": default_recipient(),
            "quota": quota_status(),
            "settings": settings,
            "today": datetime.now(timezone.utc).date().isoformat(),
            "message": msg,
            "level": level,
            "now": utcnow(),
        },
    )


@app.post("/flights")
async def register_flight(
    request: Request,
    flight_number: str = Form(...),
    flight_date: str = Form(...),
    label: str = Form(""),
    notify_emails: str = Form(""),
):
    number = normalise_flight_number(flight_number)
    if not FLIGHT_NUMBER_RE.match(number):
        return _flash(
            request, "/", f"'{flight_number}' is not a valid flight number (try KL1234).", "error"
        )
    try:
        iso_date = validate_date(flight_date)
    except ValueError as exc:
        return _flash(request, "/", str(exc), "error")

    recipients = parse_recipients(notify_emails)
    if recipients.error:
        # Reject the whole registration rather than silently drop an address
        # someone expected to be alerted.
        return _flash(request, "/", recipients.error, "error")

    with SessionLocal() as session:
        existing = (
            session.query(Flight)
            .filter(Flight.flight_number == number, Flight.flight_date == iso_date)
            .first()
        )
        if existing:
            return _flash(
                request, f"/flights/{existing.id}", f"{number} on {iso_date} is already tracked.", "warn"
            )
        flight = Flight(
            flight_number=number,
            flight_date=iso_date,
            label=(label or "").strip()[:200],
            notify_emails=serialise(recipients.emails),
            next_poll_at=utcnow().replace(tzinfo=None),
        )
        session.add(flight)
        session.commit()
        flight_id = flight.id

    # First poll immediately, so the user sees a result right away.
    outcome = await poll_flight(flight_id)
    log.info("Registered %s on %s: %s", number, iso_date, outcome)
    extra = f" Alerts also go to {len(recipients.emails)} other address(es)." if recipients.emails else ""
    confirmation = await asyncio.to_thread(send_tracking_confirmation, flight_id)
    return _flash(
        request,
        f"/flights/{flight_id}",
        f"Tracking {number} — {outcome}.{extra}{_confirmation_note(confirmation)}",
        "ok",
    )


def _confirmation_note(result) -> str:
    """Flash-message suffix describing the confirmation email, if any."""
    if result is None:
        return ""
    reached = (1 if result.inbox_sent else 0) + len(result.extra_sent)
    if reached:
        failed = f", {len(result.extra_failed)} failed" if result.extra_failed else ""
        return f" Confirmation emailed to {reached} address(es){failed}."
    if result.error:
        return f" Confirmation not sent: {result.error}"
    return ""


@app.get("/flights/{flight_id}", response_class=HTMLResponse)
async def flight_detail(request: Request, flight_id: int, msg: str = "", level: str = "ok"):
    with SessionLocal() as session:
        flight = session.get(Flight, flight_id)
        if flight is None:
            raise HTTPException(status_code=404, detail="Flight not tracked")
        events = list(flight.events)
        resolved = flight_callsign(flight)

    return templates.TemplateResponse(
        request,
        "flight.html",
        {
            "flight": flight,
            "events": events,
            "callsign": resolved,
            "positions_enabled": settings.positions_enabled,
            "default_recipient": default_recipient(),
            "extra_recipients": extra_recipients(flight.notify_emails),
            "max_recipients": MAX_EXTRA_RECIPIENTS,
            "settings": settings,
            "quota": quota_status(),
            "message": msg,
            "level": level,
            "now": utcnow(),
        },
    )


@app.get("/flights/{flight_id}/email-preview", response_class=HTMLResponse)
async def email_preview(flight_id: int, kind: str = "alert"):
    """Show the email a flight would send, in the browser. Sends nothing.

    kind=alert replays the most recent change (or shows a sample if there is
    none); kind=started / welcome show the confirmation emails."""
    import base64
    import re as _re

    if kind not in {"alert", "started", "welcome"}:
        raise HTTPException(status_code=400, detail="kind must be alert, started or welcome")

    with SessionLocal() as session:
        flight = session.get(Flight, flight_id)
        if flight is None:
            raise HTTPException(status_code=404, detail="Flight not tracked")
        view = FlightView.from_flight(flight, flight_callsign(flight))
        step = step_from_flight(flight)
        changes: list[tuple[str, str, str]] = []
        if kind == "alert":
            latest = (
                session.query(FlightEvent)
                .filter_by(flight_id=flight_id, kind="change")
                .order_by(FlightEvent.created_at.desc())
                .first()
            )
            for line in (latest.detail if latest else "").splitlines():
                match = _re.match(r"^(.+?): (.*) → (.*)$", line)
                if match:
                    old, new = match.group(2), match.group(3)
                    changes.append((match.group(1), "" if old == "—" else old, new))
            if not changes and view.status:
                changes = [("Status", "", view.status)]

    rendered = await asyncio.to_thread(build_email, kind, view, changes, step)
    html = rendered.html
    for cid, data in rendered.images.items():
        html = html.replace(f"cid:{cid}", "data:image/png;base64," + base64.b64encode(data).decode())
    return HTMLResponse(html)


@app.post("/flights/{flight_id}/refresh")
async def refresh_flight(request: Request, flight_id: int):
    outcome = await poll_flight(flight_id, force=True)
    return _flash(request, f"/flights/{flight_id}", f"Refreshed — {outcome}.", "ok")


@app.post("/flights/{flight_id}/recipients")
async def update_recipients(request: Request, flight_id: int, notify_emails: str = Form("")):
    """Replace this flight's extra recipients with the submitted list."""
    url = f"/flights/{flight_id}"
    recipients = parse_recipients(notify_emails)
    if recipients.error:
        return _flash(request, url, recipients.error, "error")

    with SessionLocal() as session:
        flight = session.get(Flight, flight_id)
        if flight is None:
            raise HTTPException(status_code=404, detail="Flight not tracked")
        before = set(extra_recipients(flight.notify_emails))
        flight.notify_emails = serialise(recipients.emails)
        after = set(recipients.emails)
        added, removed = sorted(after - before), sorted(before - after)
        if added or removed:
            bits = []
            if added:
                bits.append("added " + ", ".join(added))
            if removed:
                bits.append("removed " + ", ".join(removed))
            session.add(
                FlightEvent(flight_id=flight.id, kind="info",
                            summary="Recipients updated", detail="; ".join(bits))
            )
        session.commit()

    if not (added or removed):
        return _flash(request, url, "Recipients unchanged.", "ok")
    note = " (Your default address always receives alerts, so it isn't listed.)" if recipients.dropped_default else ""
    # Only the newly added people get a welcome; everyone else already knows.
    welcome = (
        await asyncio.to_thread(send_tracking_confirmation, flight_id, added)
        if added
        else None
    )
    return _flash(
        request,
        url,
        f"Recipients updated: {len(after)} extra.{note}{_confirmation_note(welcome)}",
        "ok",
    )


@app.post("/flights/{flight_id}/recipients/remove")
async def remove_recipient(request: Request, flight_id: int, email: str = Form(...)):
    url = f"/flights/{flight_id}"
    target = (email or "").strip().lower()
    with SessionLocal() as session:
        flight = session.get(Flight, flight_id)
        if flight is None:
            raise HTTPException(status_code=404, detail="Flight not tracked")
        current = extra_recipients(flight.notify_emails)
        if target not in current:
            return _flash(request, url, f"{target} was not a recipient.", "warn")
        flight.notify_emails = serialise([e for e in current if e != target])
        session.add(FlightEvent(flight_id=flight.id, kind="info",
                                summary="Recipients updated", detail=f"removed {target}"))
        session.commit()
    return _flash(request, url, f"Removed {target}.", "ok")


@app.post("/flights/{flight_id}/delete")
async def delete_flight(request: Request, flight_id: int):
    with SessionLocal() as session:
        flight = session.get(Flight, flight_id)
        if flight is not None:
            session.delete(flight)
            session.commit()
    return _flash(request, "/", "Flight removed.", "ok")


@app.post("/flights/{flight_id}/resume")
async def resume_flight(request: Request, flight_id: int):
    with SessionLocal() as session:
        flight = session.get(Flight, flight_id)
        if flight is None:
            raise HTTPException(status_code=404, detail="Flight not tracked")
        flight.tracking_state = "active"
        flight.next_poll_at = utcnow().replace(tzinfo=None)
        flight.consecutive_errors = 0
        session.commit()
    return _flash(request, f"/flights/{flight_id}", "Tracking resumed.", "ok")


@app.post("/test-alert")
async def test_alert(request: Request):
    result = await asyncio.to_thread(send_test_alert)
    if settings.ifttt_configured:
        phone = "sent" if result.ifttt_sent else f"failed: {result.push_error}"
    else:
        phone = "off (no IFTTT_WEBHOOK_KEY)"
    if result.error:
        return _flash(
            request, "/", f"Test alert failed: {result.error} · Phone push {phone}", "error"
        )
    return _flash(
        request,
        "/",
        f"Test alert sent (inbox={result.inbox_sent}) · Phone push {phone}.",
        "ok" if result.ifttt_sent or not settings.ifttt_configured else "error",
    )


# ------------------------------------------------------------------------- API


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "aerodatabox_configured": settings.aerodatabox_configured,
        "smtp_configured": settings.smtp_configured,
        "ifttt_configured": settings.ifttt_configured,
        "scheduler_running": scheduler.running,
        "positions_enabled": settings.positions_enabled,
        "position_source": "adsb.lol",
        "quota": quota_status(),
    }


@app.get("/api/flights/{flight_id}/track")
async def api_flight_track(flight_id: int):
    """Everything the map needs: endpoints, trail, and the latest fix."""
    with SessionLocal() as session:
        flight = session.get(Flight, flight_id)
        if flight is None:
            raise HTTPException(status_code=404, detail="Flight not tracked")

        resolved = flight_callsign(flight)
        trail = [
            {
                "lat": p.lat,
                "lon": p.lon,
                "altitude_ft": p.altitude_ft,
                "ground_speed_kt": p.ground_speed_kt,
                "track_deg": p.track_deg,
                "recorded_at": p.recorded_at.isoformat() + "Z",
            }
            for p in flight.positions
        ]

        return JSONResponse(
            {
                "flight_number": flight.flight_number,
                "status": flight.status,
                "tracking_state": flight.tracking_state,
                "airborne": flight.status in {
                    "EnRoute", "En Route", "Departed", "Approaching", "Diverted",
                },
                "callsign": resolved.value if resolved else "",
                "callsign_derived": resolved.derived if resolved else False,
                "departure": {
                    "iata": flight.dep_iata,
                    "name": flight.dep_name,
                    "lat": flight.dep_lat,
                    "lon": flight.dep_lon,
                },
                "arrival": {
                    "iata": flight.arr_iata,
                    "name": flight.arr_name,
                    "lat": flight.arr_lat,
                    "lon": flight.arr_lon,
                },
                "trail": trail,
                "latest": trail[-1] if trail else None,
                "position_source": flight.position_source,
                "position_error": flight.position_error,
                "last_position_at": flight.last_position_at.isoformat() + "Z"
                if flight.last_position_at
                else None,
            }
        )


@app.post("/flights/{flight_id}/position")
async def refresh_position(request: Request, flight_id: int):
    outcome = await poll_position(flight_id, force=True)
    return _flash(request, f"/flights/{flight_id}", f"Position: {outcome}", "ok")


@app.get("/api/flights")
async def api_flights():
    with SessionLocal() as session:
        flights = session.query(Flight).order_by(Flight.id.desc()).all()
        return JSONResponse(
            [
                {
                    "id": flight.id,
                    "flight_number": flight.flight_number,
                    "flight_date": flight.flight_date,
                    "label": flight.label,
                    "tracking_state": flight.tracking_state,
                    "status": flight.status,
                    "callsign": flight.callsign,
                    "route": flight.route,
                    "departure_local": flight.dep_scheduled_local,
                    "departure_expected_local": flight.dep_actual_local,
                    "arrival_local": flight.arr_scheduled_local,
                    "gate": flight.dep_gate,
                    "terminal": flight.dep_terminal,
                    "last_polled_at": flight.last_polled_at.isoformat() + "Z"
                    if flight.last_polled_at
                    else None,
                    "next_poll_at": flight.next_poll_at.isoformat() + "Z"
                    if flight.next_poll_at
                    else None,
                    "last_error": flight.last_error,
                    "recipients": all_recipients(flight.notify_emails),
                }
                for flight in flights
            ]
        )
