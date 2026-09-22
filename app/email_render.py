"""HTML versions of alert emails: logo, headline, bullets, map, the source of
the information, and when the reader will hear next.

The plain-text body every email already has is kept as the text/plain part;
this module adds a matching text appendix (next update + sources) so readers
of plain text get the same information.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .cadence import NextStep, describe_interval, interval_for
from .config import settings
from .diffing import delay_minutes
from .staticmap import MapData, render_map

_HERE = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(_HERE, "static", "email", "logo.png")
EXTRA_FOOTER_MARKER = "<!--EXTRA_RECIPIENT_FOOTER-->"

_env = Environment(
    loader=FileSystemLoader(os.path.join(_HERE, "templates", "email")),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

PALETTES = {  # (accent, tint)
    "info": ("#1F5FBF", "#E3EDFF"),
    "delay": ("#B45309", "#FEF3C7"),
    "bad": ("#B91C1C", "#FEE2E2"),
    "moving": ("#1D4ED8", "#DBEAFE"),
    "good": ("#047857", "#D1FAE5"),
    "gate": ("#6D28D9", "#EDE9FE"),
    "neutral": ("#374151", "#F3F4F6"),
}

_STATUS_PALETTE = {
    "canceled": "bad", "cancelled": "bad", "canceleduncertain": "bad", "diverted": "bad",
    "delayed": "delay",
    "departed": "moving", "enroute": "moving", "en route": "moving", "approaching": "moving",
    "boarding": "moving", "gateclosed": "moving",
    "arrived": "good", "landed": "good",
}


# ------------------------------------------------------------------ the data


@dataclass
class FlightView:
    """Plain copy of everything an email needs, taken while the DB session is
    open so rendering can safely happen in a worker thread."""

    flight_number: str = ""
    flight_date: str = ""
    label: str = ""
    airline: str = ""
    status: str = ""
    tracking_state: str = "active"
    callsign: str = ""
    callsign_derived: bool = False
    aircraft_model: str = ""
    aircraft_reg: str = ""
    dep_iata: str = ""
    dep_name: str = ""
    dep_terminal: str = ""
    dep_gate: str = ""
    dep_scheduled_local: str = ""
    dep_actual_local: str = ""
    dep_scheduled_utc: Optional[datetime] = None
    dep_actual_utc: Optional[datetime] = None
    dep_lat: Optional[float] = None
    dep_lon: Optional[float] = None
    arr_iata: str = ""
    arr_name: str = ""
    arr_terminal: str = ""
    arr_gate: str = ""
    arr_baggage_belt: str = ""
    arr_scheduled_local: str = ""
    arr_actual_local: str = ""
    arr_scheduled_utc: Optional[datetime] = None
    arr_actual_utc: Optional[datetime] = None
    arr_lat: Optional[float] = None
    arr_lon: Optional[float] = None
    trail: list[tuple[float, float]] = field(default_factory=list)
    latest: Optional[dict] = None
    last_position_at: Optional[datetime] = None
    position_source: str = ""
    last_polled_at: Optional[datetime] = None

    @classmethod
    def from_flight(cls, flight, resolved_callsign=None) -> "FlightView":
        view = cls()
        for name, spec in cls.__dataclass_fields__.items():
            if name in {"trail", "latest", "callsign", "callsign_derived"}:
                continue
            value = getattr(flight, name, None)
            # Only None means "unknown" — 0.0 is a real coordinate.
            if value is not None:
                setattr(view, name, value)
        positions = list(getattr(flight, "positions", []) or [])
        view.trail = [(p.lat, p.lon) for p in positions]
        if positions:
            p = positions[-1]
            view.latest = {"lat": p.lat, "lon": p.lon, "track": p.track_deg,
                           "altitude_ft": p.altitude_ft, "ground_speed_kt": p.ground_speed_kt}
        if resolved_callsign:
            view.callsign = resolved_callsign.value
            view.callsign_derived = resolved_callsign.derived
        else:
            view.callsign = flight.callsign or ""
        return view

    @property
    def is_airborne(self) -> bool:
        return self.status.strip().lower() in {"departed", "enroute", "en route", "approaching", "diverted"}


@dataclass
class RenderedEmail:
    html: str
    text_appendix: str
    images: dict[str, bytes]  # content-id -> PNG bytes
    preheader: str = ""


# --------------------------------------------------------------- formatting


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def tz_label() -> str:
    name = settings.timezone.split("/")[-1].replace("_", " ")
    return "UTC" if name.upper() == "UTC" else f"{name} time"


def fmt_when(value: Optional[datetime], now: Optional[datetime] = None) -> str:
    """Naive-UTC datetime -> 'today at 15:15', 'tomorrow at 09:15',
    'Mon 28 Sep at 09:15' in the configured timezone."""
    if value is None:
        return ""
    tz = _tz()
    aware = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(tz)
    now_local = (now or datetime.now(timezone.utc))
    now_local = (now_local if now_local.tzinfo else now_local.replace(tzinfo=timezone.utc)).astimezone(tz)
    days = (aware.date() - now_local.date()).days
    clock = aware.strftime("%H:%M")
    if days == 0:
        return f"today at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    if days == -1:
        return f"yesterday at {clock}"
    return f"{aware.strftime('%a')} {aware.day} {aware.strftime('%b')} at {clock}"


_LOCAL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})")
_UTC_Z_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})Z$")


def fmt_local(value: str, with_date: bool = True) -> str:
    """'2026-09-22 09:15' (airport-local) -> 'Tue 22 Sep, 09:15'."""
    match = _LOCAL_RE.match(value or "")
    if not match:
        return value or ""
    dt = datetime(*map(int, match.groups()))
    clock = dt.strftime("%H:%M")
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}, {clock}" if with_date else clock


def fmt_change_value(value: str) -> str:
    """Change values for times arrive as '2026-09-22 19:53Z'."""
    match = _UTC_Z_RE.match(value or "")
    if not match:
        return value
    dt = datetime(*map(int, match.groups()))
    return f"{dt.day} {dt.strftime('%b')}, {dt.strftime('%H:%M')} UTC"


_STATUS_WORDS = {
    "enroute": "En route", "en route": "En route", "gateclosed": "Gate closed",
    "checkin": "Check-in open", "canceleduncertain": "Possibly cancelled",
    "canceled": "Cancelled", "cancelled": "Cancelled",
}


def pretty_status(status: str) -> str:
    """AeroDataBox enums ('EnRoute', 'GateClosed') -> words people use."""
    key = (status or "").strip().lower()
    return _STATUS_WORDS.get(key, status or "")


def fmt_minutes(total: int) -> str:
    hours, minutes = divmod(abs(total), 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"


def _airport_offset(local: str, utc: Optional[datetime]) -> Optional[timedelta]:
    """The airport's UTC offset, recovered from a local/UTC pair we already have."""
    match = _LOCAL_RE.match(local or "")
    if not match or utc is None:
        return None
    raw = datetime(*map(int, match.groups())) - utc.replace(tzinfo=None)
    # Real UTC offsets are whole quarter-hours; rounding removes seconds noise.
    quarter = 15 * 60
    return timedelta(seconds=round(raw.total_seconds() / quarter) * quarter)


