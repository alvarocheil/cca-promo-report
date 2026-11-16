"""Playwright helpers shared by discovery and capture."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
)
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..models import CountryConfig
from ..net import BROWSER_UA

log = logging.getLogger(__name__)

# Text fragments that reveal an anti-bot interstitial. We only *detect* them: the tool
# never tries to solve or evade a challenge (spec §2), it marks the site as blocked.
STRONG_BLOCK_MARKERS = (
    "cf-chl",
    "challenge-platform",
    "attention required",
    "just a moment",
    "access denied",
    "acceso denegado",
    "request unsuccessful",
    "incapsula",
    "datadome",
    "px-captcha",
    "are you a robot",
    "verify you are human",
    "verifica que eres humano",
)
# "captcha" alone shows up in legit pages (reCAPTCHA on newsletter forms): only counts
# when the page has almost no visible content.
WEAK_BLOCK_MARKERS = ("captcha",)
WEAK_MARKER_MAX_TEXT = 1_000


def find_block_marker(text: str, html: str) -> str | None:
    hay = text[:20_000].lower() + " " + html[:200_000].lower()
    strong = next((m for m in STRONG_BLOCK_MARKERS if m in hay), None)
    if strong:
        return strong
    if len(text) <= WEAK_MARKER_MAX_TEXT:
        return next((m for m in WEAK_BLOCK_MARKERS if m in hay), None)
    return None


COOKIE_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#didomi-notice-agree-button",
    "button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    ".cc-allow",
    ".cookie-accept",
)
COOKIE_TEXT = re.compile(
    r"^\s*(aceptar( todas?)?( las cookies)?|acepto|accept( all)?|entendido|de acuerdo|ok|"
    r"permitir( todas?)?|continuar)\s*$",
    re.I,
)
CLOSE_SELECTORS = (
    "[aria-label='Close' i]",
    "[aria-label='Cerrar' i]",
    "button.close",
    ".modal [class*='close' i]",
    "[class*='popup' i] [class*='close' i]",
    ".needsclick button[aria-label*='close' i]",  # Klaviyo
)


@dataclass(slots=True)
class LoadResult:
    status: int | None
    final_url: str
    title: str
    text_chars: int
    blocked_marker: str | None


@asynccontextmanager
async def launch(headless: bool = True) -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            yield browser
        finally:
            await browser.close()


async def new_context(
    browser: Browser,
    country: CountryConfig,
    *,
    proxy: str | None = None,
    scale: float = 2.0,
) -> BrowserContext:
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        device_scale_factor=scale,
        locale=country.locale,
        timezone_id=country.timezone,
        user_agent=BROWSER_UA,
        proxy={"server": proxy} if proxy else None,
        extra_http_headers={"Accept-Language": f"{country.locale},es;q=0.9,en;q=0.7"},
    )
    ctx.set_default_timeout(15_000)
    return ctx


async def goto(
    page: Page,
    url: str,
    timeout_s: int = 45,
    before_attempt: Callable[[], Awaitable[None]] | None = None,
) -> Response | None:
    """Navigate with retries on timeouts / transient network errors (not on HTTP codes).

    ``before_attempt`` runs before *every* attempt (pass the domain throttle so retries
    also respect the per-domain delay / Crawl-delay).
    """
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=3, min=3, max=20),
        retry=retry_if_exception_type((PlaywrightTimeout, PlaywrightError)),
        reraise=True,
    ):
        with attempt:
            if before_attempt is not None:
                await before_attempt()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            # sites with long-polling never go idle; DOM is enough
            with suppress(PlaywrightTimeout):
                await page.wait_for_load_state("networkidle", timeout=15_000)
            return resp
    return None  # pragma: no cover


async def dismiss_overlays(page: Page) -> int:
    """Close cookie banners and modals generically. Returns number of clicks."""
    clicks = 0
    for sel in COOKIE_SELECTORS + CLOSE_SELECTORS:
        loc = page.locator(sel)
        try:
            if await loc.count() and await loc.first.is_visible():
                await loc.first.click(timeout=2_000)
                clicks += 1
                await page.wait_for_timeout(300)
        except PlaywrightError:
            continue
    buttons = page.get_by_role("button", name=COOKIE_TEXT)
    try:
        for i in range(min(await buttons.count(), 3)):
            b = buttons.nth(i)
            if await b.is_visible():
                await b.click(timeout=2_000)
                clicks += 1
    except PlaywrightError:
        pass
    with suppress(PlaywrightError):
        await page.keyboard.press("Escape")
    return clicks


async def progressive_scroll(page: Page, step: int = 700, max_steps: int = 25) -> None:
    """Scroll down in steps so lazy images load, then return to top."""
    last_height = 0
    for _ in range(max_steps):
        height = await page.evaluate("document.body ? document.body.scrollHeight : 0")
        pos = await page.evaluate("window.scrollY + window.innerHeight")
        if pos >= height and height == last_height:
            break
        last_height = height
        await page.mouse.wheel(0, step)
        await page.wait_for_timeout(350)
    await wait_images(page)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(300)


async def wait_images(page: Page, timeout_ms: int = 8_000) -> None:
    """Poll from Python: ``wait_for_function`` uses eval and breaks on strict-CSP sites."""
    waited = 0
    while waited < timeout_ms:
        try:
            done = await page.evaluate("() => Array.from(document.images).every(i => i.complete)")
        except PlaywrightError:
            return
        if done:
            return
        await page.wait_for_timeout(250)
        waited += 250


async def inspect(page: Page, resp: Response | None) -> LoadResult:
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    marker = find_block_marker(text, await page.content())
    return LoadResult(
        status=resp.status if resp else None,
        final_url=page.url,
        title=(await page.title())[:200],
        text_chars=len(text),
        blocked_marker=marker,
    )


async def install_robots_guard(ctx: BrowserContext, is_allowed: Callable[[str], Awaitable[bool]]) -> None:
    """Abort main-frame navigations (incl. client-side redirects) that robots.txt forbids."""

    async def handler(route: Route) -> None:
        req = route.request
        if req.is_navigation_request() and req.resource_type == "document" and not await is_allowed(req.url):
            log.info("robots guard aborted navigation to %s", req.url)
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    await ctx.route("**/*", handler)
