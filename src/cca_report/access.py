"""Phase 0: probe every configured URL and classify it (ok / js_heavy / blocked / robots)."""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TypeVar
from urllib.parse import urljoin

import httpx
from playwright.async_api import Browser, BrowserContext, Page, Response
from playwright.async_api import Error as PlaywrightError

from .brands import BRAND_PATTERNS
from .capture.browser import (
    dismiss_overlays,
    goto,
    inspect,
    install_robots_guard,
    launch,
    new_context,
    progressive_scroll,
)
from .config import Project, Settings
from .models import AccessResult, AccessStatus, Brand, RetailerConfig
from .net import DomainThrottle, RobotsCache, RobotsVerdict, domain_of, make_client

log = logging.getLogger(__name__)
T = TypeVar("T")

_TAGS = re.compile(r"<(script|style|noscript|template)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
_WS = re.compile(r"\s+")
_BODY_TEXT_JS = "() => document.body ? document.body.innerText : ''"


def static_text_chars(html: str) -> int:
    return len(_WS.sub(" ", html_lib.unescape(_TAGS.sub(" ", html))).strip())


def brand_hits(text: str) -> dict[str, int]:
    return {b.value: len(p.findall(text)) for b, p in BRAND_PATTERNS.items()}


def classify(
    *,
    robots_allowed: bool,
    http_status: int | None,
    static_chars: int | None,
    browser_status: int | None,
    rendered_chars: int | None,
    blocked_marker: str | None,
) -> tuple[AccessStatus, str]:
    if not robots_allowed:
        return AccessStatus.ROBOTS, "robots.txt disallows this path"
    if browser_status in (401, 403, 429) or blocked_marker:
        why = f"marker '{blocked_marker}'" if blocked_marker else f"HTTP {browser_status}"
        return AccessStatus.BLOCKED, f"anti-bot / access control ({why})"
    if browser_status is None and rendered_chars is None:
        return AccessStatus.ERROR, "browser could not load the page"
    if browser_status and browser_status >= 500:
        return AccessStatus.ERROR, f"server error HTTP {browser_status}"
    if browser_status and browser_status >= 400:
        return AccessStatus.ERROR, f"HTTP {browser_status} (URL moved or removed?)"
    if (rendered_chars or 0) < 200:
        return AccessStatus.ERROR, "page rendered almost no text"
    static_failed = http_status is None or http_status >= 400
    if static_failed or (static_chars or 0) < 0.4 * (rendered_chars or 0):
        return AccessStatus.JS_HEAVY, (f"plain HTTP {http_status or 'failed'}; content appears only after JS render")
    return AccessStatus.OK, "static HTML already contains the content"


class Prober:
    """Holds the shared browser, throttle and robots caches for a Phase 0 session."""

    def __init__(self, project: Project, settings: Settings, out_dir: Path) -> None:
        self.project = project
        self.settings = settings
        self.out_dir = out_dir
        self.shots_dir = out_dir / "screenshots"
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.throttle = DomainThrottle(project.report.min_delay_seconds)
        self.clients: dict[str, httpx.AsyncClient] = {}
        self.robots: dict[str, RobotsCache] = {}
        self.browser: Browser | None = None
        self.sem = asyncio.Semaphore(4)

    def client(self, r: RetailerConfig) -> httpx.AsyncClient:
        key = r.country.value
        if key not in self.clients:
            self.clients[key] = make_client(self.settings.proxy_for(r.country))
            self.robots[key] = RobotsCache(self.clients[key], self.throttle)
        return self.clients[key]

    async def robots_check(self, r: RetailerConfig, url: str) -> RobotsVerdict:
        self.client(r)
        verdict = await self.robots[r.country.value].check(url)
        if verdict.crawl_delay:
            self.throttle.set_delay(domain_of(url), verdict.crawl_delay)
        return verdict

    async def open_context(self, r: RetailerConfig, scale: float = 1) -> BrowserContext:
        """Browser context with country locale/timezone and a robots.txt navigation guard."""
        assert self.browser is not None
        ctx = await new_context(
            self.browser,
            self.project.country(r.country),
            proxy=self.settings.proxy_for(r.country),
            scale=scale,
        )

        async def allowed(url: str) -> bool:
            return (await self.robots_check(r, url)).allowed

        await install_robots_guard(ctx, allowed)
        return ctx

    async def load(self, page: Page, url: str) -> Response | None:
        """Throttled navigation (every retry waits its turn). Caller holds the domain lock."""
        return await goto(
            page,
            url,
            self.project.report.page_timeout_seconds,
            before_attempt=lambda: self.throttle.wait_turn(url),
        )

    async def run(
        self,
        retailers: list[RetailerConfig],
        fn: Callable[[RetailerConfig], Awaitable[T]],
        on_error: Callable[[RetailerConfig, Exception], T],
    ) -> list[T]:
        """Run ``fn`` per retailer; one failing retailer never aborts the others."""
        async with launch(self.settings.headless) as browser:
            self.browser = browser
            try:
                outcomes = await asyncio.gather(*(self._guarded(r, fn) for r in retailers), return_exceptions=True)
            finally:
                for c in self.clients.values():
                    await c.aclose()
        results: list[T] = []
        for r, out in zip(retailers, outcomes, strict=True):
            if isinstance(out, BaseException):
                if not isinstance(out, Exception):
                    raise out  # KeyboardInterrupt / CancelledError
                log.error("%s failed: %s: %s", r.slug, type(out).__name__, out)
                results.append(on_error(r, out))
            else:
                results.append(out)
        return results

    async def _guarded(self, r: RetailerConfig, fn: Callable[[RetailerConfig], Awaitable[T]]) -> T:
        async with self.sem:
            return await fn(r)

    # ------------------------------------------------------------------ access check
    @staticmethod
    def _base(r: RetailerConfig, url: str, verdict: RobotsVerdict | None = None) -> dict:
        return dict(
            slug=r.slug,
            country=r.country,
            retailer=r.retailer_name,
            url=url,
            robots_allowed=verdict.allowed if verdict else None,
            crawl_delay=verdict.crawl_delay if verdict else None,
            checked_at=datetime.now().astimezone(),
        )

    def error_results(self, r: RetailerConfig, exc: Exception) -> list[AccessResult]:
        return [
            AccessResult(
                status=AccessStatus.ERROR,
                detail=f"retailer aborted: {type(exc).__name__}: {str(exc)[:160]}",
                **self._base(r, str(r.base_url)),
            )
        ]

    @staticmethod
    def _brand_for_url(r: RetailerConfig, url: str) -> Brand | None:
        """Brand whose dedicated URL this is (None for shared multi-brand pages)."""
        for b, cfg in r.brands.items():
            if not cfg.filter_by_name and url in {str(u) for u in cfg.urls}:
                return b
        return None

    async def check_retailer(self, r: RetailerConfig) -> list[AccessResult]:
        results: list[AccessResult] = []
        ctx: BrowserContext | None = None
        try:
            for i, url in enumerate(r.all_urls):
                verdict = await self.robots_check(r, url)
                base = self._base(r, url, verdict)
                if not verdict.allowed:
                    st = AccessStatus.BLOCKED if verdict.access_denied else AccessStatus.ROBOTS
                    results.append(AccessResult(status=st, detail=verdict.detail, **base))
                    continue
                if r.manual_only:
                    results.append(
                        AccessResult(status=AccessStatus.MANUAL, detail="configured manual_only; not probed", **base)
                    )
                    continue
                if ctx is None:
                    ctx = await self.open_context(r)
                results.append(await self._probe(r, ctx, i, url, base))
        finally:
            if ctx is not None:
                await ctx.close()
        log.info("checked %s (%d urls)", r.slug, len(results))
        return results

    async def _probe(self, r: RetailerConfig, ctx: BrowserContext, i: int, url: str, base: dict) -> AccessResult:
        shot = self.shots_dir / f"{r.slug}__{i:02d}.png"
        async with self.throttle.lock(url):
            http_status, static_chars, static_final = await self._static(r, url)
            page = await ctx.new_page()
            try:
                resp = await self.load(page, url)
                # Inspect BEFORE any interaction: never click on a challenge page.
                info = await inspect(page, resp)
                blocked = bool(info.blocked_marker) or info.status in (401, 403, 429)
                if not blocked:
                    await dismiss_overlays(page)
                    await progressive_scroll(page, max_steps=8)
                    info = await inspect(page, resp)
                text = await page.evaluate(_BODY_TEXT_JS)
                await page.screenshot(path=str(shot))
            except PlaywrightError as exc:
                return AccessResult(
                    status=AccessStatus.ERROR,
                    http_status=http_status,
                    static_text_chars=static_chars,
                    detail=f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}",
                    **base,
                )
            finally:
                await page.close()
        status, detail = classify(
            robots_allowed=True,
            http_status=http_status,
            static_chars=static_chars,
            browser_status=info.status,
            rendered_chars=info.text_chars,
            blocked_marker=info.blocked_marker,
        )
        # Redirects: the final URL (plain HTTP and browser) must be allowed too.
        for final in {info.final_url, static_final} - {None, url}:
            v = await self.robots_check(r, final)
            if not v.allowed:
                status, detail = AccessStatus.ROBOTS, f"redirected to {final}, disallowed by robots.txt"
            elif domain_of(final) != domain_of(url):
                detail += f" · redirected off-domain to {domain_of(final)}"
        hits = brand_hits(text)
        brand = self._brand_for_url(r, url)
        if status in (AccessStatus.OK, AccessStatus.JS_HEAVY) and brand and hits.get(brand.value, 0) == 0:
            detail += f" · WARNING: no '{brand.value}' mentions (wrong URL, empty filter or iframe?)"
        return AccessResult(
            status=status,
            http_status=http_status,
            browser_status=info.status,
            static_text_chars=static_chars,
            rendered_text_chars=info.text_chars,
            brand_hits=hits,
            title=info.title,
            final_url=info.final_url,
            screenshot=shot.relative_to(self.out_dir).as_posix(),
            detail=detail,
            **base,
        )

    async def _static(self, r: RetailerConfig, url: str) -> tuple[int | None, int | None, str | None]:
        """Plain HTTP GET without auto-redirects; every hop is robots-checked and throttled."""
        target = url
        for _ in range(5):
            await self.throttle.wait_turn(target)
            try:
                resp = await self.client(r).get(target, follow_redirects=False)
            except httpx.HTTPError as exc:
                log.info("static fetch failed %s: %s", target, type(exc).__name__)
                return None, None, None
            if resp.is_redirect and "location" in resp.headers:
                nxt = urljoin(target, resp.headers["location"])
                if not (await self.robots_check(r, nxt)).allowed:
                    return None, None, nxt
                target = nxt
                continue
            return resp.status_code, static_text_chars(resp.text), target
        return None, None, target