def fmt_change(label: str, value: str, view: "FlightView") -> str:
    """Render one side of a change. Times become airport-local when the
    offset is known, so they match the details table; status gets words."""
    if label == "Status":
        return pretty_status(value)
    match = _UTC_Z_RE.match(value or "")
    if not match:
        return value
    utc = datetime(*map(int, match.groups()))
    lowered = label.lower()
    if "departure" in lowered:
        offset = (_airport_offset(view.dep_scheduled_local, view.dep_scheduled_utc)
                  or _airport_offset(view.dep_actual_local, view.dep_actual_utc))
    elif "arrival" in lowered:
        offset = (_airport_offset(view.arr_scheduled_local, view.arr_scheduled_utc)
                  or _airport_offset(view.arr_actual_local, view.arr_actual_utc))
    else:
        offset = None
    if offset is None:
        return fmt_change_value(value)
    return fmt_local((utc + offset).strftime("%Y-%m-%d %H:%M")) + " (local)"


def _same_day(a: str, b: str) -> bool:
    return bool(a and b and a[:10] == b[:10])


# ------------------------------------------------------------------ content


def headline(kind: str, view: FlightView, changes: list[tuple[str, str, str]]):
    """(banner label, headline sentence, palette key)."""
    fn = view.flight_number
    if kind == "started":
        return "Tracking started", f"We're now tracking {fn}", "info"
    if kind == "welcome":
        return "You've been added", f"You'll get updates for {fn}", "info"
    if kind == "test":
        return "Test", "Your flight alerts are working", "info"

    status = view.status.strip().lower()
    labels = {label: new for label, _, new in changes}
    delay = delay_minutes(view)

    if status in {"canceled", "cancelled", "canceleduncertain"}:
        return "Cancelled", f"{fn} has been cancelled", "bad"
    if status == "diverted":
        return "Diverted", f"{fn} has been diverted", "bad"
    late = f", {fmt_minutes(delay)} late" if delay is not None and delay >= 15 else ""
    if "Status" in labels and status in {"arrived", "landed"}:
        arr_delay = None
        if view.arr_actual_utc and view.arr_scheduled_utc:
            arr_delay = int(round((view.arr_actual_utc - view.arr_scheduled_utc).total_seconds() / 60))
        late_arr = f", {fmt_minutes(arr_delay)} late" if arr_delay is not None and arr_delay >= 15 else ""
        return "Landed", f"{fn} has landed{' in ' + view.arr_name if view.arr_name else ''}{late_arr}", "good"
    if "Status" in labels and status in {"departed", "enroute", "en route"}:
        return "Departed", f"{fn} has departed{late}", "moving"
    if delay is not None and delay >= 15:
        return "Delayed", f"{fn} is running {fmt_minutes(delay)} late", "delay"
    if "Departure gate" in labels:
        return "Gate change", f"{fn} now departs from gate {labels['Departure gate']}", "gate"
    if "Departure terminal" in labels:
        return "Terminal change", f"{fn} now departs from terminal {labels['Departure terminal']}", "gate"
    if "Status" in labels:
        pretty = {"boarding": "is boarding", "gateclosed": "has closed its gate",
                  "approaching": f"is approaching {view.arr_iata or 'its destination'}",
                  "checkin": "is open for check-in"}.get(status)
        if pretty:
            return pretty_status(view.status), f"{fn} {pretty}", "moving"
        return "Status update", f"{fn} is now: {pretty_status(view.status)}", "info"
    if any("departure" in label.lower() or "arrival" in label.lower() for label in labels):
        return "New times", f"{fn} has new times", "info"
    return "Update", f"Update for {fn}", "info"


