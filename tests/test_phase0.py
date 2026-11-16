import asyncio
from datetime import date

import httpx
import pytest

from cca_report.access import brand_hits, classify, static_text_chars
from cca_report.config import Project
from cca_report.discover import rank_links, score_link, which_brand
from cca_report.models import AccessStatus, Brand, ReportConfig
from cca_report.net import DomainThrottle, RobotsCache, domain_of


def test_all_configs_load():
    p = Project()
    import re

    assert re.fullmatch(r"\d{4}-W\d{2}", p.report.week)  # "auto" resolves to the current ISO week
    assert p.report.cutoff_date.isoweekday() == 5  # Friday
    assert len(p.retailers) >= 17
    assert [c.code.value for c in p.report.countries] == ["PA", "CR", "EC", "PY", "UY"]


@pytest.mark.parametrize(
    ("d", "label"),
    [
        (date(2026, 10, 30), "October 30th, 2026"),
        (date(2026, 11, 1), "November 1st, 2026"),
        (date(2026, 11, 2), "November 2nd, 2026"),
        (date(2026, 11, 3), "November 3rd, 2026"),
        (date(2026, 11, 6), "November 6th, 2026"),
        (date(2026, 11, 11), "November 11th, 2026"),
        (date(2026, 11, 22), "November 22nd, 2026"),
    ],
)
def test_cutoff_label(d, label):
    cfg = ReportConfig(week="2026-W45", cutoff_date=d, countries=[])
    assert cfg.cutoff_label == label


def test_week_validation():
    with pytest.raises(ValueError):
        ReportConfig(week="2026-45", cutoff_date=date(2026, 11, 6), countries=[])


def test_classify_paths():
    base = dict(
        robots_allowed=True,
        http_status=200,
        static_chars=5000,
        browser_status=200,
        rendered_chars=6000,
        blocked_marker=None,
    )
    assert classify(**base)[0] is AccessStatus.OK
    assert classify(**{**base, "robots_allowed": False})[0] is AccessStatus.ROBOTS
    assert classify(**{**base, "browser_status": 403})[0] is AccessStatus.BLOCKED
    assert classify(**{**base, "blocked_marker": "just a moment"})[0] is AccessStatus.BLOCKED
    assert classify(**{**base, "static_chars": 300})[0] is AccessStatus.JS_HEAVY
    assert classify(**{**base, "http_status": None, "static_chars": None})[0] is AccessStatus.JS_HEAVY
    assert classify(**{**base, "browser_status": 404})[0] is AccessStatus.ERROR


def test_static_text_strips_scripts():
    html = "<html><script>var a='xxxxxxxxxxxx';</script><body><p>Hola  mundo</p></body></html>"
    assert static_text_chars(html) == len("Hola mundo")


def test_brand_hits():
    hits = brand_hits("Galaxy S26 Ultra · iPhone 17 · HONOR Magic8 · moto g85 · Razr 70")
    assert hits == {"Samsung": 1, "Apple": 1, "Honor": 1, "Motorola": 2}


def test_link_scoring_and_brand():
    kw, menu = ["celular"], ["Celulares"]
    assert score_link("https://x.com/celulares", "Celulares", kw, menu) > score_link(
        "https://x.com/celulares/fundas", "Fundas para celular", kw, menu
    )
    assert which_brand("https://x.com/celulares/samsung", "") is Brand.SAMSUNG
    assert which_brand("https://x.com/c", "iPhone 17") is Brand.APPLE


def test_rank_links_filters_offsite_and_accessories():
    r = next(x for x in Project().retailers if x.slug == "pa_panafoto")
    raw = [
        {"href": "/celulares", "text": "Celulares"},
        {"href": "/celulares/samsung", "text": "Samsung"},
        {"href": "/celulares/accesorios", "text": "Accesorios para celular"},
        {"href": "https://facebook.com/celulares", "text": "Celulares"},
        {"href": "javascript:void(0)", "text": "Celulares"},
    ]
    cats, brands = rank_links(raw, "https://www.panafoto.com/", "https://www.panafoto.com", r)
    assert [c.url for c in cats] == ["https://www.panafoto.com/celulares"]
    assert brands[Brand.SAMSUNG][0].url == "https://www.panafoto.com/celulares/samsung"


def test_domain_of():
    assert domain_of("https://www.tigo.com.pa/x") == "tigo.com.pa"


def test_robots_cache():
    robots = "User-agent: *\nDisallow: /checkout\nCrawl-delay: 5\n"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "forbidden.test":
            return httpx.Response(403)
        return httpx.Response(200, text=robots)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            rc = RobotsCache(c)
            ok = await rc.check("https://shop.test/celulares")
            no = await rc.check("https://shop.test/checkout/1")
            forb = await rc.check("https://forbidden.test/")
            return ok, no, forb

    ok, no, forb = asyncio.run(go())
    assert ok.allowed and ok.crawl_delay == 5
    assert not no.allowed
    assert not forb.allowed


