"""`extract` step (offline): capture.json -> data.json (products, slides, statuses)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from ..brands import BRAND_PATTERNS
from ..capture.runner import rel
from ..config import PROJECT_ROOT, Project
from ..models import (
    AccessStatus,
    Brand,
    CapturedGroup,
    CaptureMode,
    CaptureRun,
    ExtractMode,
    RawCard,
)
from ..render.document import BannerIn, CountryIn, GroupIn, ReportData
from ..select import Candidate, choose, is_phone, model_key
from . import dom, shopify

log = logging.getLogger(__name__)


class GroupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: str
    retailer: str
    brand: str
    status: str  # ok | empty | blocked | robots | error | manual | manual_pending
    products: int = 0
    detail: str = ""


class RunData(ReportData):
    status: list[GroupStatus] = Field(default_factory=list)


def _brand_match(brand: Brand, card: RawCard) -> bool:
    head = " ".join([card.name, *[ln.text for ln in card.lines[:4]]]) if card.lines else card.text[:200]
    return bool(BRAND_PATTERNS[brand].search(head or card.text[:200]))


MANUAL_PRICES = "precios.csv"
MANUAL_PRICE_COLS = {
    "precio_final": "price_final",
    "precio_antes": "price_before",
    "entrada": "downpayment",
    "cuota_mensual": "monthly_payment",
    "cuotas": "installments_n",
    "regalo": "bundle_gift",
    "etiqueta": "price_label",
    "promo": "promo_type",  # e.g. "Special discount"; empty = inferred from the prices
}
_AMOUNT_TOKEN = re.compile(r"(?:[A-Za-z]{0,3}\s?[$₡₲]?\s?)\d[\d.,\s]*")  # 'USD $1.299,00', '₡650.000'
_INT_TOKEN = re.compile(r"\d{1,3}")
MANUAL_PRICES_HEADER = "archivo;" + ";".join(MANUAL_PRICE_COLS)


class ManualPricesError(ValueError):
    pass


def read_manual_prices(folder: Path) -> dict[str, dict[str, Any]]:
    """Optional ``precios.csv`` next to the manual screenshots: one row per image file
    (``archivo`` = file name, with or without extension). Excel's ';' or ',' both work.
    Returns {file stem (lower case): product fields}."""
    import csv
    import io

    from ..normalize import parse_amount

    path = folder / MANUAL_PRICES
    if not path.exists():
        return {}
    raw = path.read_bytes()
    if raw.startswith(b"<## NASCA DRM"):
        raise ManualPricesError(
            f"{path} está cifrado por el DRM corporativo (se guardó con Excel): edítalo con el Bloc de notas"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")  # Excel "CSV (delimitado por comas)" on Spanish Windows
    head = text.splitlines()[0] if text.strip() else ""
    reader = csv.DictReader(io.StringIO(text), delimiter=";" if head.count(";") >= head.count(",") else ",")
    reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames or []]
    if "archivo" not in reader.fieldnames:
        raise ManualPricesError(f"{path.name}: falta la columna 'archivo' (cabecera: {MANUAL_PRICES_HEADER})")
    out: dict[str, dict[str, Any]] = {}
    for n, row in enumerate(reader, start=2):
        if None in row and any((v or "").strip() for v in row[None]):
            raise ManualPricesError(
                f"{path.name} fila {n}: más valores que columnas (¿un decimal con coma? usa ';' como separador)"
            )
        name = (row.get("archivo") or "").strip()
        if not name:
            continue
        vals: dict[str, Any] = {}
        for col, field in MANUAL_PRICE_COLS.items():
            v = (row.get(col) or "").strip()
            if not v:
                continue
            if field in ("bundle_gift", "price_label", "promo_type"):
                vals[field] = v
                continue
            whole = _INT_TOKEN if field == "installments_n" else _AMOUNT_TOKEN
            num = parse_amount(v) if whole.fullmatch(v) else None
            if num is None or num < 0:
                raise ManualPricesError(f"{path.name} fila {n}, columna '{col}': valor inválido {v!r}")
            vals[field] = int(num) if field == "installments_n" else num
        before, final = vals.get("price_before"), vals.get("price_final")
        if before and final and before > final:
            vals["discount_pct"] = round((before - final) / before * 100)
        out[Path(name).stem.strip().lower()] = vals
    return out


def infer_promo_types(products: list[dict[str, Any]]) -> list[str]:
    types: list[str] = []
    if any(p.get("monthly_payment") is not None for p in products):
        types.append("Monthly Payment")
    if any(p.get("price_before") is not None or p.get("discount_pct") for p in products):
        types.append("Price Discount")
    if any(p.get("bundle_gift") for p in products):
        types.append("Bundle promotion")
    return types


def recompose_listings(data: ReportData, run_dir: Path) -> None:
    """After review.xlsx edits (include/order), rebuild each listing mosaic from the
    remaining products so the image always matches the table columns."""
    for g in data.groups:
        if g.kind != "detail" or g.capture != CaptureMode.LISTING:
            continue
        shots = [PROJECT_ROOT / p["screenshot"] for p in g.products if p.get("screenshot")]
        if not shots or any(not s.exists() for s in shots):
            continue
        tag = "_".join(Path(p).stem for p in [str(s) for s in shots])
        import hashlib

        digest = hashlib.sha1(tag.encode()).hexdigest()[:10]
        out = (
            run_dir / "screenshots" / "listings" / f"{g.country.value}_{_slug(g.retailer)}_{g.brand.value}_{digest}.png"
        )
        made = compose_listing(shots, out)
        if made:
            g.listing_screenshot = rel(made)


def _slug(s: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def compose_listing(images: list[Path], out: Path, gap: int = 24) -> Path | None:
    """Real card screenshots side by side (the listing view of the selected products)."""
    imgs = [Image.open(p).convert("RGB") for p in images if p.exists()]
    if not imgs:
        return None
    h = max(i.height for i in imgs)
    imgs = [i.resize((round(i.width * h / i.height), h)) if i.height != h else i for i in imgs]
    canvas = Image.new("RGB", (sum(i.width for i in imgs) + gap * (len(imgs) - 1), h), "white")
    x = 0
    for i in imgs:
        canvas.paste(i, (x, 0))
        x += i.width + gap
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, optimize=True)
    return out


class Extractor:
    def __init__(self, project: Project, run_dir: Path) -> None:
        self.project = project
        self.run_dir = run_dir
        self.cfg_by_slug = {r.slug: r for r in project.retailers}

    def run(self, cap: CaptureRun) -> RunData:
        rep = self.project.report
        data = RunData(week=cap.week, cutoff_date=rep.cutoff_for(cap.week))
        for c in rep.countries:
            if any(g.country == c.code for g in cap.groups):
                data.countries[c.code] = CountryIn(retailer_order=list(c.retailer_order))
        for g in cap.groups:
            group, status = self._group(g)
            data.status.append(status)
            if group is not None:
                data.groups.append(group)
        return data

    # ------------------------------------------------------------------
    def _group(self, g: CapturedGroup) -> tuple[GroupIn | None, GroupStatus]:
        brand = g.brand.value if g.brand else ""
        st = GroupStatus(country=g.country.value, retailer=g.retailer, brand=brand, status="ok")
        country = self.project.country(g.country)
        caption = country.event_caption
        if g.kind == "overview":
            return self._overview(g, st)
        if g.kind == "branding":
            banners = [
                BannerIn(label=_domain(p.final_url or p.url), screenshot=p.screenshot)
                for p in g.pages
                if p.screenshot and p.status == AccessStatus.OK
            ]
            if not banners:
                st.status, st.detail = "error", "; ".join(p.detail for p in g.pages)
                return None, st
            st.products = len(banners)
            return GroupIn(
                kind="branding",
                country=g.country,
                retailer=g.retailer,
                brand=g.brand,
                promo_types=["Branding"],
                caption=caption,
                banners=banners,
            ), st

        ok_pages = [p for p in g.pages if p.status in (AccessStatus.OK, AccessStatus.JS_HEAVY)]
        if g.mode == ExtractMode.MANUAL or not ok_pages:
            return self._manual(g, st, caption, blocked=[p for p in g.pages if p not in ok_pages])

        r = self.cfg_by_slug[g.retailer_slug]
        bcfg = r.brands[g.brand]
        cands: list[Candidate] = []
        pos = 0
        for page in ok_pages:
            for card in page.cards:
                pos += 1
                if bcfg.filter_by_name and not _brand_match(g.brand, card):
                    continue
                parsed = dom.parse_card(card, g.brand, r.preferred_installments)
                if card.product_json:
                    parsed = shopify.merge_with_dom(shopify.parse_product(card.product_json, g.brand), parsed)
                source = ExtractMode.SHOPIFY if card.product_json else ExtractMode.DOM
                title = (card.name or (card.lines[0].text if card.lines else "")).split(" + ")[0]
                if not parsed.model_name or not is_phone(f"{parsed.model_name} {title}", self.project.report.selection):
                    continue
                prod = self._product(parsed, card, page, source, country.model_with_storage, g.row_format)
                cands.append(
                    Candidate(
                        key=model_key(
                            f"{parsed.model_name} {parsed.condition}",
                            parsed.storage if country.model_with_storage else None,
                        ),
                        name=prod["model_name"],
                        has_promo=bool(
                            parsed.price_before or parsed.discount_pct or parsed.monthly_payment or parsed.bundle_gift
                        ),
                        discount=parsed.discount_pct or 0.0,
                        position=pos,
                        out_of_stock=parsed.out_of_stock,
                        payload=prod,
                    )
                )
        picked = choose(cands, self.project.report.selection, bcfg.max_products)
        products = [c.payload for c in picked]
        if not products:
            st.status = "empty"
            st.detail = "; ".join(p.detail for p in ok_pages if p.detail) or "no products of this brand on the page"
            return None, st
        listing = None
        if g.capture == CaptureMode.LISTING:
            shots = [PROJECT_ROOT / p["screenshot"] for p in products if p.get("screenshot")]
            out = self.run_dir / "screenshots" / g.retailer_slug / f"listing_{brand}.png"
            made = compose_listing(shots, out)
            listing = rel(made) if made else None
        st.products = len(products)
        return GroupIn(
            country=g.country,
            retailer=g.retailer,
            brand=g.brand,
            promo_types=list(r.promo_types) or infer_promo_types(products),
            capture=g.capture,
            row_format=g.row_format,
            caption=caption,
            listing_screenshot=listing,
            products=products,
        ), st

    def _product(
        self, parsed: dom.Parsed, card: RawCard, page, source: ExtractMode, with_storage: bool, row_format: str
    ) -> dict[str, Any]:
        d = parsed.as_product_fields()
        name = d["model_name"]
        if (
            with_storage
            and d.get("storage")
            and d["storage"].replace(" ", "").lower() not in name.replace(" ", "").lower()
        ):
            name = f"{name} {d['storage']}"
        d["model_name"] = name
        if not d.get("price_label") and "from_price" in parsed.notes and row_format == "uy_cover":
            d["price_label"] = "From"
        # keep numeric zeros (0.0 == False!): only drop None / "" / the bool False
        d = {k: v for k, v in d.items() if not (v is None or v == "" or v is False) or k == "out_of_stock"}
        if "renewal_term" in parsed.notes:
            d["badge_text"] = d.get("badge_text") or f"Renewal up to {d.get('installments_n')} months"
        d["screenshot"] = card.screenshot
        d["trace"] = {
            "url": card.href or page.final_url or page.url,
            "captured_at": page.captured_at.isoformat(),
            "screenshot": card.screenshot,
            "html": page.html,
            "source": source.value,
        }
        return d

    def _overview(self, g: CapturedGroup, st: GroupStatus) -> tuple[GroupIn | None, GroupStatus]:
        st.brand = "(campaign)"
        if not g.manual_images:
            st.status, st.detail = "manual_pending", f"optional: banner + caption.txt in {g.manual_dir}"
            return None, st
        folder = PROJECT_ROOT / (g.manual_dir or "")
        cap = folder / "caption.txt"
        lines = cap.read_text(encoding="utf-8").splitlines() if cap.exists() else []
        campaign = (lines[0].strip() if lines else "") or "Campaign overview"
        source = next((t.strip() for t in lines[1:] if t.strip().startswith("http")), "")
        caption = " ".join(t.strip() for t in lines[1:] if t.strip() and not t.strip().startswith("http"))
        image = PROJECT_ROOT / g.manual_images[0]
        st.products = 1
        st.detail = (
            f"campaign '{campaign}' · image {g.manual_images[0]} ({_mtime(image)}) · "
            f"source {source or 'n/a (add the URL as a line of caption.txt)'}"
        )
        return GroupIn(
            kind="overview",
            country=g.country,
            retailer=g.retailer,
            campaign=campaign,
            caption=caption,
            banners=[BannerIn(label="", screenshot=g.manual_images[0])],
        ), st

    def _manual(self, g: CapturedGroup, st: GroupStatus, caption: str, blocked) -> tuple[GroupIn | None, GroupStatus]:
        why = "; ".join(f"{p.status.value}: {p.detail}" for p in blocked) or "manual mode"
        images = [PROJECT_ROOT / i for i in g.manual_images]
        if not images:
            st.status = (
                "manual_pending"
                if g.mode == ExtractMode.MANUAL
                else (blocked[0].status.value if blocked else "manual_pending")
            )
            st.detail = f"{why} -> drop screenshots in {g.manual_dir}"
            return None, st
        listing = next((i for i in images if i.stem.lower().startswith("listing")), None)
        cards = [i for i in images if i != listing]
        try:
            prices = read_manual_prices(PROJECT_ROOT / g.manual_dir) if g.manual_dir else {}
        except ManualPricesError as exc:
            log.warning("%s", exc)
            prices, price_error = {}, str(exc)
        else:
            price_error = ""
        products = [
            {
                "model_name": i.stem.replace("_", " ").strip(),
                "screenshot": rel(i),
                **prices.get(i.stem.strip().lower(), {}),
                "trace": {"url": "manual", "captured_at": _mtime(i), "screenshot": rel(i), "source": "manual"},
            }
            for i in cards
        ]
        if not products and listing:
            products = [
                {
                    "model_name": f"(model {n})",
                    "trace": {
                        "url": "manual",
                        "captured_at": _mtime(listing),
                        "screenshot": rel(listing),
                        "source": "manual",
                    },
                }
                for n in range(1, 4)
            ]
        st.status, st.products = ("price_error" if price_error else "manual"), len(products)
        st.detail = f"{price_error}; " if price_error else ""
        priced = sum(1 for prod in products if prod.get("price_final") is not None)
        retailer_cfg = self.cfg_by_slug.get(g.retailer_slug)
        cfg_types = retailer_cfg.promo_types if retailer_cfg else []
        st.detail = (st.detail or "") + (
            f"{len(images)} image(s) from {g.manual_dir}; "
            + (
                f"{priced}/{len(products)} priced from {MANUAL_PRICES}"
                if priced
                else "prices to be filled in review.xlsx"
            )
        )
        return GroupIn(
            country=g.country,
            retailer=g.retailer,
            brand=g.brand,
            promo_types=list(dict.fromkeys(pr["promo_type"] for pr in products if pr.get("promo_type")))
            or (list(cfg_types) if priced and cfg_types else infer_promo_types(products)),
            capture=g.capture,
            row_format=g.row_format,
            caption=caption,
            listing_screenshot=rel(listing) if listing else None,
            products=products,
        ), st


def _domain(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or url).removeprefix("www.")


def _mtime(p: Path) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat()