def next_update(step: NextStep, view: FlightView, now: Optional[datetime] = None):
    """(title, [paragraphs]) explaining when the reader will hear next."""
    now = now or datetime.now(timezone.utc)
    if step.kind == "completed":
        what = "has been cancelled" if step.final_status.lower().startswith("cancel") else "has landed"
        return "This is the final update", [
            f"The flight {what}, so tracking has ended. You won't receive further emails about it."
        ]
    if step.kind == "abandoned":
        return "Tracking has stopped", [
            f"The data provider never reported this flight as landed within "
            f"{settings.poll_abandon_after_hours} hours of its scheduled arrival, so tracking has stopped."
        ]

    when = fmt_when(step.at, now)
    every = describe_interval(interval_for(step.phase))
    tail = f" Next check: <strong>{when}</strong> ({tz_label()})." if when else ""
    paragraphs = []
    if step.phase == "airborne":
        paragraphs.append(f"The flight is in the air, so we check {every} until it lands.{tail}")
    elif step.phase == "close":
        paragraphs.append(f"Departure is close, so we check {every}.{tail}")
    elif step.phase == "day":
        dep = view.dep_scheduled_utc
        switch = fmt_when(dep - timedelta(hours=2), now) if dep else ""
        closer = describe_interval(interval_for("close"))
        paragraphs.append(
            f"Departure is within 24 hours, so we check {every}.{tail}"
            + (f" From {switch} we'll check {closer}." if switch else "")
        )
    elif step.phase == "far":
        dep = view.dep_scheduled_utc
        switch_at = dep - timedelta(hours=24) if dep else None
        hourly = describe_interval(interval_for("day"))
        close = describe_interval(interval_for("close"))
        intro = (f"Departure is more than 24 hours away, so for now we check {every} to catch "
                 f"early cancellations or schedule changes.")
        if switch_at and step.at and abs((step.at - switch_at.replace(tzinfo=None)).total_seconds()) < 300:
            # The next check *is* the 24-hour mark: say it once.
            paragraphs.append(
                f"{intro} Next check: <strong>{when}</strong> ({tz_label()}), 24 hours before "
                f"departure. From then on we check {hourly}, and {close} in the last 2 hours."
            )
        else:
            switch = fmt_when(switch_at, now) if switch_at else ""
            paragraphs.append(
                f"{intro}{tail}"
                + (f" From {switch} we'll check {hourly}, and {close} in the last 2 hours."
                   if switch else "")
            )
    else:
        paragraphs.append(f"The data provider doesn't have a departure time for this flight yet, so we check {every}.{tail}")

    paragraphs.append(
        "You'll only get an email when something changes: the status (including departure "
        "and landing), the departure gate or terminal, or a time moving by more than a couple "
        "of minutes. <strong>No email means no change.</strong>"
    )
    return "When you'll hear from us next", paragraphs


