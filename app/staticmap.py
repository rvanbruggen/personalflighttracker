"""Render a static map image for emails.

Email can't run the Leaflet map, and OpenStreetMap has no official static-map
API, so we stitch standard OSM tiles together ourselves and draw the planned
route (great circle), flown trail, airports and aircraft on top.

Good-citizen rules from the OSM tile policy: an identifying User-Agent, local
caching (tiles are kept for MAP_TILE_CACHE_DAYS), no bulk fetching, and
visible attribution on the image.
"""

from __future__ import annotations

import io
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

from .config import settings
from .graphics import BRAND_BLUE, plane_polygon

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 560, 300
PADDING = 38
TILE = 256
SUPERSAMPLE = 3
FETCH_DEADLINE_SECONDS = 12.0
PLACEHOLDER = (229, 233, 239)


@dataclass
class MapData:
    dep: Optional[tuple[float, float, str]] = None  # lat, lon, code
    arr: Optional[tuple[float, float, str]] = None
    trail: list[tuple[float, float]] = field(default_factory=list)
    latest: Optional[tuple[float, float, Optional[float]]] = None  # lat, lon, track

    @property
    def has_anything(self) -> bool:
        return bool(self.dep or self.arr or self.trail or self.latest)


# ------------------------------------------------------------------ geometry


