"""Domain models (Pydantic v2) for configuration, extracted data and report pages."""

from __future__ import annotations

import contextlib
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# --------------------------------------------------------------------------- enums
class CountryCode(StrEnum):
    PA = "PA"
    CR = "CR"
    EC = "EC"
    PY = "PY"
    UY = "UY"


class Brand(StrEnum):
    SAMSUNG = "Samsung"
    APPLE = "Apple"
    HONOR = "Honor"
    MOTOROLA = "Motorola"


BRAND_ORDER: tuple[Brand, ...] = (Brand.SAMSUNG, Brand.APPLE, Brand.HONOR, Brand.MOTOROLA)


class ExtractMode(StrEnum):
    DOM = "dom"
    VISION = "vision"
    SHOPIFY = "shopify"
    ML_API = "ml_api"
    MANUAL = "manual"


class CaptureMode(StrEnum):
    CARDS = "cards"
    LISTING = "listing"
    BANNER = "banner"


class RetailerType(StrEnum):
    OPERATOR = "operator"
    RETAILER = "retailer"
    RESELLER = "reseller"
    DISTRIBUTOR = "distributor"
    MARKETPLACE = "marketplace"
    BRAND = "brand"


class GroupBy(StrEnum):
    BRAND = "brand"
    RETAILER = "retailer"


class AccessStatus(StrEnum):
    OK = "ok"
    JS_HEAVY = "js_heavy"
    BLOCKED = "blocked"
    ROBOTS = "robots"
    ERROR = "error"
    MANUAL = "manual"  # configured as manual-only; not probed with a browser


# --------------------------------------------------------------------------- config
class Selectors(_Strict):
    product_card: str = ""
    name: str = ""
    price: str = ""
    old_price: str = ""
    installments: str = ""
    badge: str = ""


class BrandConfig(_Strict):
    urls: list[HttpUrl] = Field(default_factory=list)
    mode: ExtractMode = ExtractMode.DOM
    capture: CaptureMode = CaptureMode.LISTING
    max_products: int = Field(default=6, ge=1, le=12)
    filter_by_name: bool = False  # page lists every brand: keep only cards whose name matches
    selectors: Selectors = Field(default_factory=Selectors)
    enabled: bool = True


class BrandingConfig(_Strict):
    brand: Brand = Brand.SAMSUNG
    urls: list[HttpUrl]
    capture: CaptureMode = CaptureMode.BANNER


class DiscoverConfig(_Strict):
    """Hints for Phase 0 discovery of category/brand URLs."""

    start_urls: list[HttpUrl] = Field(default_factory=list)  # defaults to base_url
    menu_texts: list[str] = Field(default_factory=lambda: ["Celulares"])
    keywords: list[str] = Field(
        default_factory=lambda: ["celular", "smartphone", "telefon", "equipo", "movil", "phone"]
    )


class RetailerConfig(_Strict):
    slug: str = ""  # filled from the YAML filename
    country: CountryCode
    retailer_name: str
    type: RetailerType
    base_url: HttpUrl
    currency: str
    locale: str
    row_format: str
    pages_per_brand_max: int = Field(default=1, ge=1, le=4)
    enabled: bool = True
    manual_only: bool = False  # robots.txt or policy forbids automated access
    preferred_installments: int | None = None  # several plans on a card (1x..18x): show this one
    render_wait_seconds: int = Field(default=25, ge=0, le=120)  # lazy catalogues: max wait for prices
    promo_types: list[str] = Field(default_factory=list)  # fixed slide promo type(s) instead of inferred
    campaign_overview: bool = False  # manual/<PAIS>_<retailer>_Campaign/ -> 'Campaign overview' slide
    notes: str = ""
    brands: dict[Brand, BrandConfig] = Field(default_factory=dict)
    branding: list[BrandingConfig] = Field(default_factory=list)
    pre_actions: list[dict[str, str | bool | int]] = Field(default_factory=list)
    discover: DiscoverConfig | None = None

    @property
    def all_urls(self) -> list[str]:
        urls: list[str] = [str(self.base_url)]
        for cfg in self.brands.values():
            urls += [str(u) for u in cfg.urls]
        for b in self.branding:
            urls += [str(u) for u in b.urls]
        return list(dict.fromkeys(urls))


