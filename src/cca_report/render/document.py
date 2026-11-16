"""Report document model and builder: data groups -> ordered, paginated slides (spec §3.7)."""

from __future__ import annotations

import logging
import math
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import Project
from ..models import BRAND_ORDER, Brand, CaptureMode, CountryCode, GroupBy, Product
from ..normalize import clean_model_name, duplicate_columns, product_flags, promo_type_ok, row_texts
from . import placeholders

log = logging.getLogger(__name__)

SlideKind = Literal["cover", "divider", "insights", "detail", "branding", "overview"]
MAX_PER_PAGE = 6


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- input data
class InsightsIn(_M):
    retailers: list[str]
    cells: dict[Brand, dict[str, str]] = Field(default_factory=dict)
    bullets: dict[Brand, list[str]] = Field(default_factory=dict)
    subtitle: str = ""


class CountryIn(_M):
    insights: InsightsIn | None = None
    retailer_order: list[str] = Field(default_factory=list)


class BannerIn(_M):
    label: str = ""
    screenshot: str | None = None


class GroupIn(_M):
    """One retailer x brand block (or a branding / campaign-overview block)."""

    kind: Literal["detail", "branding", "overview"] = "detail"
    country: CountryCode
    retailer: str
    brand: Brand | None = None
    campaign: str = ""  # overview: shown instead of the brand
    promo_types: list[str] = Field(default_factory=list)
    capture: CaptureMode = CaptureMode.CARDS
    row_format: str = ""
    caption: str = ""
    listing_screenshot: str | None = None
    banners: list[BannerIn] = Field(default_factory=list)
    products: list[dict[str, Any]] = Field(default_factory=list)


class ReportData(_M):
    week: str
    cutoff_date: date
    countries: dict[CountryCode, CountryIn] = Field(default_factory=dict)
    groups: list[GroupIn] = Field(default_factory=list)


# --------------------------------------------------------------------------- slides
class Column(_M):
    model: str
    row2: list[str]
    row3: str
    screenshot: str | None = None
    flags: list[str] = Field(default_factory=list)


class InsightsTable(_M):
    title: str
    subtitle: str = ""
    retailers: list[str]
    rows: list[tuple[str, list[str]]]  # (brand, cell per retailer)
    bullets: list[tuple[str, list[str]]] = Field(default_factory=list)


class Slide(_M):
    id: str
    kind: SlideKind
    group_id: str = ""
    country: CountryCode | None = None
    country_name: str = ""
    retailer: str = ""
    heading_right: str = ""
    promo_line: str = ""
    promo_types: list[str] = Field(default_factory=list)
    capture: CaptureMode = CaptureMode.CARDS
    listing_image: str | None = None
    columns: list[Column] = Field(default_factory=list)
    banners: list[BannerIn] = Field(default_factory=list)
    caption: str = ""
    insights: InsightsTable | None = None
    page_label: str = ""  # "1/2" when a block is split
    title_lines: list[str] = Field(default_factory=list)
    date_label: str = ""


class BuildResult(_M):
    slides: list[Slide]
    flags: list[str]  # human-readable review notes