def great_circle(lat1: float, lon1: float, lat2: float, lon2: float, steps: int = 64):
    """Points along the shortest path over the globe — what a flight roughly flies."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(
        math.sqrt(math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2)
    )
    if d < 1e-9:
        return [(lat1, lon1)]
    points = []
    for i in range(steps + 1):
        f = i / steps
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
        y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
        z = a * math.sin(p1) + b * math.sin(p2)
        points.append((math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))))
    return points


def _near(lon: float, target: float) -> float:
    """Shift a longitude by whole turns so it sits within 180° of `target`.
    Keeps Pacific crossings continuous instead of wrapping across the map."""
    while lon - target > 180:
        lon -= 360
    while lon - target < -180:
        lon += 360
    return lon


def _unwrap(points: list[tuple[float, float]], start_near: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    previous = start_near
    for lat, lon in points:
        lon = _near(lon, previous)
        out.append((lat, lon))
        previous = lon
    return out


def _world_px(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, 85.0511), -85.0511)
    n = TILE * (2**zoom)
    x = (lon + 180.0) / 360.0 * n
    phi = math.radians(lat)
    y = (1.0 - math.log(math.tan(phi) + 1.0 / math.cos(phi)) / math.pi) / 2.0 * n
    return x, y


# --------------------------------------------------------------------- tiles


def _cache_path(z: int, x: int, y: int) -> str:
    return os.path.join(settings.map_tile_cache_dir, str(z), str(x), f"{y}.png")


def fetch_tile(z: int, x: int, y: int, client: Optional[httpx.Client] = None) -> Optional[Image.Image]:
    """One tile, from the disk cache when fresh. Returns None on failure.
    Module-level so tests can replace it and never touch the network."""
    path = _cache_path(z, x, y)
    max_age = settings.map_tile_cache_days * 86400
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age:
            return Image.open(path).convert("RGB")
    except OSError:
        pass

    url = settings.map_tile_url.format(z=z, x=x, y=y)
    try:
        own = client is None
        client = client or httpx.Client(timeout=6.0, headers={"User-Agent": settings.http_user_agent})
        try:
            response = client.get(url)
        finally:
            if own:
                client.close()
        if response.status_code != 200:
            log.warning("Map tile %s returned HTTP %s", url, response.status_code)
            return None
        image = Image.open(io.BytesIO(response.content)).convert("RGB")
    except (httpx.HTTPError, OSError) as exc:
        log.warning("Map tile %s failed: %s", url, exc)
        return None

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(response.content)
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("Could not cache tile %s: %s", path, exc)
    return image


# ------------------------------------------------------------------ drawing


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _dashed(draw: ImageDraw.ImageDraw, pts, fill, width, dash, gap):
    """Pillow has no dashed lines; walk the polyline and emit segments."""
    carry, drawing = 0.0, True
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        seg = math.hypot(x2 - x1, y2 - y1)
        pos = 0.0
        while pos < seg:
            span = (dash if drawing else gap) - carry
            step = min(span, seg - pos)
            if drawing:
                a, b = pos / seg, (pos + step) / seg
                draw.line([(x1 + (x2 - x1) * a, y1 + (y2 - y1) * a),
                           (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b)], fill=fill, width=width)
            pos += step
            carry += step
            if carry >= (dash if drawing else gap) - 1e-9:
                carry, drawing = 0.0, not drawing


def render_map(data: MapData) -> Optional[bytes]:
    """PNG bytes, or None when there is nothing to draw. Never raises: a map
    problem must not stop an alert from going out."""
    if not settings.email_map_enabled or not data.has_anything:
        return None
    try:
        return _render(data)
    except Exception:  # noqa: BLE001
        log.exception("Map rendering failed; sending the email without a map")
        return None


def _render(data: MapData) -> bytes:
    ref = data.dep[1] if data.dep else (data.trail[0][1] if data.trail else
                                         data.latest[1] if data.latest else data.arr[1])

    route: list[tuple[float, float]] = []
    dep = arr = None
    if data.dep:
        dep = (data.dep[0], _near(data.dep[1], ref), data.dep[2])
    if data.dep and data.arr:
        route = _unwrap(great_circle(data.dep[0], data.dep[1], data.arr[0], data.arr[1]), ref)
        arr = (data.arr[0], route[-1][1], data.arr[2])
    elif data.arr:
        arr = (data.arr[0], _near(data.arr[1], ref), data.arr[2])
    trail = _unwrap(data.trail, ref) if data.trail else []
    latest = None
    if data.latest:
        anchor = trail[-1][1] if trail else ref
        latest = (data.latest[0], _near(data.latest[1], anchor), data.latest[2])

    points = list(route) + list(trail)
    points += [(p[0], p[1]) for p in (dep, arr, latest) if p]

    # Deepest zoom that still fits everything, capped so a lone point isn't
    # shown at street level.
    max_zoom = 8 if len({(round(a, 2), round(b, 2)) for a, b in points}) > 1 else 6
    zoom = 1
    for z in range(max_zoom, 0, -1):
        xs, ys = zip(*(_world_px(lat, lon, z) for lat, lon in points))
        if max(xs) - min(xs) <= WIDTH - 2 * PADDING and max(ys) - min(ys) <= HEIGHT - 2 * PADDING:
            zoom = z
            break
    xs, ys = zip(*(_world_px(lat, lon, zoom) for lat, lon in points))
    origin_x = (max(xs) + min(xs)) / 2 - WIDTH / 2
    origin_y = (max(ys) + min(ys)) / 2 - HEIGHT / 2

    # --- base: stitched tiles ---
    base = Image.new("RGB", (WIDTH, HEIGHT), PLACEHOLDER)
    n = 2**zoom
    stats = {"ok": 0, "failed": 0}
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    with httpx.Client(timeout=6.0, headers={"User-Agent": settings.http_user_agent}) as client:
        for ty in range(math.floor(origin_y / TILE), math.floor((origin_y + HEIGHT) / TILE) + 1):
            for tx in range(math.floor(origin_x / TILE), math.floor((origin_x + WIDTH) / TILE) + 1):
                if not 0 <= ty < n:
                    continue
                tile = fetch_tile(zoom, tx % n, ty, client) if time.monotonic() < deadline else None
                if tile is None:
                    stats["failed"] += 1
                    continue
                stats["ok"] += 1
                base.paste(tile, (int(round(tx * TILE - origin_x)), int(round(ty * TILE - origin_y))))
    log.info("Map rendered: zoom %s, %s tiles, %s unavailable", zoom, stats["ok"], stats["failed"])

    def px(lat: float, lon: float, k: int = 1) -> tuple[float, float]:
        x, y = _world_px(lat, lon, zoom)
        return (x - origin_x) * k, (y - origin_y) * k

    # --- overlay, supersampled for smooth lines ---
    k = SUPERSAMPLE
    overlay = Image.new("RGBA", (WIDTH * k, HEIGHT * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if len(route) > 1:
        _dashed(draw, [px(a, b, k) for a, b in route], (70, 80, 95, 230), 2 * k, 9 * k, 7 * k)
    if len(trail) > 1:
        pts = [px(a, b, k) for a, b in trail]
        draw.line(pts, fill=(255, 255, 255, 235), width=8 * k, joint="curve")
        draw.line(pts, fill=BRAND_BLUE + (255,), width=4 * k, joint="curve")
    for airport in (dep, arr):
        if airport:
            x, y = px(airport[0], airport[1], k)
            r = 6 * k
            draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 255),
                         outline=BRAND_BLUE + (255,), width=3 * k)
    if latest:
        x, y = px(latest[0], latest[1], k)
        heading = latest[2] if latest[2] is not None else 90
        draw.polygon(plane_polygon(x, y, 15 * k, heading), fill=(255, 255, 255, 255))
        draw.polygon(plane_polygon(x, y, 12 * k, heading), fill=BRAND_BLUE + (255,))
    overlay = overlay.resize((WIDTH, HEIGHT), Image.LANCZOS)
    canvas = base.convert("RGBA")
    canvas.alpha_composite(overlay)

    # --- labels at native resolution ---
    draw = ImageDraw.Draw(canvas)
    label_font = _font(13)
    for airport in (dep, arr):
        if airport and airport[2]:
            x, y = px(airport[0], airport[1])
            text = airport[2]
            left, top, right, bottom = draw.textbbox((0, 0), text, font=label_font)
            w, h = right - left + 12, bottom - top + 8
            bx = min(max(x + 9, 4), WIDTH - w - 4)
            by = min(max(y - h - 7, 4), HEIGHT - h - 4)
            draw.rounded_rectangle([bx, by, bx + w, by + h], radius=5,
                                   fill=(255, 255, 255, 240), outline=BRAND_BLUE + (255,), width=1)
            draw.text((bx + 6 - left, by + 4 - top), text, font=label_font, fill=(15, 39, 71, 255))

    credit = "© OpenStreetMap contributors"
    small = _font(10)
    left, top, right, bottom = draw.textbbox((0, 0), credit, font=small)
    w, h = right - left + 8, bottom - top + 6
    draw.rectangle([WIDTH - w, HEIGHT - h, WIDTH, HEIGHT], fill=(255, 255, 255, 215))
    draw.text((WIDTH - w + 4 - left, HEIGHT - h + 3 - top), credit, font=small, fill=(70, 80, 95, 255))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