def sources(view: FlightView, has_map: bool, now: Optional[datetime] = None) -> list[str]:
    items = []
    checked = fmt_when(view.last_polled_at, now)
    items.append(
        "Flight status and times: <strong>AeroDataBox</strong> (via RapidAPI)"
        + (f", last checked {checked} ({tz_label()})" if checked else "") + "."
    )
    if view.latest:
        fix = fmt_when(view.last_position_at, now)
        items.append(
            "Live position: <strong>adsb.lol</strong> community ADS-B network"
            + (f", last fix {fix}" if fix else "") + "."
        )
    if has_map:
        items.append('Map: © <a href="https://www.openstreetmap.org/copyright" '
                     'style="color:#1F5FBF;">OpenStreetMap</a> contributors.')
    items.append("Departure and arrival times are local to each airport.")
    return items


def map_caption(view: FlightView, now: Optional[datetime] = None) -> str:
    bits = []
    if view.latest:
        facts = [f"last seen {fmt_when(view.last_position_at, now)}" if view.last_position_at else "last position"]
        if view.latest.get("altitude_ft"):
            facts.append(f"{view.latest['altitude_ft']:,} ft")
        if view.latest.get("ground_speed_kt"):
            facts.append(f"{view.latest['ground_speed_kt']} kt")
        bits.append(" · ".join(facts).capitalize() + ". Solid line: flown so far. Dashed: planned route.")
    elif view.is_airborne:
        bits.append("In the air, but out of range of ADS-B receivers right now (common over "
                    "oceans). Dashed line: planned route.")
    else:
        bits.append("Planned route. The live position is added once the flight is in the air.")
    if view.callsign_derived and view.callsign and view.latest:
        bits.append(f"Position matched on callsign {view.callsign}, derived from the flight number "
                    "(unconfirmed).")
    return " ".join(bits)


def detail_rows(view: FlightView) -> list[tuple[str, str]]:
    """(label, html) rows for the flight-details table."""
    from markupsafe import escape

    rows: list[tuple[str, str]] = []
    accent, tint = PALETTES[_STATUS_PALETTE.get(view.status.strip().lower(), "neutral")]
    if view.status:
        rows.append(("Status", f'<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
                               f'background:{tint};color:{accent};font-weight:600;font-size:13px;">'
                               f'{escape(pretty_status(view.status))}</span>'))

    def when(scheduled: str, actual: str) -> str:
        if not scheduled:
            return escape(fmt_local(actual)) if actual else ""
        text = escape(fmt_local(scheduled))
        if actual and actual != scheduled:
            new = fmt_local(actual, with_date=not _same_day(scheduled, actual))
            text = (f'<span style="text-decoration:line-through;color:#9CA3AF;">{text}</span> '
                    f'&rarr; <strong>{escape(new)}</strong>')
        return text

    if view.dep_scheduled_local or view.dep_actual_local:
        rows.append(("Departure (local)", when(view.dep_scheduled_local, view.dep_actual_local)))
    if view.dep_terminal or view.dep_gate:
        rows.append(("Terminal / gate", f"{escape(view.dep_terminal or '—')} / "
                                        f"<strong>{escape(view.dep_gate or '—')}</strong>"))
    if view.arr_scheduled_local or view.arr_actual_local:
        rows.append(("Arrival (local)", when(view.arr_scheduled_local, view.arr_actual_local)))
    if view.arr_baggage_belt:
        rows.append(("Baggage belt", f"<strong>{escape(view.arr_baggage_belt)}</strong>"))
    if view.aircraft_model or view.aircraft_reg:
        rows.append(("Aircraft", escape(f"{view.aircraft_model} {view.aircraft_reg}".strip())))
    return rows


