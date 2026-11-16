"""Jinja2 -> HTML -> PDF with Playwright/Chromium (13.333in x 7.5in, one slide per page).

Text never overflows (spec §3.4): every ``.fit`` box is shrunk in the browser down to its
``data-min`` size; detail slides that still overflow are reported back and the builder
splits them into more pages.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape
from PIL import Image
from playwright.async_api import async_playwright

from ..config import PROJECT_ROOT
from .document import DocumentBuilder, Slide

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"
LOGO = PROJECT_ROOT / "assets" / "logo_cheil.png"
MAX_ROUNDS = 4
IMAGE_MAX_SIDE = 1400  # px; slides show cards at ~2-4 in, this is >= 300 dpi for them
IMAGE_KEEP_BYTES = 150_000  # smaller images are embedded untouched


def compress_image(src: Path, cache_dir: Path) -> Path:
    """JPEG copy (white background, longest side <= IMAGE_MAX_SIDE) so the PDF stays mailable.
    The original screenshot is never modified (traceability)."""
    if src.stat().st_size <= IMAGE_KEEP_BYTES:
        return src
    st = src.stat()
    key = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:20]
    out = cache_dir / f"{key}.jpg"
    if out.exists():
        return out
    cache_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
        tmp = out.with_suffix(".tmp")
        im.save(tmp, "JPEG", quality=82, optimize=True, progressive=True)
    if tmp.stat().st_size >= st.st_size:  # recompression did not help
        tmp.unlink()
        return src
    tmp.replace(out)
    return out


FIT_JS = """
() => {
  const overflows = el => {
    if (el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1) return true;
    for (const c of el.querySelectorAll('td, th, li, .brand, .ptype')) {
      if (c.scrollWidth > c.clientWidth + 1) return true;
    }
    return false;
  };
  const failed = [];
  for (const el of document.querySelectorAll('.fit')) {
    const max = parseFloat(el.dataset.max || '11');
    const min = parseFloat(el.dataset.min || '8');
    let size = max;
    el.style.fontSize = size + 'pt';
    while (overflows(el) && size > min) {
      size = Math.max(min, size - 0.25);
      el.style.fontSize = size + 'pt';
    }
    // Second pass: a detail line (.line) should not wrap; shrink further while any line
    // wraps, but never below data-prefer (default 9pt) for this cosmetic pass.
    const prefer = Math.max(min, parseFloat(el.dataset.prefer || '9'));
    const wraps = () => Array.from(el.querySelectorAll('.line')).some(l => l.getClientRects().length > 1);
    while (wraps() && size > prefer) {
      size = Math.max(prefer, size - 0.25);
      el.style.fontSize = size + 'pt';
    }
    el.dataset.fitted = size;
    if (overflows(el)) {
      const slide = el.closest('.slide');
      failed.push({
        slide: slide.dataset.slideId,
        group: slide.dataset.groupId || '',
        box: el.className,
        splittable: el.classList.contains('table-box'),
      });
    }
  }
  return failed;
}
"""


def md_bold(text: str) -> Markup:
    """Escape, then turn **x** into <strong>x</strong> (insights cells / bullets)."""
    html = str(escape(text))
    return Markup(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html))


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md_bold"] = md_bold
    return env


@dataclass(slots=True)
class FontFace:
    family: str
    url: str
    weight: int
    format: str
    unicode_range: str = ""


def font_faces(fonts_dir: Path = FONTS_DIR) -> list[FontFace]:
    faces: list[FontFace] = []
    for weight, name in ((400, "Inter-Regular.woff2"), (700, "Inter-Bold.woff2")):
        f = fonts_dir / name
        if f.exists():
            faces.append(FontFace("Inter", f.as_uri(), weight, "woff2"))
    # Optional Samsung fonts dropped by the user (any of woff2/woff/ttf/otf).
    fmt = {".woff2": "woff2", ".woff": "woff", ".ttf": "truetype", ".otf": "opentype"}
    for f in sorted(fonts_dir.glob("*")):
        stem = f.stem.lower().replace(" ", "").replace("-", "").replace("_", "")
        if f.suffix.lower() not in fmt:
            continue
        family = "Samsung Sharp Sans" if "sharpsans" in stem else "SamsungOne" if "samsungone" in stem else None
        if family:
            weight = 700 if "bold" in stem else 400
            faces.append(FontFace(family, f.as_uri(), weight, fmt[f.suffix.lower()]))
    return faces


class RenderError(RuntimeError):
    pass


_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
_DRIVE = re.compile(r"^[a-z]:[\\/]", re.I)


def _to_uri(path: str | None, roots: tuple[Path, ...] = (PROJECT_ROOT,), cache_dir: Path | None = None) -> str | None:
    """Local image path -> file:// URI. Only files inside the project are allowed: data
    comes from external sites, so URLs, other schemes and ``..`` escapes are rejected."""
    if not path:
        return None
    if _SCHEME.match(path) and not _DRIVE.match(path):
        raise RenderError(f"image must be a local file, got {path!r}")
    p = Path(path)
    p = (p if p.is_absolute() else roots[0] / p).resolve()
    if not any(p.is_relative_to(r.resolve()) for r in roots):
        raise RenderError(f"image outside the project folder: {path!r}")
    if not p.is_file():
        raise RenderError(f"image not found: {path!r}")
    if cache_dir is not None:
        p = compress_image(p, cache_dir)
    return p.as_uri()


def render_html(
    slides: list[Slide],
    week_number: int,
    image_roots: tuple[Path, ...] = (PROJECT_ROOT,),
    cache_dir: Path | None = None,
) -> str:
    for s in slides:  # templates receive file:// URIs so Chromium can load local images
        s.listing_image = _to_uri(s.listing_image, image_roots, cache_dir)
        for c in s.columns:
            c.screenshot = _to_uri(c.screenshot, image_roots, cache_dir)
        for b in s.banners:
            b.screenshot = _to_uri(b.screenshot, image_roots, cache_dir)
    return (
        _env()
        .get_template("report.html")
        .render(
            slides=slides,
            week_number=week_number,
            fonts=font_faces(),
            css=(TEMPLATES / "base.css").read_text(encoding="utf-8"),
            logo=LOGO.as_uri() if LOGO.exists() else None,
        )
    )


@dataclass(slots=True)
class RenderOutcome:
    pdf: Path
    html: Path
    pages: int
    rounds: int
    overflow: list[dict]  # boxes still overflowing at minimum size (should be empty)


async def build_pdf(
    builder: DocumentBuilder, out_pdf: Path, week_number: int, *, allow_overflow: bool = False
) -> tuple[RenderOutcome, list[str]]:
    """Build slides, fit text, split overflowing blocks, then print the PDF."""
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    html_path = out_pdf.with_suffix(".html")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 720})
            # defence in depth: the report never needs the network
            await page.route(
                "**/*",
                lambda route: (
                    route.continue_() if route.request.url.startswith(("file:", "data:", "about:")) else route.abort()
                ),
            )
            failed: list[dict] = []
            rounds = 0
            for rounds in range(1, MAX_ROUNDS + 1):  # noqa: B007 - reported in RenderOutcome
                result = builder.build()
                roots = (PROJECT_ROOT, builder.assets_dir, out_pdf.parent)
                html_path.write_text(
                    render_html(result.slides, week_number, roots, out_pdf.parent / "render_cache"), encoding="utf-8"
                )
                await page.goto(html_path.as_uri(), wait_until="load")
                await page.evaluate("() => document.fonts.ready.then(() => true)")
                await page.emulate_media(media="print")
                failed = await page.evaluate(FIT_JS)
                progressed = False
                for gid in {f["group"] for f in failed if f["splittable"] and f["group"]}:
                    current = builder.per_page_override.get(gid) or max(
                        len(s.columns) for s in result.slides if s.group_id == gid
                    )
                    if current > 1:  # split: halve the products per page of that block
                        builder.per_page_override[gid] = math.ceil(current / 2)
                        progressed = True
                        log.info(
                            "block %s overflows: %d -> %d products per page",
                            gid,
                            current,
                            builder.per_page_override[gid],
                        )
                if not progressed:
                    break
            # keep the fitted DOM for traceability / debugging
            html_path.write_text(await page.content(), encoding="utf-8")
            await page.pdf(
                path=str(out_pdf),
                width="13.333in",
                height="7.5in",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                prefer_css_page_size=True,
                outline=True,
                tagged=True,
            )
        finally:
            await browser.close()
    if failed and not allow_overflow:
        where = ", ".join(f"{f['slide']} ({f['box']})" for f in failed)
        raise RenderError(f"text still overflows at the minimum size on: {where}")
    return RenderOutcome(out_pdf, html_path, len(result.slides), rounds, failed), result.flags
