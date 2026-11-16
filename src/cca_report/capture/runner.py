"""`capture` step: every network access of a weekly run happens here.

For each enabled retailer x brand it loads the configured URLs (once per URL even when
several brands share a page), detects the product cards, screenshots each card as an
element, saves the page HTML and, for Shopify stores, the collection ``products.json``.
Blocked / robots-forbidden pages are recorded and get a ``manual/`` folder instead.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError

from ..access import Prober
from ..config import PROJECT_ROOT, Project
from ..models import (
    AccessStatus,
    Brand,
    BrandConfig,
    CapturedGroup,
    CapturedPage,
    CaptureMode,
    ExtractMode,
    RawCard,
    RetailerConfig,
)
from .browser import dismiss_overlays, inspect, progressive_scroll
from .cards import detect_cards, wait_for_prices

log = logging.getLogger(__name__)
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
MAX_CARD_SHOTS = 60

# Fixed / sticky layers (cookie bars, chat bubbles, sticky headers) are hidden *only for the
# element screenshots*: no click, no change to the data read from the page.
HIDE_FIXED_JS = """
() => {
  let n = 0;
  for (const el of document.querySelectorAll('body *')) {
    const pos = getComputedStyle(el).position;
    const holdsCard = el.querySelector('[data-cca-card]') || el.closest('[data-cca-card]');
    if ((pos === 'fixed' || pos === 'sticky') && !holdsCard) {
      el.style.setProperty('visibility', 'hidden', 'important');
      n++;
    }
  }
  return n;
}
"""


def rel(path: Path) -> str:
    """Paths stored in run files are relative to the project root (portable runs)."""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def manual_dir_name(r: RetailerConfig, brand: Brand | None) -> str:
    tail = r.slug.split("_", 1)[1] if "_" in r.slug else r.slug
    return f"{r.country.value}_{tail}_{brand.value if brand else 'Branding'}"


def _hash(url: str) -> str:
    import hashlib

    return hashlib.sha1(url.encode()).hexdigest()[:8]


def failed_groups(r: RetailerConfig, exc: Exception) -> list[CapturedGroup]:
    """A retailer that crashed still replaces its previous capture (with an error status)."""
    now = datetime.now().astimezone()
    detail = f"capture crashed: {type(exc).__name__}: {str(exc)[:160]}"
    return [
        CapturedGroup(
            country=r.country,
            retailer_slug=r.slug,
            retailer=r.retailer_name,
            brand=b,
            mode=c.mode,
            capture=c.capture,
            row_format=r.row_format,
            pages=[CapturedPage(url=str(r.base_url), captured_at=now, status=AccessStatus.ERROR, detail=detail)],
        )
        for b, c in r.brands.items()
        if c.enabled
    ]


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60]


class Capturer:
    def __init__(self, project: Project, prober: Prober, run_dir: Path) -> None:
        self.project = project
        self.prober = prober
        self.run_dir = run_dir
        self.shots = run_dir / "screenshots"
        self.html = run_dir / "html"
        self.manual = run_dir / "manual"
        for d in (self.shots, self.html, self.manual):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ manual
    def _manual_group(self, r: RetailerConfig, brand: Brand, cfg: BrandConfig, why: str) -> CapturedGroup:
        folder = self.manual / manual_dir_name(r, brand)
        folder.mkdir(parents=True, exist_ok=True)
        readme = folder / "LEEME.txt"
        if not readme.exists():
            readme.write_text(
                f"{r.retailer_name} · {brand.value} ({r.country.value}) — {why}\n"
                "Coloca aquí las capturas de las tarjetas de producto (una imagen por producto, PNG/JPG).\n"
                "El nombre del archivo se usa como modelo (ej. 'Galaxy S26 Ultra.png').\n"
                "Precios: completa precios.csv (una fila por imagen; ';' o ',') para que la corrida\n"
                "automática del viernes los publique sin pasar por review.xlsx. Columnas:\n"
                "  archivo, precio_final, precio_antes, entrada, cuota_mensual, cuotas, regalo, etiqueta, promo\n"
                "Sin precios.csv, los precios se completan en review.xlsx.\n"
                "Para capture=listing, nombra la captura del grid 'listing.png'.\n",
                encoding="utf-8",
            )
        prices = folder / "precios.csv"
        if not prices.exists():
            from ..extract.pipeline import MANUAL_PRICES_HEADER

            prices.write_text(MANUAL_PRICES_HEADER + "\n", encoding="utf-8-sig")
        images = sorted(rel(p) for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXT)
        return CapturedGroup(
            country=r.country,
            retailer_slug=r.slug,
            retailer=r.retailer_name,
            brand=brand,
            mode=ExtractMode.MANUAL,
            capture=cfg.capture,
            row_format=r.row_format,
            manual_dir=rel(folder),
            manual_images=images,
        )

    def _campaign_group(self, r: RetailerConfig) -> CapturedGroup:
        folder = self.manual / manual_dir_name(r, None).replace("_Branding", "_Campaign")
        folder.mkdir(parents=True, exist_ok=True)
        readme = folder / "LEEME.txt"
        if not readme.exists():
            readme.write_text(
                f"{r.retailer_name} ({r.country.value}) — slide 'Campaign overview' (opcional).\n"
                "Coloca aquí UNA captura del banner de la campaña (PNG/JPG) y un archivo caption.txt:\n"
                "  línea 1 = nombre de la campaña (va en el encabezado derecho, ej. 'Ciberlunes')\n"
                "  línea 2 = resumen (caption), ej. 'Mercado Libre advertised up to 60% off ...'\n"
                "  línea 3 = URL de origen del banner (trazabilidad)\n",
                encoding="utf-8",
            )
        images = sorted(rel(p) for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXT)
        return CapturedGroup(
            country=r.country,
            retailer_slug=r.slug,
            retailer=r.retailer_name,
            brand=None,
            kind="overview",
            mode=ExtractMode.MANUAL,
            capture=CaptureMode.BANNER,
            row_format=r.row_format,
            manual_dir=rel(folder),
            manual_images=images,
        )

    # ------------------------------------------------------------------ retailer
    async def capture_retailer(self, r: RetailerConfig) -> list[CapturedGroup]:
        groups: list[CapturedGroup] = []
        live = {b: c for b, c in r.brands.items() if c.enabled}
        if r.campaign_overview:
            groups.append(self._campaign_group(r))
        if r.manual_only:
            return groups + [self._manual_group(r, b, c, "manual_only (robots.txt / policy)") for b, c in live.items()]
        auto = {b: c for b, c in live.items() if c.mode != ExtractMode.MANUAL}
        groups += [self._manual_group(r, b, c, "mode: manual") for b, c in live.items() if b not in auto]
        if not auto and not r.branding:
            return groups
        ctx = await self.prober.open_context(r, scale=2)
        pages: dict[str, CapturedPage] = {}
        try:
            for brand, cfg in auto.items():
                group = CapturedGroup(
                    country=r.country,
                    retailer_slug=r.slug,
                    retailer=r.retailer_name,
                    brand=brand,
                    mode=cfg.mode,
                    capture=cfg.capture,
                    row_format=r.row_format,
                )
                for url in map(str, cfg.urls):
                    if url not in pages:
                        pages[url] = await self._capture_page(r, ctx, url, cfg)
                    group.pages.append(pages[url])
                if not cfg.urls:
                    group.pages.append(
                        CapturedPage(
                            url=str(r.base_url),
                            captured_at=datetime.now().astimezone(),
                            status=AccessStatus.ERROR,
                            detail="no URL configured",
                        )
                    )
                if not any(p.status in (AccessStatus.OK, AccessStatus.JS_HEAVY) for p in group.pages):
                    why = "; ".join(f"{p.status.value}: {p.detail}" for p in group.pages)
                    manual = self._manual_group(r, brand, cfg, why)
                    group.manual_dir, group.manual_images = manual.manual_dir, manual.manual_images
                groups.append(group)
            for b in r.branding:
                g = CapturedGroup(
                    country=r.country,
                    retailer_slug=r.slug,
                    retailer=r.retailer_name,
                    brand=b.brand,
                    kind="branding",
                    mode=ExtractMode.DOM,
                    capture=CaptureMode.BANNER,
                    row_format=r.row_format,
                )
                for url in map(str, b.urls):
                    g.pages.append(await self._capture_banner(r, ctx, url, b.brand))
                groups.append(g)
        finally:
            await ctx.close()
        log.info("captured %s: %d groups", r.slug, len(groups))
        return groups

    # ------------------------------------------------------------------ page
    async def _capture_page(self, r: RetailerConfig, ctx: BrowserContext, url: str, cfg: BrandConfig) -> CapturedPage:
        now = datetime.now().astimezone()
        verdict = await self.prober.robots_check(r, url)
        if not verdict.allowed:
            st = AccessStatus.BLOCKED if verdict.access_denied else AccessStatus.ROBOTS
            return CapturedPage(url=url, captured_at=now, status=st, detail=verdict.detail)
        key = f"{r.slug}__{_safe(urlsplit(url).path)[:40]}_{_hash(url)}"
        shot_dir = self.shots / r.slug / f"{_safe(urlsplit(url).path or 'home')[:40]}_{_hash(url)}"
        shot_dir.mkdir(parents=True, exist_ok=True)
        cp = CapturedPage(url=url, captured_at=now, status=AccessStatus.OK)
        async with self.prober.throttle.lock(url):
            page = await ctx.new_page()
            try:
                resp = await self.prober.load(page, url)
                info = await inspect(page, resp)
                cp.final_url = info.final_url
                if info.blocked_marker or info.status in (401, 403, 429):
                    cp.status, cp.detail = AccessStatus.BLOCKED, f"anti-bot ({info.blocked_marker or info.status})"
                    await page.screenshot(path=str(shot_dir / "blocked.png"))
                    cp.screenshot = rel(shot_dir / "blocked.png")
                    return cp
                if info.status and info.status >= 400:
                    cp.status, cp.detail = AccessStatus.ERROR, f"HTTP {info.status}"
                    return cp
                await dismiss_overlays(page)
                await wait_for_prices(page, r.render_wait_seconds * 1000)
                await progressive_scroll(page)
                found = await detect_cards(page, cfg.selectors.product_card, cfg.selectors.name)
                cp.how = found["how"]
                await page.screenshot(path=str(shot_dir / "page.png"))
                cp.screenshot = rel(shot_dir / "page.png")
                html_path = self.html / f"{key}.html"
                html_path.write_text(await page.content(), encoding="utf-8")
                cp.html = rel(html_path)
                await page.evaluate(HIDE_FIXED_JS)
                for c in found["cards"][:MAX_CARD_SHOTS]:
                    card = RawCard.model_validate(
                        {k: c[k] for k in ("index", "text", "lines", "struck", "href", "img", "name")}
                    )
                    path = shot_dir / f"card_{c['index']:02d}.png"
                    try:
                        loc = page.locator(f'[data-cca-card="{c["index"]}"]')
                        await loc.scroll_into_view_if_needed(timeout=5_000)
                        await loc.screenshot(path=str(path), animations="disabled", timeout=15_000)
                        card.screenshot = rel(path)
                    except PlaywrightError as exc:
                        log.info("card %s screenshot failed: %s", c["index"], str(exc).splitlines()[0])
                    cp.cards.append(card)
                if not cp.cards:
                    cp.detail = "no product cards found (empty listing or selector needed)"
            except PlaywrightError as exc:
                cp.status, cp.detail = AccessStatus.ERROR, f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
                return cp
            finally:
                await page.close()
        if cfg.mode == ExtractMode.SHOPIFY:
            await self._attach_shopify(r, cp)
        return cp

    async def _attach_shopify(self, r: RetailerConfig, cp: CapturedPage) -> None:
        parts = urlsplit(cp.final_url or cp.url)
        path = re.sub(r"/+$", "", parts.path)
        m = re.match(r"(.*/collections/[^/]+)", path)
        if not m:
            cp.detail += " · not a Shopify collection URL"
            return
        api = urlunsplit((parts.scheme, parts.netloc, m.group(1) + "/products.json", "limit=250", ""))
        verdict = await self.prober.robots_check(r, api)
        if not verdict.allowed:
            cp.detail += " · products.json disallowed by robots.txt: DOM fallback"
            return
        async with self.prober.throttle.lock(api):
            await self.prober.throttle.wait_turn(api)
            try:
                resp = await self.prober.client(r).get(api, follow_redirects=False)
                resp.raise_for_status()
                products = resp.json().get("products", [])
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                cp.detail += f" · products.json failed ({type(exc).__name__}): DOM fallback"
                return
        cp.shopify = products
        (self.html / f"{r.slug}__{_safe(m.group(1))}__products.json").write_text(
            json.dumps(products, ensure_ascii=False), encoding="utf-8"
        )
        by_handle = {p.get("handle"): p for p in products}
        for card in cp.cards:
            h = re.search(r"/products/([^/?#]+)", card.href or "")
            if h and h.group(1) in by_handle:
                card.product_json = by_handle[h.group(1)]

    async def _capture_banner(self, r: RetailerConfig, ctx: BrowserContext, url: str, brand: Brand) -> CapturedPage:
        now = datetime.now().astimezone()
        verdict = await self.prober.robots_check(r, url)
        if not verdict.allowed:
            return CapturedPage(url=url, captured_at=now, status=AccessStatus.ROBOTS, detail=verdict.detail)
        shot_dir = self.shots / r.slug / "branding"
        shot_dir.mkdir(parents=True, exist_ok=True)
        cp = CapturedPage(url=url, captured_at=now, status=AccessStatus.OK)
        async with self.prober.throttle.lock(url):
            page = await ctx.new_page()
            try:
                resp = await self.prober.load(page, url)
                info = await inspect(page, resp)
                if info.blocked_marker or info.status in (401, 403, 429):
                    cp.status, cp.detail = AccessStatus.BLOCKED, f"anti-bot ({info.blocked_marker or info.status})"
                    return cp
                await dismiss_overlays(page)
                await page.wait_for_timeout(1500)
                path = shot_dir / f"banner_{_safe(urlsplit(url).netloc)}_{_hash(url)}.png"
                hero = page.locator(
                    "[class*=hero i], [class*=banner i], [class*=carousel i], [class*=slider i], .swiper"
                ).first
                try:
                    box = await hero.bounding_box(timeout=3_000)
                except PlaywrightError:
                    box = None
                if box and box["width"] > 600 and 200 < box["height"] < 900 and box["y"] < 700:
                    await hero.screenshot(path=str(path), animations="disabled")
                else:  # fall back to the top of the page
                    await page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": 1440, "height": 640})
                cp.screenshot = rel(path)
                cp.final_url = info.final_url
            except PlaywrightError as exc:
                cp.status, cp.detail = AccessStatus.ERROR, f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
            finally:
                await page.close()
        log.info("banner %s %s: %s", r.slug, brand.value, cp.status.value)
        return cp


def merge_groups(old: list[CapturedGroup], new: list[CapturedGroup]) -> list[CapturedGroup]:
    """Replace the groups of re-captured retailers, keep the rest (subset runs)."""
    fresh = {g.retailer_slug for g in new}
    return [g for g in old if g.retailer_slug not in fresh] + new