def map_data(view: FlightView) -> MapData:
    data = MapData(trail=list(view.trail))
    if view.dep_lat is not None and view.dep_lon is not None:
        data.dep = (view.dep_lat, view.dep_lon, view.dep_iata)
    if view.arr_lat is not None and view.arr_lon is not None:
        data.arr = (view.arr_lat, view.arr_lon, view.arr_iata)
    if view.latest:
        data.latest = (view.latest["lat"], view.latest["lon"], view.latest.get("track"))
    return data


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("&rarr;", "→")


# ------------------------------------------------------------------- render


def build_email(
    kind: str,
    view: Optional[FlightView] = None,
    changes: Optional[list[tuple[str, str, str]]] = None,
    step: Optional[NextStep] = None,
    now: Optional[datetime] = None,
    include_map: bool = True,
) -> RenderedEmail:
    """kind: 'alert' | 'started' | 'welcome' | 'test'."""
    now = now or datetime.now(timezone.utc)
    changes = changes or []
    images: dict[str, bytes] = {}
    try:
        with open(LOGO_PATH, "rb") as fh:
            images["logo"] = fh.read()
    except OSError:
        pass

    view = view or FlightView()
    label, title, palette = headline(kind, view, changes)
    accent, tint = PALETTES[palette]

    map_png = render_map(map_data(view)) if (include_map and kind != "test") else None
    if map_png:
        images["map"] = map_png

    nxt_title, nxt_paragraphs = next_update(step, view, now) if step else ("", [])
    source_items = sources(view, bool(map_png), now) if kind != "test" else []

    route = ""
    if view.dep_iata or view.arr_iata:
        dep = f"{view.dep_name} ({view.dep_iata})" if view.dep_name and view.dep_iata else (view.dep_iata or view.dep_name)
        arr = f"{view.arr_name} ({view.arr_iata})" if view.arr_name and view.arr_iata else (view.arr_iata or view.arr_name)
        route = f"{dep or '?'} → {arr or '?'}"
    meta = " · ".join(x for x in [view.flight_number, view.airline,
                                  fmt_local(f"{view.flight_date} 00:00").split(",")[0] if view.flight_date else ""] if x)

    preheader = title + (f" — {_strip_tags(nxt_paragraphs[0])}" if nxt_paragraphs else "")

    html = _env.get_template("message.html").render(
        kind=kind,
        app_name=settings.app_name,
        has_logo="logo" in images,
        banner_label=label.upper(),
        headline=title,
        accent=accent,
        tint=tint,
        route=route,
        meta=meta,
        changes=[(lbl, fmt_change(lbl, old, view), fmt_change(lbl, new, view)) for lbl, old, new in changes],
        rows=detail_rows(view) if kind != "test" else [],
        has_map=bool(map_png),
        map_caption=map_caption(view, now) if map_png else "",
        next_title=nxt_title,
        next_paragraphs=nxt_paragraphs,
        sources=source_items,
        note=view.label,
        extra_footer_marker=EXTRA_FOOTER_MARKER,
        preheader=preheader[:180],
    )

    text = []
    if nxt_title:
        text += ["", nxt_title + ":"] + [f"  {_strip_tags(p)}" for p in nxt_paragraphs]
    if source_items:
        text += ["", "Sources:"] + [f"  • {_strip_tags(s)}" for s in source_items]
    return RenderedEmail(html=html, text_appendix="\n".join(text), images=images, preheader=preheader)


EXTRA_FOOTER_HTML = (
    '<p style="margin:12px 0 0;font-size:12px;line-height:18px;color:#6B7280;">'
    "You are receiving this because you were added as a recipient for this flight on "
    "{app}. Ask the person tracking it to remove you.</p>"
)