def test_throttle_enforces_min_delay():
    t = DomainThrottle(min_delay=0.2)

    async def go():
        import time

        start = time.monotonic()
        await t.wait_turn("https://a.test/1")
        await t.wait_turn("https://a.test/2")
        await t.wait_turn("https://b.test/1")  # other domain: no extra wait
        return time.monotonic() - start

    elapsed = asyncio.run(go())
    assert 0.18 <= elapsed < 0.6


# ---------------------------------------------------------------- review follow-ups
def _robots_client(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        return routes[req.url.host]

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_robots_rfc9309_wildcards_and_precedence():
    txt = "User-agent: *\nAllow: /\nDisallow: /private\nDisallow: /*checkout\nAllow: /private/ok$\n"

    async def go():
        async with _robots_client({"s.test": httpx.Response(200, text=txt)}) as c:
            rc = RobotsCache(c)
            return [
                (await rc.check(f"https://s.test{p}")).allowed
                for p in ("/celulares", "/private", "/x/checkout", "/private/ok")
            ]

    assert asyncio.run(go()) == [True, False, False, True]


def test_robots_status_semantics(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("cca_report.net.asyncio.sleep", no_sleep)

    async def go():
        async with _robots_client(
            {
                "nf.test": httpx.Response(404),
                "err.test": httpx.Response(503),
                "deny.test": httpx.Response(403),
            }
        ) as c:
            rc = RobotsCache(c)
            return (
                await rc.check("https://nf.test/a"),
                await rc.check("https://err.test/a"),
                await rc.check("https://deny.test/a"),
            )

    nf, err, deny = asyncio.run(go())
    assert nf.allowed  # 4xx = unavailable -> allowed
    assert not err.allowed and not err.access_denied  # 5xx -> complete disallow
    assert not deny.allowed and deny.access_denied  # 403 -> access control


def test_classify_other_4xx_is_error():
    st, _ = classify(
        robots_allowed=True,
        http_status=200,
        static_chars=5000,
        browser_status=410,
        rendered_chars=6000,
        blocked_marker=None,
    )
    assert st is AccessStatus.ERROR


def test_block_markers_strong_and_weak():
    from cca_report.capture.browser import find_block_marker

    long_text = "x" * 5000
    assert find_block_marker(long_text, "<script src='recaptcha'>") is None  # weak, rich page
    assert find_block_marker("short", "<div>captcha</div>") == "captcha"
    assert find_block_marker(long_text, "captcha ... challenge-platform") == "challenge-platform"


def test_product_pages_not_proposed_as_brand_listing():
    r = next(x for x in Project().retailers if x.slug == "pa_panafoto")
    raw = [
        {"href": "/celulares", "text": "Celulares"},
        {"href": "/celulares/samsung-galaxy-s26-256gb/p", "text": "Galaxy S26"},
        {"href": "/celulares/samsung", "text": "Samsung"},
    ]
    _, brands = rank_links(raw, "https://www.panafoto.com/", "https://www.panafoto.com", r)
    assert [c.url for c in brands[Brand.SAMSUNG]] == ["https://www.panafoto.com/celulares/samsung"]


def test_domain_lock_serialises_same_domain_only():
    t = DomainThrottle(min_delay=0.0)
    events: list[str] = []

    async def worker(url: str, tag: str):
        async with t.lock(url):
            events.append(f"{tag}+")
            await asyncio.sleep(0.05)
            events.append(f"{tag}-")

    async def go():
        await asyncio.gather(
            worker("https://a.test/1", "a1"),
            worker("https://www.a.test/2", "a2"),
            worker("https://b.test/1", "b1"),
        )

    asyncio.run(go())
    a = [e for e in events if e.startswith("a")]
    assert a in (["a1+", "a1-", "a2+", "a2-"], ["a2+", "a2-", "a1+", "a1-"])
    assert events.index("b1+") < events.index("a1-")  # other domain ran concurrently


def test_prober_isolates_failing_retailer(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    from cca_report.access import Prober
    from cca_report.config import Settings

    @asynccontextmanager
    async def fake_launch(headless=True):
        yield object()

    monkeypatch.setattr("cca_report.access.launch", fake_launch)
    p = Project()
    rs = [x for x in p.retailers if x.slug in ("cr_monge", "py_bristol")]
    prober = Prober(p, Settings(), tmp_path)

    async def fn(r):
        if r.slug == "cr_monge":
            raise RuntimeError("boom")
        return ["fine"]

    out = asyncio.run(prober.run(rs, fn, lambda r, e: [f"error:{r.slug}"]))
    assert out == [["error:cr_monge"], ["fine"]]


def test_empty_proxy_env_is_none(monkeypatch):
    from cca_report.config import Settings
    from cca_report.models import CountryCode

    monkeypatch.setenv("HTTPS_PROXY_PA", "")
    assert Settings().proxy_for(CountryCode.PA) is None
