"""Number parsing, country text formats (spec §5) and automatic validations."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .brands import BRAND_PATTERNS
from .models import BRAND_ORDER, Brand, Product

# --------------------------------------------------------------------------- parsing
# a number with optional 3-digit groups (".", "," or a space) and optional decimals;
# "1,240 18 cuotas" -> 1240, never 1.24018
_NUM = re.compile(r"[-+]?\d{1,3}(?:[.,\u00a0 ]\d{3})+(?:[.,]\d+)?(?!\d)|[-+]?\d+(?:[.,]\d+)?")


def parse_amount(raw: str | float | int | None, *, decimals_allowed: bool = True) -> float | None:
    """Parse '₡1.209.900', '1,209,900.00', 'USD $1.240,50', 'PYG 2.299.000' -> float.

    Rules: when both separators appear, the last one is the decimal mark. With a single
    separator type, it is a thousands separator if it repeats or is followed by exactly
    three digits (``1.209``) — sites in CR/PY write thousands with dots.
    """
    if raw is None:
        return None
    if isinstance(raw, int | float):
        return float(raw)
    m = _NUM.search(raw)
    if not m:
        return None
    s = re.sub(r"\s", "", m.group(0)).rstrip(".,")
    if "." in s and "," in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        thou = "," if dec == "." else "."
        s = s.replace(thou, "").replace(dec, ".")
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        parts = s.split(sep)
        is_thousands = len(parts) > 2 or len(parts[-1]) == 3 or not decimals_allowed
        s = s.replace(sep, "") if is_thousands else s.replace(sep, ".")
    try:
        return float(s)
    except ValueError:
        return None


_IPHONE = re.compile(r"[¡!i|l1]\s?phone", re.I)


def clean_model_name(name: str) -> str:
    """Normalise brand casing; OCR/vision often returns '¡Phone' or 'IPHONE'."""
    name = re.sub(r"\s+", " ", name).strip()
    name = _IPHONE.sub("iPhone", name)
    name = re.sub(r"\bgalaxy\b", "Galaxy", name, flags=re.I)
    return name


# --------------------------------------------------------------------------- money
def _num(v: float, decimals: int) -> str:
    return f"{v:,.{decimals}f}"


def _auto(v: float) -> str:
    """USD 649 / USD 798.33: no decimals when the amount is whole."""
    return _num(v, 0) if abs(v - round(v)) < 0.005 else _num(v, 2)


def money(v: float | None, style: str) -> str:
    if v is None:
        return ""
    match style:
        case "usd_long":  # USD $1,240.00
            return f"USD ${_num(v, 2)}"
        case "usd_short":  # $1,240.00
            return f"${_num(v, 2)}"
        case "crc":  # ₡1,329,900.00
            return f"₡{_num(v, 2)}"
        case "pyg":  # PYG 2,299,000
            return f"PYG {_num(v, 0)}"
        case "pyg_bare":  # 2,669,000
            return _num(v, 0)
        case "usd_uy":  # USD 579 / USD 798.33
            return f"USD {_auto(v)}"
        case "uyu":  # UYU 3,665
            return f"UYU {_num(v, 0)}"
    raise ValueError(f"unknown money style {style}")


def pct(v: float | None) -> str:
    return "" if v is None else f"{v:.0f}%"


# --------------------------------------------------------------------------- row formats
@dataclass(frozen=True, slots=True)
class RowTexts:
    row2: list[str]  # promotion detail lines
    row3: str  # final price


def _installments(p: Product, style: str, unit: str = "cuotas") -> str | None:
    if p.monthly_payment is None:
        return None
    n = f" x {p.installments_n} {unit}" if p.installments_n else ""
    return f"Monthly payment: {money(p.monthly_payment, style)}{n}"


def _gift(p: Product) -> str | None:
    return f"Bundle Promotion: Gift {p.bundle_gift}" if p.bundle_gift else None


def fmt_pa_operator(p: Product) -> RowTexts:
    rows = [
        f"Downpayment: {money(p.downpayment, 'usd_long')}" if p.downpayment is not None else None,
        _installments(p, "usd_long"),
        _gift(p),
    ]
    label = p.price_label or ""
    row3 = f"{label} {money(p.price_final, 'usd_short')}".strip()
    return RowTexts([r for r in rows if r], row3)


def fmt_pa_retailer(p: Product) -> RowTexts:
    if p.price_before is not None:
        rows = [
            f"Before: {money(p.price_before, 'usd_long')} / After: {money(p.price_final, 'usd_long')}",
            _installments(p, "usd_long"),
        ]
    elif p.monthly_payment is not None:
        rows = [
            f"Downpayment: {money(p.downpayment or 0.0, 'usd_long')}",
            _installments(p, "usd_long", unit="meses"),
        ]
    else:  # no discount and no financing on the card: say so, never invent a $0 downpayment
        rows = [p.badge_text or "Regular price"]
    rows.append(_gift(p))
    return RowTexts([r for r in rows if r], money(p.price_final, "usd_short"))


def fmt_cr_default(p: Product) -> RowTexts:
    rows: list[str | None] = []
    if p.price_label and p.price_final is not None:
        rows.append(f"{p.price_label} {money(p.price_final, 'crc')}")
    if p.price_before is not None:
        disc = f" · {pct(p.discount_pct)} off" if p.discount_pct else ""
        rows.append(f"Before {money(p.price_before, 'crc')}{disc}")
    if p.monthly_payment is not None:
        n = f" x {p.installments_n}" if p.installments_n else ""
        rows.append(f"Monthly payment {money(p.monthly_payment, 'crc')}{n}")
    rows.append(_gift(p))
    rows.append(p.badge_text)
    return RowTexts([r for r in rows if r], money(p.price_final, "crc"))


def fmt_ec_claro(p: Product) -> RowTexts:
    rows = [p.bundle_gift, p.badge_text]
    if p.monthly_payment is not None:
        n = f" x {p.installments_n}" if p.installments_n else ""
        rows.append(f"Monthly payment {money(p.monthly_payment, 'usd_short')}{n}")
    return RowTexts([r for r in rows if r], money(p.price_final, "usd_short"))


def fmt_ec_tigo(p: Product) -> RowTexts:
    n = p.installments_n or 18
    rows = [f"Renewal up to {n} months for the post paid plan of: {money(p.price_final, 'usd_short')}"]
    if p.bundle_gift:
        rows.append(_gift(p))
    return RowTexts([r for r in rows if r], money(p.price_final, "usd_short"))


def fmt_py_default(p: Product) -> RowTexts:
    rows: list[str | None] = []
    if p.price_before is not None:
        disc = f" · {pct(p.discount_pct)}" if p.discount_pct else ""
        rows.append(f"Was {money(p.price_before, 'pyg_bare')}{disc}")
    if p.monthly_payment is not None and p.installments_n:
        rows.append(f"{p.installments_n} × {money(p.monthly_payment, 'pyg')}")
    if p.badge_text:
        rows.append(p.badge_text)
    rows.append(_gift(p))
    return RowTexts([r for r in rows if r], f"{money(p.price_final, 'pyg')} cash price")


def fmt_uy_cover(p: Product) -> RowTexts:
    rows: list[str | None] = []
    if p.discount_pct and p.price_before is not None:
        rows.append(f"{pct(p.discount_pct)} advertised. Was {money(p.price_before, 'usd_uy')}")
    elif p.price_before is not None:
        rows.append(f"Was {money(p.price_before, 'usd_uy')}")
    if p.condition == "open-box":
        rows.append("Open-box, lightly used")
    if p.out_of_stock:
        rows.append("Listed as out of stock")
    if p.badge_text:
        rows.append(p.badge_text)
    if not rows:
        rows.append("New" if p.condition == "new" else p.condition.capitalize())
    label = p.price_label or ""
    return RowTexts([r for r in rows if r], f"{label} {money(p.price_final, 'usd_uy')}".strip())


def fmt_uy_ml(p: Product) -> RowTexts:
    first = pct(p.discount_pct)
    if p.coupon_price is not None:
        first = (
            f"{first}. With coupon {money(p.coupon_price, 'usd_uy')}"
            if first
            else (f"With coupon {money(p.coupon_price, 'usd_uy')}")
        )
    rows = [first or None]
    if p.monthly_payment is not None and p.installments_n:
        style = "uyu" if (p.secondary_currency or "UYU") == "UYU" else "usd_uy"
        rows.append(f"{p.installments_n} × {money(p.monthly_payment, style)} interest-free")
    if p.badge_text:
        rows.append(p.badge_text)
    return RowTexts([r for r in rows if r], money(p.price_final, "usd_uy"))


ROW_FORMATS: dict[str, Callable[[Product], RowTexts]] = {
    "pa_operator": fmt_pa_operator,
    "pa_retailer": fmt_pa_retailer,
    "cr_default": fmt_cr_default,
    "ec_claro": fmt_ec_claro,
    "ec_tigo": fmt_ec_tigo,
    "py_default": fmt_py_default,
    "uy_cover": fmt_uy_cover,
    "uy_ml": fmt_uy_ml,
}

# Allowed financing surcharge over the final price (operators and retailers add interest).
# PY credit prices run ~50% above the cash price (W44: 15 x 230,000 vs 2,299,000 cash).
FINANCING_SURCHARGE_MAX = {"pa_retailer": 0.10, "pa_operator": 0.10, "cr_default": 0.15, "py_default": 0.55}
TOLERANCE = 0.03


def row_texts(p: Product, row_format: str) -> RowTexts:
    try:
        fn = ROW_FORMATS[row_format]
    except KeyError as exc:
        raise ValueError(f"unknown row_format '{row_format}' (known: {', '.join(ROW_FORMATS)})") from exc
    return fn(p)


# --------------------------------------------------------------------------- validations
_PLACEHOLDER = re.compile(r"^\s*(x{3,}|tbd|n/?a|-+|\?+)?\s*$", re.I)


def _other_brand_in_name(p: Product) -> Brand | None:
    for b in BRAND_ORDER:
        if b != p.brand and BRAND_PATTERNS[b].search(p.model_name):
            return b
    return None


def product_flags(p: Product, row_format: str) -> list[str]:
    flags: list[str] = []
    if _PLACEHOLDER.match(p.model_name or ""):
        flags.append("missing_model_name")
    if p.price_final is None:
        flags.append("missing_price")
    if (
        p.monthly_payment is not None
        and p.downpayment is not None
        and (p.monthly_payment > 0 or p.downpayment > 0)
        and abs(p.monthly_payment - p.downpayment) < 0.005
    ):
        flags.append("monthly_equals_downpayment")
    other_ccy = p.secondary_currency and p.secondary_currency != p.currency  # e.g. UYU installments
    if p.installments_n is not None and p.installments_n <= 0:
        flags.append("invalid_installments_n")
    for field in ("price_final", "monthly_payment", "downpayment", "price_before", "coupon_price"):
        if (v := getattr(p, field)) is not None and v < 0:
            flags.append(f"negative_{field}")
    if p.monthly_payment is not None and p.installments_n and p.price_final is not None and not other_ccy:
        total = (p.downpayment or 0.0) + p.monthly_payment * p.installments_n
        surcharge = FINANCING_SURCHARGE_MAX.get(row_format, 0.0)
        low = p.price_final * (1 - TOLERANCE)
        high = p.price_final * (1 + surcharge + TOLERANCE)
        if not (low <= total <= high):
            flags.append(f"installments_mismatch:{total:,.2f}_vs_{p.price_final:,.2f}")
    if (other := _other_brand_in_name(p)) is not None:
        flags.append(f"brand_mismatch:{other.value}")
    if p.promo_type is not None and _PLACEHOLDER.match(p.promo_type):
        flags.append("placeholder_promo_type")
    if any("¡" in s for s in (p.model_name, p.bundle_gift or "", p.badge_text or "")):
        flags.append("ocr_artifact")
    return flags


def duplicate_columns(columns: Iterable[tuple[str, tuple[str, ...], str]]) -> set[int]:
    """Indexes of columns identical to an earlier one (same model, row2 and row3)."""
    seen: dict[tuple, int] = {}
    dups: set[int] = set()
    for i, col in enumerate(columns):
        if col in seen:
            dups.add(i)
        else:
            seen[col] = i
    return dups


def promo_type_ok(text: str) -> bool:
    return not _PLACEHOLDER.match(text or "")
