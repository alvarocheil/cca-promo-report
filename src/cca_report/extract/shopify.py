"""Shopify products.json -> Product fields (exact prices, no DOM guessing)."""

from __future__ import annotations

import re

from ..models import Brand
from .dom import OPEN_BOX, Parsed, _storage, clean_card_name


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_product(p: dict, brand: Brand | None = None) -> Parsed:
    out = Parsed()
    title = p.get("title", "")
    out.model_name, out.bundle_gift = clean_card_name(title, brand)
    out.storage = _storage(title)
    variants = p.get("variants") or []
    available = [v for v in variants if v.get("available", True)] or variants
    if not available:
        out.notes.append("no variants")
        return out
    cheapest = min(available, key=lambda v: _f(v.get("price")) or float("inf"))
    out.price_final = _f(cheapest.get("price"))
    before = _f(cheapest.get("compare_at_price"))
    if before and out.price_final and before > out.price_final:
        out.price_before = before
        out.discount_pct = round((1 - out.price_final / before) * 100)
    prices = {_f(v.get("price")) for v in available}
    if len(prices) > 1:
        out.notes.append("from_price")  # several variant prices -> "From USD x"
    out.out_of_stock = not any(v.get("available", True) for v in variants)
    tags = " ".join(p.get("tags") or []) if isinstance(p.get("tags"), list) else str(p.get("tags", ""))
    if OPEN_BOX.search(f"{title} {tags} {p.get('product_type', '')}"):
        out.condition = "open-box"
    if colors := [o for o in p.get("options") or [] if re.match(r"colou?r", o.get("name", ""), re.I)]:
        vals = colors[0].get("values") or []
        out.color = vals[0] if len(vals) == 1 else None
    return out


def merge_with_dom(js: Parsed, dom_parsed: Parsed) -> Parsed:
    """Prices as the customer sees them (DOM): Shopify JSON carries the shop base currency
    (Cover Company: UYU) while the storefront shows USD. JSON adds availability/variants."""
    if dom_parsed.price_final is None:
        # no visible price to check the currency against: never publish the JSON figure
        js.price_final = js.price_before = js.discount_pct = None
        js.notes.append("price_not_visible")
        return js
    out = dom_parsed
    out.out_of_stock = js.out_of_stock or dom_parsed.out_of_stock
    if js.condition != "new":
        out.condition = js.condition
    out.color = out.color or js.color
    out.storage = out.storage or js.storage
    out.bundle_gift = out.bundle_gift or js.bundle_gift
    if "from_price" in js.notes:
        out.notes.append("from_price")
    if js.price_final and abs(js.price_final - dom_parsed.price_final) / dom_parsed.price_final > 0.05:
        out.notes.append("json_currency_differs")
    return out
