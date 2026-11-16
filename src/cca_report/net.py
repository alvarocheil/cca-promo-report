"""Polite networking: robots.txt, per-domain throttling and a shared HTTP client."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from protego import Protego

log = logging.getLogger(__name__)

# Standard desktop Chrome UA (spec §6 "user-agent de navegador estándar").
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
# Token used for robots.txt evaluation, together with the wildcard group.
ROBOTS_AGENT = "CCAPromoReport"


def domain_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class DomainThrottle:
    """One session at a time per domain and a minimum delay between page loads."""

    def __init__(self, min_delay: float = 3.0) -> None:
        self.min_delay = min_delay
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._delay_override: dict[str, float] = {}

    def set_delay(self, domain: str, seconds: float) -> None:
        self._delay_override[domain] = max(self.min_delay, seconds)

    def lock(self, url: str) -> asyncio.Lock:
        return self._locks.setdefault(domain_of(url), asyncio.Lock())

    async def wait_turn(self, url: str) -> None:
        """Call while holding ``lock(url)`` right before each request."""
        dom = domain_of(url)
        delay = self._delay_override.get(dom, self.min_delay)
        elapsed = time.monotonic() - self._last.get(dom, 0.0)
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last[dom] = time.monotonic()


@dataclass(slots=True)
class RobotsVerdict:
    allowed: bool
    crawl_delay: float | None
    detail: str
    access_denied: bool = False  # robots.txt itself answered 401/403 (access control)


@dataclass(slots=True)
class _RobotsEntry:
    parser: Protego | None  # None = no rules (allow all)
    allow_all: bool
    deny_all: bool
    access_denied: bool
    detail: str


class RobotsCache:
    """Fetches and evaluates robots.txt once per origin, following RFC 9309.

    * 2xx -> parsed with Protego (wildcards, ``$`` and longest-match precedence).
    * 401/403 -> the site refuses us even robots.txt: treated as access control, no crawl.
    * other 4xx -> "unavailable": allowed (RFC 9309 §2.3.1.3).
    * 5xx / network error -> retried once; if still unreachable, complete disallow
      (RFC 9309 §2.3.1.4).
    Robots requests go through the same per-domain throttle as page loads.
    """

    def __init__(self, client: httpx.AsyncClient, throttle: DomainThrottle | None = None) -> None:
        self.client = client
        self.throttle = throttle
        self._entries: dict[str, _RobotsEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _fetch(self, origin: str) -> _RobotsEntry:
        last_err = ""
        for attempt in range(2):
            if self.throttle:
                await self.throttle.wait_turn(origin)
            try:
                resp = await self.client.get(f"{origin}/robots.txt", timeout=20)
            except httpx.HTTPError as exc:
                last_err = f"unreachable ({type(exc).__name__})"
            else:
                code = resp.status_code
                if code in (401, 403):
                    return _RobotsEntry(
                        None, False, True, True, f"robots.txt HTTP {code}: access denied to automated clients"
                    )
                if 400 <= code < 500:
                    return _RobotsEntry(None, True, False, False, f"robots.txt HTTP {code} -> allow all")
                if code < 400:
                    return _RobotsEntry(Protego.parse(resp.text), False, False, False, "robots.txt parsed")
                last_err = f"HTTP {code}"
            if attempt == 0:
                await asyncio.sleep(5)
        return _RobotsEntry(None, False, True, False, f"robots.txt {last_err} -> disallow (RFC 9309)")

    async def _entry(self, url: str) -> _RobotsEntry:
        origin = origin_of(url)
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._entries:
                self._entries[origin] = await self._fetch(origin)
            return self._entries[origin]

    async def check(self, url: str) -> RobotsVerdict:
        e = await self._entry(url)
        if e.deny_all:
            return RobotsVerdict(False, None, e.detail, e.access_denied)
        if e.allow_all or e.parser is None:
            return RobotsVerdict(True, None, e.detail)
        allowed = bool(e.parser.can_fetch(url, ROBOTS_AGENT))
        delay = e.parser.crawl_delay(ROBOTS_AGENT)
        detail = e.detail if allowed else f"robots.txt disallows {urlsplit(url).path or '/'}"
        return RobotsVerdict(allowed, float(delay) if delay else None, detail)


def make_client(proxy: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": BROWSER_UA,
            "Accept-Language": "es-419,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        follow_redirects=True,
        timeout=httpx.Timeout(45.0, connect=20.0),
        proxy=proxy,
        http2=False,
    )
