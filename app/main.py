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
from .db import Flight, FlightEvent, SessionLocal, init_db, utcnow
from .notify import send_test_alert
from .providers.aerodatabox import provider as status_provider
from .tracker import (
    flight_callsign,
    poll_flight,
    poll_position,
    position_tick,
    quota_status,
    tick,
)

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
            next_poll_at=utcnow().replace(tzinfo=None),
        )
        session.add(flight)
        session.commit()
        flight_id = flight.id

    # First poll immediately, so the user sees a result right away.
    outcome = await poll_flight(flight_id)
    log.info("Registered %s on %s: %s", number, iso_date, outcome)
    return _flash(request, f"/flights/{flight_id}", f"Tracking {number} — {outcome}.", "ok")


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
            "settings": settings,
            "quota": quota_status(),
            "message": msg,
            "level": level,
            "now": utcnow(),
        },
    )


@app.post("/flights/{flight_id}/refresh")
async def refresh_flight(request: Request, flight_id: int):
    outcome = await poll_flight(flight_id, force=True)
    return _flash(request, f"/flights/{flight_id}", f"Refreshed — {outcome}.", "ok")


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
    if result.error:
        return _flash(request, "/", f"Test alert failed: {result.error}", "error")
    return _flash(
        request,
        "/",
        f"Test alert sent (inbox={result.inbox_sent}, IFTTT={result.ifttt_sent}).",
        "ok",
    )


# ------------------------------------------------------------------------- API


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "aerodatabox_configured": settings.aerodatabox_configured,
        "smtp_configured": settings.smtp_configured,
        "ifttt_enabled": settings.ifttt_enabled,
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
                }
                for flight in flights
            ]
        )
