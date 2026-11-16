"""Phase 0: navigate each retailer's menu and propose category / brand URLs.

Proposals are written to ``runs/discovery/proposals/<slug>.yaml``; they are never applied
to ``config/retailers`` automatically — a human approves them first.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import yaml
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from .access import Prober
from .brands import BRAND_PATTERNS
from .capture.browser import dismiss_overlays, inspect
from .models import AccessStatus, Brand, DiscoverConfig, DiscoveryResult, LinkCandidate, RetailerConfig, Visit
from .net import domain_of

log = logging.getLogger(__name__)

NOT_PHONES = re.compile(
    r"funda|case|carcasa|cargador|charger|accesorio|protector|vidrio|cable|reloj|watch|"
    r"tablet|ipad|audifono|aud[ií]fono|buds|airpods|parlante|tv|televisor|laptop|"
    r"notebook|mac\b|macbook|plan(es)?-|recarga|portabilidad|fijo|internet|hogar",
    re.I,
)
# Signals of a single product page rather than a listing.
PRODUCT_PAGE = re.compile(
    r"/p/?$|/dp/|/producto?/|\d{2,4}\s?gb|-\d+gb|sku|/item/",
    re.I,
)
SKIP_HREF = re.compile(r"^(javascript:|mailto:|tel:|#)", re.I)

_COLLECT_JS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
  href: a.getAttribute('href') || '',
  text: (a.innerText || a.textContent || a.getAttribute('aria-label') || a.title || '')
        .replace(/\\s+/g, ' ').trim().slice(0, 120),
}))
"""


def _same_site(url: str, base: str) -> bool:
    a, b = domain_of(url), domain_of(base)
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def score_link(href: str, text: str, keywords: list[str], menu_texts: list[str]) -> float:
    parts = urlsplit(href)
    path = parts.path.lower() + "?" + (parts.query or "").lower()
    t = text.lower()
    s = 0.0
    if any(k in path for k in keywords):
        s += 3
    if any(k in t for k in keywords):
        s += 2
    if any(m.lower() == t for m in menu_texts):
        s += 2
    if NOT_PHONES.search(path) or NOT_PHONES.search(t):
        s -= 4
    if PRODUCT_PAGE.search(parts.path):
        s -= 3
    s -= path.count("/") * 0.1  # prefer shallower category pages
    return round(s, 2)


def which_brand(href: str, text: str) -> Brand | None:
    blob = f"{urlsplit(href).path} {urlsplit(href).query} {text}"
    for b, pat in BRAND_PATTERNS.items():
        if pat.search(blob):
            return b
    return None


def rank_links(
    raw: list[dict[str, str]], page_url: str, base_url: str, r: RetailerConfig
) -> tuple[list[LinkCandidate], dict[Brand, list[LinkCandidate]]]:
    d = r.discover or DiscoverConfig()
    seen: dict[str, LinkCandidate] = {}
    for item in raw:
        href = item.get("href", "").strip()
        if not href or SKIP_HREF.match(href):
            continue
        url = urldefrag(urljoin(page_url, href)).url
        if not url.startswith("http") or not _same_site(url, base_url):
            continue
        text = item.get("text", "")
        sc = score_link(url, text, d.keywords, d.menu_texts)
        cand = LinkCandidate(url=url, text=text, score=sc, brand=which_brand(url, text))
        if url not in seen or seen[url].score < sc:
            seen[url] = cand
    cats = [c for c in seen.values() if c.score >= 2 and c.brand is None]
    cat_paths = [urlsplit(k.url).path.rstrip("/") for k in cats if urlsplit(k.url).path not in ("", "/")]
    brands: dict[Brand, list[LinkCandidate]] = {}
    for c in seen.values():
        if not c.brand or PRODUCT_PAGE.search(urlsplit(c.url).path):
            continue
        # brand link that looks like a phone listing, or that lives under a phone category
        under_cat = any(urlsplit(c.url).path.startswith(p + "/") for p in cat_paths)
        if c.score >= 2 or (under_cat and c.score > -1):
            brands.setdefault(c.brand, []).append(c)
    return cats, brands


async def _reveal_menus(page: Page, menu_texts: list[str]) -> None:
    for txt in menu_texts:
        loc = page.get_by_text(txt, exact=False)
        try:
            for i in range(min(await loc.count(), 3)):
                el = loc.nth(i)
                if await el.is_visible():
                    await el.hover(timeout=2_000)
                    await page.wait_for_timeout(700)
        except PlaywrightError:
            continue


