"""review.xlsx: human review of every product before the PDF (spec §6 "Revisión humana").

`export_review` writes one sheet per country (one row per product), a Slides sheet
(promotion type, caption, include), an Insights sheet (editable texts) and a Status sheet.
`import_review` reads it back: **the workbook is the source of truth for `build`**.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .config import Project
from .models import BRAND_ORDER, Product
from .normalize import product_flags, row_texts
from .render.document import BannerIn, CountryIn, GroupIn, InsightsIn, ReportData

PRODUCT_COLS = [
    "include",
    "order",
    "slide",
    "retailer",
    "brand",
    "model_name",
    "storage",
    "color",
    "condition",
    "price_label",
    "price_final",
    "price_before",
    "discount_pct",
    "downpayment",
    "monthly_payment",
    "installments_n",
    "coupon_price",
    "secondary_currency",
    "bundle_gift",
    "badge_text",
    "out_of_stock",
    "row2_preview",
    "row3_preview",
    "row2_override",
    "row3_override",
    "flags",
    "url",
    "screenshot",
    "html",
    "captured_at",
    "source",
]
NUMERIC = {"price_final", "price_before", "discount_pct", "downpayment", "monthly_payment", "coupon_price"}
INTEGER = {"installments_n", "order"}
FIELD_COLS = [c for c in PRODUCT_COLS[5:21]]
SLIDE_COLS = [
    "include",
    "slide",
    "country",
    "retailer",
    "brand",
    "kind",
    "promo_types",
    "capture",
    "row_format",
    "caption",
    "listing_screenshot",
    "banners",
]
INSIGHT_COLS = ["country", "brand", "retailer", "text"]
READONLY = {
    "html",
    "slide",
    "retailer",
    "brand",
    "row2_preview",
    "row3_preview",
    "flags",
    "url",
    "screenshot",
    "captured_at",
    "source",
}

HEAD = PatternFill("solid", fgColor="1A1A1A")
EDIT = PatternFill("solid", fgColor="FFF7D6")
FLAG = PatternFill("solid", fgColor="FCE4D6")


def slide_key(g: GroupIn) -> str:
    return "|".join([g.country.value, g.retailer, g.brand.value if g.brand else g.campaign, g.kind])


def _header(ws, cols: list[str], widths: dict[str, int] | None = None) -> None:
    ws.append(cols)
    for i, c in enumerate(cols, 1):
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(c, max(10, min(32, len(c) + 4)))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}1"


def data_fingerprint(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else ""


DRM_MARKER = b"<## NASCA DRM"


def drm_protected(path: Path) -> bool:
    """Corporate DRM (NASCA) encrypts Office files when Excel saves them: unreadable for us."""
    try:
        with path.open("rb") as fh:
            return fh.read(len(DRM_MARKER)) == DRM_MARKER
    except OSError:
        return False


DRM_HELP = (
    "is encrypted by the corporate DRM (it was opened/saved in Excel). Save an unprotected copy "
    "(e.g. File > Save As without protection, or ask IT) or regenerate it with `review --force`."
)


def _open_workbook(path: Path, **kw):
    if drm_protected(path):
        raise ReviewError(f"{path.name} {DRM_HELP}")
    return load_workbook(path, **kw)


def review_fingerprint(path: Path) -> str:
    """Fingerprint of the data.json a review.xlsx was generated from ('' if unknown)."""
    if not path.exists():
        return ""
    wb = _open_workbook(path, read_only=True)
    try:
        if "_meta" not in wb.sheetnames:
            return ""
        for row in wb["_meta"].iter_rows(values_only=True):
            if row and row[0] == "data_fingerprint":
                return str(row[1] or "")
        return ""
    finally:
        wb.close()


def export_review(
    project: Project,
    data: ReportData,
    insights: dict[str, dict[str, dict[str, str]]],
    out: Path,
    status: list[dict] | None = None,
    fingerprint: str = "",
) -> Path:
    wb = Workbook()
    readme = wb.active
    readme.title = "LEEME"
    for line in [
        "Revisión semanal — CCA Consolidated Competitors Report",
        "",
        "1. Revisa cada hoja de país: una fila por producto. Celdas amarillas = editables.",
        "2. include = FALSE quita el producto del PDF; order cambia el orden dentro de la slide.",
        "3. Corrige los campos (precios, cuotas...). row2/row3_preview muestran el texto actual;",
        "   si necesitas un texto distinto escribe row2_override (líneas separadas con ' | ') o row3_override.",
        "4. flags (naranja) = validaciones automáticas a revisar (cuotas que no cuadran, marca equivocada...).",
        "5. Hoja Slides: tipo de promoción, caption e include por slide. Hoja Insights: textos de la tabla.",
        "6. Guarda y ejecuta:  python -m uv run cca-report build --week <semana>",
        "",
        f"Generado {datetime.now():%Y-%m-%d %H:%M}",
    ]:
        readme.append([line])
    readme.column_dimensions["A"].width = 110

    yes_no = '"TRUE,FALSE"'
    by_country: dict[str, list[GroupIn]] = {}
    for g in data.groups:
        by_country.setdefault(g.country.value, []).append(g)

    for code, groups in by_country.items():
        ws = wb.create_sheet(code)
        _header(
            ws,
            PRODUCT_COLS,
            {
                "model_name": 30,
                "bundle_gift": 28,
                "row2_preview": 44,
                "row3_preview": 22,
                "row2_override": 30,
                "flags": 36,
                "url": 40,
                "screenshot": 40,
                "slide": 30,
            },
        )
        dv = DataValidation(type="list", formula1=yes_no, allow_blank=False)
        ws.add_data_validation(dv)
        for g in groups:
            if g.kind != "detail":
                continue
            for i, raw in enumerate(g.products, 1):
                p = _product(project, g, raw)
                texts = row_texts(p, g.row_format)
                flags = product_flags(p, g.row_format)
                trace = raw.get("trace") or {}
                row = {
                    "include": True,
                    "order": i,
                    "slide": slide_key(g),
                    "retailer": g.retailer,
                    "brand": g.brand.value if g.brand else "",
                    **{f: _cell(getattr(p, f)) for f in FIELD_COLS},
                    "row2_preview": " | ".join(texts.row2),
                    "row3_preview": texts.row3,
                    "row2_override": raw.get("row2_override", ""),
                    "row3_override": raw.get("row3_override", ""),
                    "flags": ", ".join(flags),
                    "url": trace.get("url", ""),
                    "screenshot": raw.get("screenshot") or trace.get("screenshot") or "",
                    "html": trace.get("html") or "",
                    "captured_at": str(trace.get("captured_at", "")),
                    "source": trace.get("source", ""),
                }
                _append_text_safe(ws, [row[c] for c in PRODUCT_COLS])
                r = ws.max_row
                dv.add(ws.cell(row=r, column=1))
                for ci, c in enumerate(PRODUCT_COLS, 1):
                    cell = ws.cell(row=r, column=ci)
                    if c not in READONLY:
                        cell.fill = EDIT
                    if c == "flags" and row["flags"]:
                        cell.fill = FLAG
                    if c in NUMERIC:
                        cell.number_format = "#,##0.00"
                    cell.alignment = Alignment(vertical="top", wrap_text=c in {"row2_preview", "flags"})

    ws = wb.create_sheet("Slides")
    _header(ws, SLIDE_COLS, {"slide": 34, "promo_types": 34, "caption": 50, "listing_screenshot": 40, "banners": 50})
    dv = DataValidation(type="list", formula1=yes_no, allow_blank=False)
    ws.add_data_validation(dv)
    for g in data.groups:
        # spec §4: a retailer x brand without promotion gets no slide (the reviewer may opt in)
        _append_text_safe(
            ws,
            [
                bool(g.promo_types) or g.kind != "detail",
                slide_key(g),
                g.country.value,
                g.retailer,
                g.brand.value if g.brand else "",
                g.kind,
                ", ".join(g.promo_types),
                g.capture.value,
                g.row_format,
                g.caption,
                g.listing_screenshot or "",
                "; ".join(f"{b.label}=>{b.screenshot}" for b in g.banners),
            ],
        )
        dv.add(ws.cell(row=ws.max_row, column=1))
        for ci in (1, 7, 10):
            ws.cell(row=ws.max_row, column=ci).fill = EDIT

    ws = wb.create_sheet("Insights")
    _header(ws, INSIGHT_COLS, {"text": 110, "retailer": 22})
    for code, brands in insights.items():
        for brand in [*[b.value for b in BRAND_ORDER], "(subtitle)"]:
            for retailer, text in (brands.get(brand) or {}).items():
                _append_text_safe(ws, [code, brand, retailer, text])
                ws.cell(row=ws.max_row, column=4).fill = EDIT
                ws.cell(row=ws.max_row, column=4).alignment = Alignment(wrap_text=True, vertical="top")

    if status:
        ws = wb.create_sheet("Status")
        cols = ["country", "retailer", "brand", "status", "products", "detail"]
        _header(ws, cols, {"detail": 90})
        for s in status:
            _append_text_safe(ws, [s.get(c, "") for c in cols])
    meta = wb.create_sheet("_meta")
    meta.append(["data_fingerprint", fingerprint])
    meta.append(["generated_at", datetime.now().astimezone().isoformat()])
    meta.sheet_state = "hidden"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


_FORMULA_START = ("=", "+", "-", "@")


def _append_text_safe(ws, values: list[Any]) -> None:
    """Strings scraped from websites must never become Excel formulas (CSV/formula injection)."""
    ws.append(values)
    r = ws.max_row
    for ci, v in enumerate(values, 1):
        if isinstance(v, str) and v.startswith(_FORMULA_START):
            cell = ws.cell(row=r, column=ci)
            cell.value = v
            cell.data_type = "s"


def _cell(v: Any) -> Any:
    if hasattr(v, "value"):
        return v.value
    return "" if v is None else v


def _product(project: Project, g: GroupIn, raw: dict[str, Any]) -> Product:
    fields = {k: v for k, v in raw.items() if k in Product.model_fields and k != "trace"}
    return Product.model_validate(
        {
            "currency": project.country(g.country).currency,
            **fields,
            "country": g.country,
            "retailer": g.retailer,
            "brand": g.brand,
        }
    )


# --------------------------------------------------------------------------- import
class ReviewError(ValueError):
    pass


def _rows(ws) -> list[dict[str, Any]]:
    """Rows as dicts; each value keeps its cell (coordinate + number format) under '_cells'."""
    it = ws.iter_rows()
    head = [str(c.value) if c.value is not None else "" for c in next(it, [])]
    out = []
    for cells in it:
        if not any(c.value not in (None, "") for c in cells):
            continue
        d: dict[str, Any] = dict(zip(head, (c.value for c in cells), strict=False))
        d["_cells"] = dict(zip(head, cells, strict=False))
        out.append(d)
    return out


def _bool(v: Any) -> bool:
    return v is True or str(v).strip().lower() in {"true", "1", "yes", "si", "sí", "verdadero", "x"}


def _num(v: Any, integer: bool = False, cell: Any = None, percent: bool = False) -> float | int | None:
    """Strict: booleans, unparsable text or trailing garbage raise ReviewError with the cell."""
    if v in (None, ""):
        return None
    where = f" (sheet {cell.parent.title}, cell {cell.coordinate})" if cell is not None else ""
    if isinstance(v, bool):
        raise ReviewError(f"boolean where a number was expected{where}")
    if isinstance(v, int | float):
        x = float(v)
        if percent and cell is not None and "%" in (cell.number_format or ""):
            x *= 100  # Excel stores 10% as 0.1
    else:
        import re

        from .normalize import parse_amount

        txt = str(v).strip()
        if not re.fullmatch(r"[^\d]{0,6}\d[\d.,\s]*\s?%?", txt):
            raise ReviewError(f"not a number: {txt!r}{where}")
        x = parse_amount(txt)
        if x is None:
            raise ReviewError(f"not a number: {txt!r}{where}")
    return round(x) if integer else x


def import_review(path: Path, base: ReportData) -> ReportData:
    wb = _open_workbook(path, data_only=True)
    slides = _rows(wb["Slides"])
    groups: list[GroupIn] = []
    orig = {slide_key(g): g for g in base.groups}
    for s in slides:
        if not _bool(s.get("include")):
            continue
        key = str(s["slide"])
        g0 = orig.get(key)
        if g0 is None:
            continue
        g = g0.model_copy(deep=True)
        g.promo_types = [t.strip() for t in str(s.get("promo_types") or "").split(",") if t.strip()]
        g.caption = str(s.get("caption") or "")
        if s.get("listing_screenshot"):
            g.listing_screenshot = str(s["listing_screenshot"])
        if g.kind == "detail":
            if not g.promo_types:
                continue  # spec §4: no promotion -> no slide
            g.products = _products_for(wb, g.country.value, key)
            if not g.products:
                continue
        elif s.get("banners"):
            g.banners = [
                BannerIn(label=a.split("=>")[0].strip(), screenshot=a.split("=>")[1].strip() or None)
                for a in str(s["banners"]).split(";")
                if "=>" in a
            ]
        groups.append(g)

    countries: dict = {}
    for code, cin in base.countries.items():
        countries[code] = CountryIn(retailer_order=list(cin.retailer_order), insights=None)
    if "Insights" in wb.sheetnames:
        per: dict[str, dict[str, dict[str, str]]] = {}
        for r in _rows(wb["Insights"]):
            per.setdefault(str(r["country"]), {}).setdefault(str(r["brand"]), {})[str(r["retailer"])] = str(
                r.get("text") or ""
            )
        for code, brands in per.items():
            subtitle = next(iter((brands.pop("(subtitle)", {}) or {}).values()), "")
            bullets: dict[str, list[str]] = {}
            for brand, cells in brands.items():
                for key in [k for k in cells if k.startswith("(bullet")]:
                    text = cells.pop(key).strip()
                    if text:
                        bullets.setdefault(brand, []).append(text)
            retailers = list(dict.fromkeys(rt for b in brands.values() for rt in b))
            cin = countries.setdefault(code, CountryIn())
            cin.insights = InsightsIn(retailers=retailers, cells=brands, bullets=bullets, subtitle=subtitle)
    return ReportData(week=base.week, cutoff_date=base.cutoff_date, countries=countries, groups=groups)


def _products_for(wb, country: str, key: str) -> list[dict[str, Any]]:
    if country not in wb.sheetnames:
        return []
    rows = [r for r in _rows(wb[country]) if str(r.get("slide")) == key and _bool(r.get("include"))]
    rows.sort(key=lambda r: _num(r.get("order"), True, r["_cells"].get("order")) or 0)
    out = []
    for r in rows:
        d: dict[str, Any] = {}
        for f in FIELD_COLS:
            v = r.get(f)
            cell = r["_cells"].get(f)
            if f in NUMERIC:
                v = _num(v, cell=cell, percent=f == "discount_pct")
            elif f == "installments_n":
                v = _num(v, integer=True, cell=cell)
            elif f == "out_of_stock":
                v = _bool(v)
            elif v is not None:
                v = str(v).strip()
            if v not in (None, ""):
                d[f] = v
        if r.get("row2_override"):
            d["row2_override"] = str(r["row2_override"])
        if r.get("row3_override"):
            d["row3_override"] = str(r["row3_override"])
        if r.get("screenshot"):
            d["screenshot"] = str(r["screenshot"])
        if r.get("url") or r.get("captured_at"):
            d["trace"] = {
                "url": str(r.get("url") or ""),
                "captured_at": str(r.get("captured_at") or datetime.now().astimezone().isoformat()),
                "screenshot": d.get("screenshot"),
                "html": str(r["html"]) if r.get("html") else None,
                "source": str(r.get("source") or "manual"),
            }
        out.append(d)
    return out