class CountryConfig(_Strict):
    code: CountryCode
    name: str
    currency: str
    locale: str
    timezone: str
    group_by: GroupBy
    products_per_page: int = Field(default=6, ge=1, le=6)
    event_caption: str = ""
    retailer_order: list[str] = Field(default_factory=list)  # order of retailers in the report
    model_with_storage: bool = False  # "Galaxy A37 256 GB" (PY, UY) vs "Galaxy A37" (PA, CR, EC)
    insights_bullets: bool = False  # 3-5 "Key insights" bullets per brand under the table (PY, UY)


class SelectionConfig(_Strict):
    """Which 3-6 products to keep per retailer x brand (spec §4, configurable)."""

    min_products: int = Field(default=3, ge=1, le=6)
    flagship_patterns: list[str] = Field(
        default_factory=lambda: [
            r"galaxy\s+(s\d{2}|z\s*(fold|flip))",
            r"\bfold\b|\bflip\b",
            r"iphone\s+\d+\s*pro",
            r"honor\s+(magic|600)",
            r"\b(edge|razr)\b",
        ]
    )
    midrange_patterns: list[str] = Field(
        default_factory=lambda: [r"galaxy\s+a[1-5]\d", r"iphone\s+\d+(e)?\b", r"honor\s+\d{3}", r"moto\s*g"]
    )
    # a product must look like a phone (brand pages also list appliances, watches, pencils...)
    phone_patterns: list[str] = Field(
        default_factory=lambda: [
            r"celular|smartphone|tel[eé]fono|\biphone\b|\bgalaxy\b|\bmoto\b|\brazr\b|\bedge\s?\d",
            r"honor\s+(magic|x\d|\d{2,3}|play|200|400|600)",
            r"galaxy\s+(s|z|a)\d",
        ]
    )
    exclude_patterns: list[str] = Field(
        default_factory=lambda: [
            r"\b(tab|tablet|ipad|watch|buds|airpods|funda|case|cargador|charger|cable|protector|"
            r"audifonos?|auriculares?|smartwatch|band|lavarropas|lavadora|refrigerador|televisor|tv)(?![a-z])"
        ]
    )


class MailConfig(_Strict):
    """Weekly e-mail (sent through the local Outlook profile, no stored passwords)."""

    enabled: bool = True
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    alert_to: list[str] = Field(default_factory=list)  # failed runs; empty = `to`
    account: str = ""  # Outlook account to send from; empty = default account
    send_at: str = "10:00"  # HH:MM in ReportConfig.timezone (weekly waits until then)
    include_run_status: bool = True  # internal coverage section; turn off for stakeholders
    max_attachment_mb: float = Field(default=18.0, gt=0)
    subject: str = "CCA Competitor's Promotions Report – Week {week_number} (cut-off {cutoff})"

    @field_validator("send_at")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("send_at must look like 10:00")
        return v


def friday_of(week: str) -> date:
    y, w = week.split("-W")
    return date.fromisocalendar(int(y), int(w), 5)