# --------------------------------------------------------------------------- builder
def _slug(*parts: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")


def cutoff_label(d: date) -> str:
    n = d.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{d:%B} {n}{suffix}, {d.year}"


class DocumentBuilder:
    def __init__(self, project: Project, data: ReportData, assets_dir: Path) -> None:
        self.project = project
        self.data = data
        self.assets_dir = assets_dir  # where placeholder screenshots are written
        self.per_page_override: dict[str, int] = {}

    def _per_page(self, g: GroupIn, gid: str) -> int:
        base = min(self.project.country(g.country).products_per_page, MAX_PER_PAGE)
        return self.per_page_override.get(gid, base)

    def _retailer_rank(self, code: CountryCode) -> dict[str, int]:
        cin = self.data.countries.get(code)
        configured: list[str] = []
        if cin:
            configured = cin.retailer_order or (cin.insights.retailers if cin.insights else [])
        rank: dict[str, int] = {}
        # copy, never extend the input lists (build() runs again on re-pagination)
        for name in [*configured, *(g.retailer for g in self.data.groups if g.country == code)]:
            rank.setdefault(name.casefold(), len(rank))  # first position wins
        return rank

    def _ordered_groups(self, code: CountryCode) -> list[tuple[str, GroupIn]]:
        cfg = self.project.country(code)
        rr = self._retailer_rank(code)
        brand_rank = {b: i for i, b in enumerate(BRAND_ORDER)}
        items = [(i, g) for i, g in enumerate(self.data.groups) if g.country == code]

        def key(item: tuple[int, GroupIn]) -> tuple:
            i, g = item
            b = -1 if g.kind == "overview" else brand_rank.get(g.brand, 9) if g.brand else 9
            r = rr.get(g.retailer.casefold(), 99)
            return (b, r, i) if cfg.group_by == GroupBy.BRAND else (r, b, i)

        return [(f"g{i:03d}", g) for i, g in sorted(items, key=key)]

    def build(self) -> BuildResult:
        rep = self.project.report
        slides: list[Slide] = [
            Slide(
                id="cover",
                kind="cover",
                title_lines=rep.title_lines,
                date_label=cutoff_label(self.data.cutoff_date),
            )
        ]
        notes: list[str] = []
        present = {g.country for g in self.data.groups} | set(self.data.countries)
        for c in rep.countries:
            if c.code not in present:
                continue
            slides.append(Slide(id=f"div-{c.code}", kind="divider", country=c.code, country_name=c.name))
            cin = self.data.countries.get(c.code)
            if cin and cin.insights:
                slides.append(self._insights_slide(c.code, c.name, cin.insights))
            for gid, g in self._ordered_groups(c.code):
                new, gnotes = self._group_slides(gid, g, c.name)
                slides += new
                notes += gnotes
        return BuildResult(slides=slides, flags=notes)

    def _insights_slide(self, code: CountryCode, name: str, ins: InsightsIn) -> Slide:
        rows = []
        for b in BRAND_ORDER:
            cells = ins.cells.get(b, {})
            lookup = {k.casefold(): v for k, v in cells.items()}
            rows.append(
                (
                    b.value,
                    [lookup.get(r.casefold(), "") or "No information available." for r in ins.retailers],
                )
            )
        bullets = [(b.value, ins.bullets[b]) for b in BRAND_ORDER if ins.bullets.get(b)]
        table = InsightsTable(
            title=f"{name} – Comparison of Promotions and Financing Options by Brand and Retailer",
            subtitle=ins.subtitle,
            retailers=ins.retailers,
            rows=rows,
            bullets=bullets,
        )
        return Slide(id=f"ins-{code}", kind="insights", country=code, country_name=name, insights=table)

    def _group_slides(self, gid: str, g: GroupIn, country_name: str) -> tuple[list[Slide], list[str]]:
        label = f"{g.country} · {g.retailer} · {g.brand.value if g.brand else g.campaign}"
        notes: list[str] = []
        promo_line = ", ".join(g.promo_types)
        common = dict(
            kind=g.kind,
            group_id=gid,
            country=g.country,
            country_name=country_name,
            retailer=g.retailer,
            heading_right=g.campaign if g.kind == "overview" else (g.brand.value if g.brand else ""),
            promo_line="" if g.kind == "overview" else promo_line,
            promo_types=[] if g.kind == "overview" else g.promo_types,
            caption=g.caption,
        )
        if g.kind != "overview" and (not g.promo_types or not all(promo_type_ok(t) for t in g.promo_types)):
            notes.append(f"{label}: promotion type is empty or a placeholder")
        if g.kind in ("branding", "overview"):
            banners = [
                BannerIn(
                    label=b.label,
                    screenshot=b.screenshot or placeholders.make(self.assets_dir, b.label or g.retailer, "banner"),
                )
                for i, b in enumerate(g.banners or [BannerIn(label=g.campaign or g.retailer)])
            ]
            return [Slide(id=gid, banners=banners, **common)], notes

        columns: list[Column] = []
        for raw in g.products:
            raw = dict(raw)  # never mutate the input (the builder runs again on re-pagination)
            screenshot = raw.pop("screenshot", None)
            row2_override = raw.pop("row2_override", None)  # manual text from review.xlsx
            row3_override = raw.pop("row3_override", None)
            # the slide owns country/retailer/brand: a product cannot move itself elsewhere
            fixed = {"country": g.country, "retailer": g.retailer, "brand": g.brand}
            conflicts = [
                k for k, v in fixed.items() if k in raw and str(raw[k]) != str(v.value if hasattr(v, "value") else v)
            ]
            p = Product.model_validate({"currency": self.project.country(g.country).currency, **raw, **fixed})
            p.model_name = clean_model_name(p.model_name)
            texts = row_texts(p, g.row_format)
            row2 = [t.strip() for t in str(row2_override).split(" | ")] if row2_override else texts.row2
            row3 = str(row3_override).strip() if row3_override else texts.row3
            flags = product_flags(p, g.row_format) + [f"group_conflict:{k}" for k in conflicts]
            if g.capture == CaptureMode.CARDS and not screenshot:
                screenshot = placeholders.make(self.assets_dir, p.model_name, "card")
            columns.append(Column(model=p.model_name, row2=row2, row3=row3, screenshot=screenshot, flags=flags))
        for i in duplicate_columns((c.model, tuple(c.row2), c.row3) for c in columns):
            columns[i].flags.append("duplicate_column")
        for c in columns:
            if c.flags:
                notes.append(f"{label} · {c.model}: {', '.join(c.flags)}")
        if not columns:
            notes.append(f"{label}: no products -> no slide")
            return [], notes

        listing = None
        if g.capture == CaptureMode.LISTING:
            listing = g.listing_screenshot or placeholders.make(
                self.assets_dir,
                f"{g.retailer} · {g.brand.value if g.brand else ''} listing",
                "listing",
            )
        per = self._per_page(g, gid)
        pages = math.ceil(len(columns) / per)
        # balance pages: 7 products at 6/page -> 4 + 3 instead of 6 + 1
        size = math.ceil(len(columns) / pages)
        out = []
        for n in range(pages):
            chunk = columns[n * size : (n + 1) * size]
            out.append(
                Slide(
                    id=f"{gid}-{n + 1}",
                    capture=g.capture,
                    listing_image=listing,
                    columns=chunk,
                    page_label=f"{n + 1}/{pages}" if pages > 1 else "",
                    **common,
                )
            )
        return out, notes
