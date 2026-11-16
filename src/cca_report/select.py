"""Choose 3-6 products per retailer x brand (spec §4: promo first, then flagships, then
visible mid-range), one per model so colours of the same phone do not fill the slide."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import SelectionConfig


@dataclass
class Candidate:
    key: str  # model identity used for de-duplication
    name: str
    has_promo: bool
    discount: float
    position: int  # order on the retailer page (visibility)
    out_of_stock: bool
    payload: object  # whatever the caller needs back


def model_key(name: str, storage: str | None = None) -> str:
    base = re.sub(r"\b(5g|4g|lte|dual\s?sim|nuevo|new)\b", "", name.lower())
    base = re.sub(r"[^a-z0-9+]+", " ", base).strip()
    return f"{base}|{(storage or '').lower().replace(' ', '')}"


def is_excluded(name: str, cfg: SelectionConfig) -> bool:
    return any(re.search(p, name, re.I) for p in cfg.exclude_patterns)


def is_phone(text: str, cfg: SelectionConfig) -> bool:
    return any(re.search(p, text, re.I) for p in cfg.phone_patterns) and not is_excluded(text, cfg)


def tier(name: str, cfg: SelectionConfig) -> int:
    if any(re.search(p, name, re.I) for p in cfg.flagship_patterns):
        return 2
    if any(re.search(p, name, re.I) for p in cfg.midrange_patterns):
        return 1
    return 0


def choose(cands: list[Candidate], cfg: SelectionConfig, max_products: int) -> list[Candidate]:
    best: dict[str, Candidate] = {}
    for c in cands:
        if is_excluded(c.name, cfg):
            continue
        cur = best.get(c.key)
        # same model: keep the variant with a promo / bigger discount / earlier position
        if cur is None or (c.has_promo, c.discount, -c.position) > (cur.has_promo, cur.discount, -cur.position):
            best[c.key] = c
    ranked = sorted(
        best.values(),
        key=lambda c: (c.out_of_stock, not c.has_promo, -tier(c.name, cfg), -c.discount, c.position),
    )
    picked = ranked[: max(max_products, 0)]
    return sorted(picked, key=lambda c: c.position)  # keep the retailer's on-page order