def discovery_error(r: RetailerConfig, exc: Exception) -> DiscoveryResult:
    return DiscoveryResult(
        slug=r.slug,
        country=r.country,
        retailer=r.retailer_name,
        status=AccessStatus.ERROR,
        detail=f"retailer aborted: {type(exc).__name__}: {str(exc)[:160]}",
    )


async def discover_retailer(prober: Prober, r: RetailerConfig) -> DiscoveryResult:
    d = r.discover
    res = DiscoveryResult(slug=r.slug, country=r.country, retailer=r.retailer_name, status=AccessStatus.OK)
    if d is None:
        res.detail = "no discover block in YAML"
        return res
    if r.manual_only:
        res.status, res.detail = AccessStatus.MANUAL, "manual_only retailer, skipped"
        return res
    start = [str(u) for u in d.start_urls] or [str(r.base_url)]
    shots = prober.shots_dir
    all_cats: list[LinkCandidate] = []
    all_brands: dict[Brand, list[LinkCandidate]] = {}
    ctx = await prober.open_context(r)
    try:
        queue = list(start)
        visited: set[str] = set()
        loaded_ok = 0
        while queue and len(visited) < len(start) + 2:  # start pages + 2 best category pages
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            verdict = await prober.robots_check(r, url)
            visit = Visit(url=url, visited_at=datetime.now().astimezone())
            res.visits.append(visit)
            if not verdict.allowed:
                visit.outcome = verdict.detail
                continue
            shot = shots / f"discover_{r.slug}__{len(res.visits) - 1:02d}.png"
            async with prober.throttle.lock(url):
                page = await ctx.new_page()
                try:
                    resp = await prober.load(page, url)
                    info = await inspect(page, resp)  # before any interaction
                    visit.final_url, visit.http_status = info.final_url, info.status
                    if info.blocked_marker or info.status in (401, 403, 429):
                        await page.screenshot(path=str(shot))
                        visit.screenshot = shot.relative_to(prober.out_dir).as_posix()
                        visit.outcome = f"blocked ({info.blocked_marker or info.status})"
                        res.status = AccessStatus.BLOCKED
                        break
                    if info.status and info.status >= 400:
                        visit.outcome = f"HTTP {info.status}"
                        continue
                    await dismiss_overlays(page)
                    await _reveal_menus(page, d.menu_texts)
                    await page.screenshot(path=str(shot))
                    visit.screenshot = shot.relative_to(prober.out_dir).as_posix()
                    raw = await page.evaluate(_COLLECT_JS)
                    page_url = page.url
                except PlaywrightError as exc:
                    visit.outcome = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
                    continue
                finally:
                    await page.close()
            if not (await prober.robots_check(r, page_url)).allowed:
                visit.outcome = f"redirected to {page_url}, disallowed by robots.txt"
                continue
            loaded_ok += 1
            cats, brands = rank_links(raw, page_url, str(r.base_url), r)
            visit.outcome = f"ok: {len(raw)} links, {len(cats)} category candidates"
            all_cats += cats
            for b, lst in brands.items():
                all_brands.setdefault(b, []).extend(lst)
            for c in sorted(cats, key=lambda c: -c.score)[:2]:
                if c.url not in visited:
                    queue.append(c.url)
    finally:
        await ctx.close()

    if res.status == AccessStatus.OK and loaded_ok == 0:
        res.status = AccessStatus.ROBOTS if all("robots" in v.outcome for v in res.visits) else AccessStatus.ERROR
    res.detail = "; ".join(f"{v.url} → {v.outcome}" for v in res.visits if not v.outcome.startswith("ok"))

    def dedupe(items: list[LinkCandidate], n: int) -> list[LinkCandidate]:
        best: dict[str, LinkCandidate] = {}
        for c in items:
            if c.url not in best or best[c.url].score < c.score:
                best[c.url] = c
        return sorted(best.values(), key=lambda c: -c.score)[:n]

    res.category_candidates = dedupe(all_cats, 6)
    res.brand_candidates = {b: dedupe(v, 3) for b, v in all_brands.items()}
    if res.status == AccessStatus.OK and not res.category_candidates:
        res.detail += " no phone category link found in menus"
    log.info("discovered %s: %s, %d categories", r.slug, res.status.value, len(res.category_candidates))
    return res


def write_proposal(res: DiscoveryResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "status": res.status.value,
        "detail": res.detail.strip(),
        "visits": [v.model_dump(mode="json") for v in res.visits],
        "category_candidates": [{"url": c.url, "text": c.text, "score": c.score} for c in res.category_candidates],
        "brands": {b.value: {"proposed_urls": [c.url for c in lst]} for b, lst in res.brand_candidates.items()},
    }
    (out_dir / f"{res.slug}.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
