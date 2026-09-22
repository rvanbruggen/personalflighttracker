"""Shared drawing primitives: the plane silhouette used by the logo and by
the aircraft marker on emailed maps, so both look like the same brand."""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

BRAND_BLUE = (31, 95, 191)
BRAND_BLUE_DARK = (15, 55, 125)
WHITE = (255, 255, 255)

# Top view of an airliner pointing straight up, in unit coordinates
# (nose at y=-1). Right half only; mirrored to make it symmetric.
_RIGHT_HALF = [
    (0.00, -1.00),
    (0.035, -0.985),
    (0.065, -0.95),
    (0.09, -0.89),
    (0.10, -0.80),
    (0.10, -0.30),
    (0.92, 0.12),
    (0.92, 0.26),
    (0.10, 0.06),
    (0.09, 0.60),
    (0.36, 0.80),
    (0.36, 0.92),
    (0.05, 0.84),
    (0.00, 0.90),
]


def plane_polygon(cx: float, cy: float, size: float, heading_deg: float) -> list[tuple[float, float]]:
    """Polygon for a plane centred at (cx, cy), `size` = half-length,
    rotated so it points along `heading_deg` (0 = north, 90 = east)."""
    half = _RIGHT_HALF
    outline = half + [(-x, y) for x, y in reversed(half[1:-1])]
    theta = math.radians(heading_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return [
        (cx + size * (x * cos_t - y * sin_t), cy + size * (x * sin_t + y * cos_t))
        for x, y in outline
    ]


def draw_logo(size: int = 256) -> Image.Image:
    """Rounded-square app mark: blue gradient, dashed flight path, white plane."""
    scale = 4  # supersample for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # Vertical gradient, masked to a rounded square.
    gradient = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(gradient)
    for y in range(s):
        t = y / (s - 1)
        colour = tuple(int(a + (b - a) * t) for a, b in zip((52, 132, 235), BRAND_BLUE_DARK))
        gd.line([(0, y), (s, y)], fill=colour + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255)
    img.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(img)

    # Dashed trail curving in from the bottom-left to just behind the tail.
    p0, p1, p2 = (0.13, 0.90), (0.20, 0.66), (0.40, 0.62)
    points = []
    for i in range(121):
        t = i / 120
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t**2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t**2 * p2[1]
        points.append((x * s, y * s))
    dash, gap = 9, 7
    for start in range(0, len(points) - 1, dash + gap):
        segment = points[start : start + dash]
        if len(segment) > 1:
            draw.line(segment, fill=(255, 255, 255, 165), width=int(s * 0.032), joint="curve")

    # Plane heading north-east.
    draw.polygon(plane_polygon(s * 0.61, s * 0.40, s * 0.31, 45), fill=WHITE + (255,))

    return img.resize((size, size), Image.LANCZOS)