class ReportConfig(_Strict):
    week: str  # ISO week, e.g. 2026-W45, or "auto" (current week in `timezone`)
    cutoff_date: date  # or "auto" (Friday of `week`)
    timezone: str = "America/Panama"
    title_lines: list[str] = Field(default_factory=lambda: ["Weekly Competitor's", "Promotions Report", "MX Division"])
    insights_provider: Literal["codex", "rules"] = "codex"
    codex_timeout_seconds: int = Field(default=420, ge=30, le=1800)
    min_delay_seconds: float = Field(default=3.0, ge=3.0)
    page_timeout_seconds: int = 45
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    countries: list[CountryConfig]
    cutoff_auto: bool = False  # set by the loader, not in YAML

    @model_validator(mode="before")
    @classmethod
    def _auto(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if str(data.get("week", "")).strip().lower() == "auto":
            from zoneinfo import ZoneInfo

            y, w, _ = datetime.now(ZoneInfo(data.get("timezone") or "America/Panama")).date().isocalendar()
            data["week"] = f"{y}-W{w:02d}"
        if str(data.get("cutoff_date", "auto")).strip().lower() == "auto":
            data["cutoff_auto"] = True
            with contextlib.suppress(ValueError):  # the week validator reports the real problem
                data["cutoff_date"] = friday_of(str(data.get("week")))
        return data

    def cutoff_for(self, week: str) -> date:
        """Cut-off date of any week: the configured one for `week`, else that week's Friday."""
        return self.cutoff_date if week == self.week and not self.cutoff_auto else friday_of(week)

    @field_validator("week")
    @classmethod
    def _iso_week(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"\d{4}-W\d{2}", v):
            raise ValueError("week must look like 2026-W45")
        return v

    @property
    def week_number(self) -> int:
        return int(self.week.split("-W")[1])

    @property
    def cutoff_label(self) -> str:
        d = self.cutoff_date.day
        suffix = "th" if 11 <= d % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
        return f"{self.cutoff_date:%B} {d}{suffix}, {self.cutoff_date.year}"


# --------------------------------------------------------------------------- data
class Trace(_Strict):
    url: str
    captured_at: datetime
    screenshot: str | None = None
    html: str | None = None
    source: ExtractMode


class Product(_Strict):
    """One product card as extracted (schema shared by DOM, vision and APIs)."""

    country: CountryCode
    retailer: str
    brand: Brand
    model_name: str
    storage: str | None = None
    color: str | None = None
    condition: Literal["new", "open-box", "refurbished"] = "new"
    price_final: float | None = None
    price_label: str | None = None  # e.g. "Pre Paid", "From", "Payment in full"
    price_before: float | None = None
    discount_pct: float | None = None
    downpayment: float | None = None
    monthly_payment: float | None = None
    installments_n: int | None = None
    coupon_price: float | None = None
    bundle_gift: str | None = None
    promo_type: str | None = None
    badge_text: str | None = None
    currency: str
    secondary_currency: str | None = None  # e.g. UYU installments on Mercado Libre
    out_of_stock: bool = False
    trace: Trace | None = None
    flags: list[str] = Field(default_factory=list)
    include: bool = True
    order: int = 0

    @property
    def needs_review(self) -> bool:
        return bool(self.flags)


class AccessResult(_Strict):
    slug: str
    country: CountryCode
    retailer: str
    url: str
    status: AccessStatus
    robots_allowed: bool | None = None
    crawl_delay: float | None = None
    http_status: int | None = None
    browser_status: int | None = None
    static_text_chars: int | None = None
    rendered_text_chars: int | None = None
    brand_hits: dict[str, int] = Field(default_factory=dict)
    title: str = ""
    final_url: str = ""
    screenshot: str | None = None
    detail: str = ""
    checked_at: datetime


class LinkCandidate(_Strict):
    url: str
    text: str
    score: float
    brand: Brand | None = None


class Visit(_Strict):
    """Traceability for every page discovery loaded."""

    url: str
    final_url: str = ""
    visited_at: datetime
    http_status: int | None = None
    outcome: str = ""
    screenshot: str | None = None


class DiscoveryResult(_Strict):
    slug: str
    country: CountryCode
    retailer: str
    status: AccessStatus
    visits: list[Visit] = Field(default_factory=list)
    category_candidates: list[LinkCandidate] = Field(default_factory=list)
    brand_candidates: dict[Brand, list[LinkCandidate]] = Field(default_factory=dict)
    detail: str = ""

    @model_validator(mode="after")
    def _sort(self) -> DiscoveryResult:
        self.category_candidates.sort(key=lambda c: -c.score)
        for v in self.brand_candidates.values():
            v.sort(key=lambda c: -c.score)
        return self


# --------------------------------------------------------------------------- capture
class TextLine(_Strict):
    text: str
    strike: bool = False
    bold: bool = False
    size: float = 0.0


class RawCard(_Strict):
    """A product card as seen in the browser (input of the offline DOM extractor)."""

    index: int
    text: str
    lines: list[TextLine] = Field(default_factory=list)
    struck: list[str] = Field(default_factory=list)  # struck-through texts
    href: str = ""
    img: str = ""
    name: str = ""  # text of selectors.name when configured
    screenshot: str | None = None
    product_json: dict | None = None  # Shopify product JSON matched by handle


class CapturedPage(_Strict):
    url: str
    final_url: str = ""
    captured_at: datetime
    status: AccessStatus
    detail: str = ""
    how: str = ""  # selector | heuristic
    html: str | None = None
    screenshot: str | None = None
    cards: list[RawCard] = Field(default_factory=list)
    shopify: list[dict] | None = None  # products.json payload when available


class CapturedGroup(_Strict):
    country: CountryCode
    retailer_slug: str
    retailer: str
    brand: Brand | None = None
    kind: Literal["detail", "branding", "overview"] = "detail"
    mode: ExtractMode
    capture: CaptureMode
    row_format: str
    pages: list[CapturedPage] = Field(default_factory=list)
    manual_dir: str | None = None
    manual_images: list[str] = Field(default_factory=list)


class CaptureRun(_Strict):
    week: str
    started_at: datetime
    finished_at: datetime | None = None
    groups: list[CapturedGroup] = Field(default_factory=list)
