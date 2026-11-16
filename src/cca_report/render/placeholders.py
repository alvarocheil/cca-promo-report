"""Placeholder screenshots (Pillow) for fixtures and for products without a capture yet."""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

# pixel sizes at 200 dpi, keeping the spec aspect ratios (cards 2.7x3.85in, listing
# 11.4x2.6in, banner 5.9x2.6in)
SIZES: dict[str, tuple[int, int]] = {"card": (540, 770), "listing": (2280, 520), "banner": (1180, 520)}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A system TrueType font with accents/¡ glyphs; Pillow's default lacks them."""
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def make(out_dir: Path, label: str, kind: Literal["card", "listing", "banner"]) -> str:
    """Return a placeholder image; the file name is a hash of (kind, label) so a cached
    image can never show another product's text."""
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(f"{kind}|{label}".encode()).hexdigest()[:12]
    path = out_dir / f"placeholder-{kind}-{digest}.png"
    if path.exists():
        return path.as_posix()
    w, h = SIZES[kind]
    img = Image.new("RGB", (w, h), (238, 240, 243))
    d = ImageDraw.Draw(img)
    d.rectangle([6, 6, w - 7, h - 7], outline=(170, 176, 186), width=4)
    # simple "device" silhouette so cards read as product shots
    if kind == "card":
        pw, ph = int(w * 0.42), int(h * 0.48)
        x0, y0 = (w - pw) // 2, int(h * 0.1)
        d.rounded_rectangle([x0, y0, x0 + pw, y0 + ph], radius=28, fill=(205, 210, 218))
    font = _font(34 if kind == "card" else 44)
    lines = textwrap.wrap(label, width=22 if kind == "card" else 60) or [""]
    y = int(h * 0.66) if kind == "card" else h // 2 - 30 * len(lines)
    for line in [*lines, "PLACEHOLDER"]:
        tw = d.textlength(line, font=font)
        d.text(((w - tw) / 2, y), line, fill=(70, 76, 86), font=font)
        y += 48
    img.save(path, optimize=True)
    return path.as_posix()
