"""Regenerate the logo PNGs from app/graphics.py.

    python scripts/make_logo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.graphics import draw_logo  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = {
    os.path.join(ROOT, "app", "static", "email", "logo.png"): 96,  # shown at 48px, sharp on retina
}

for path, size in TARGETS.items():
    os.makedirs(os.path.dirname(path), exist_ok=True)
    draw_logo(size).save(path, optimize=True)
    print(f"wrote {os.path.relpath(path, ROOT)} ({size}x{size}, {os.path.getsize(path)} bytes)")
