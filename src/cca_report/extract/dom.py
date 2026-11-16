"""Offline DOM extraction: card text lines -> Product fields (label-driven heuristics).

Every amount on the card is classified by the words around it: the label on the same
line before the amount, or the previous text line when the amount sits on its own line
(retailers usually render "Abono inicial" / "B/.549.00" as two nodes).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..brands import BRAND_PATTERNS
from ..models import Brand, RawCard, TextLine
from ..normalize import clean_model_name, parse_amount

CURRENCY = r"(?:US\$|USD|U\$S|B\/\.|B\.\/|\$U|\$|₡|Gs\.?|₲|PYG|UYU)"
MONEY = re.compile(rf"({CURRENCY})\s?(\d[\d.,]*\d|\d)")
INSTALLMENTS = re.compile(r"(\d{1,2})\s*(?:x\s*)?(cuotas|meses|pagos|mensualidades|quincenas)\b", re.I)
TIMES = re.compile(rf"(\d{{1,2}})\s*[x×]\s*{CURRENCY}?\s?(\d[\d.,]*)", re.I)
PCT = re.compile(r"(?<![\d.,])-?\s?(\d{1,2})\s?%")
RATING = re.compile(r"valoraci|rating|estrellas|reseñ", re.I)
STORAGE = re.compile(r"(\d{1,4})\s?(GB|TB)\b(?!\s*(?:de\s*)?ram)", re.I)
FROM = re.compile(r"\b(desde|from|a\s+partir\s+de)\b", re.I)

LABELS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore", re.compile(r"\bplan\b(?!\s*de\s*pagos)|ahorr|envío|envio|desde\s+el\s+plan|\bbono\b|puntos", re.I)),
    ("downpayment", re.compile(r"abono|inicial|prima|entrada|enganche|down\s*payment", re.I)),
    ("monthly", re.compile(r"cuota|/\s*mes|mensual|al\s+mes|por\s+mes|x\s*\d+\s*meses|^\s*a\s*\d+\s*meses", re.I)),
    ("before", re.compile(r"antes|habitual|regular|normal|precio\s+de\s+lista|was|tachado|anterior", re.I)),
    ("coupon", re.compile(r"cup[oó]n|coupon", re.I)),
    ("final", re.compile(r"ahora|contado|precio|oferta|rebajado|postpago|prepago|hoy|total", re.I)),
]
GIFT = re.compile(r"regalo|gratis|obsequio|\bincluye\b|de\s+regalo|bundle|lleva\s+gratis", re.I)
OUT_OF_STOCK = re.compile(r"agotado|sin\s+stock|no\s+disponible|out\s+of\s+stock", re.I)
OPEN_BOX = re.compile(r"open\s*box|seminuev", re.I)
REFURB = re.compile(r"reacondicionad|refurbished|\bcpo\b", re.I)
BADGE = re.compile(r"cyber\s*(day|monday|lunes)|black\s*friday|hot\s*sale|outlet|oferta\s+exclusiva|liquidaci", re.I)
NOISE = re.compile(
    r"^(lo quiero|comprar|agregar al carrito|añadir al carrito|ver más|ver detalle|"
    r"características adicionales|especificaciones|detalles del plan|elegir opciones|nuevo)$",
    re.I,
)


@dataclass
class Parsed:
    model_name: str = ""
    storage: str | None = None
    color: str | None = None
    condition: str = "new"
    price_final: float | None = None
    price_before: float | None = None
    discount_pct: float | None = None
    downpayment: float | None = None
    monthly_payment: float | None = None
    installments_n: int | None = None
    coupon_price: float | None = None
    bundle_gift: str | None = None
    badge_text: str | None = None
    out_of_stock: bool = False
    price_label: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_product_fields(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "notes"}
        return d


def _label_of(prefix: str) -> str | None:
    for name, pat in LABELS:
        if pat.search(prefix):
            return name
    return None


def _storage(text: str) -> str | None:
    """Largest GB/TB figure = storage (the smaller one is RAM: "12GB/512GB")."""
    best = None
    for m in STORAGE.finditer(text):
        gb = int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
        if best is None or gb > best[0]:
            best = (gb, f"{m.group(1)} {m.group(2).upper()}")
    if m := re.search(r"\b(\d{2,4})\s*/\s*(\d{1,2})\s?GB\b", text, re.I):  # "512/12GB" = storage/RAM
        gb = int(m.group(1))
        if best is None or gb > best[0]:
            best = (gb, f"{gb} GB")
    if best is None and (m := re.search(r"\b(64|128|256|512|1024)\s*$", text.strip())):
        best = (int(m.group(1)), f"{m.group(1)} GB")
    return best[1] if best else None


def model_from(card: RawCard, brand: Brand | None) -> tuple[str, str | None, str | None]:
    """Pick the product name line; returns (name, storage, bundle gift found in the name)."""
    lines = [ln.text for ln in card.lines] or card.text.splitlines()

    def usable(t: str) -> bool:
        return (
            bool(t)
            and not MONEY.search(t)
            and not NOISE.match(t)
            and _label_of(t) is None
            and (t.strip().lower() not in {b.value.lower() for b in Brand})
        )

    candidates = [card.name] if card.name and usable(card.name) else []
    if brand is not None:
        candidates += [t for t in lines if BRAND_PATTERNS[brand].search(t) and usable(t)]
    candidates += [t for t in lines if len(t) > 2 and usable(t)]
    raw = next(iter(candidates), "")
    name, gift = clean_card_name(raw, brand)
    return name, _storage(raw) or _storage(card.text), gift


_SPEC_TOKENS = re.compile(
    r"\b(\d+\s?(gb|tb)(\s?(de\s)?ram)?|ram|dual\s?sim|lte|[45]g|android(\s?\d+)?|octa-core|\d+(\.\d+)?(\"|''|\s?pulgadas)|"
    r"\d+\s?mp|\d+\s?mah|hd\+?|magic\s?os\s?\d+|open\s*box|seminuevos?|reacondicionados?|fifa\s+edition)\b",
    re.I,
)
# marketing tails: everything from these words on is not part of the model name
_PREFIX = re.compile(r"^\s*(celular(es)?|smartphone|tel[eé]fono( m[oó]vil)?)(\s+[45]g)?\s+", re.I)
_COLORS = (
    r"negro|blanco|azul|verde|gris|grafito|plata|plateado|dorado|oro|rosa|rosado|morado|violeta|lila|beige|"
    r"naranja|rojo|amarillo|celeste|crema|black|white|blue|green|gr[ae]y|silver|gold|purple|pink|"
    r"burgund\w*|borgo[ñn]a|burdeos|titanio\w*|titanium|medianoche|midnight|starlight|oc[eé]ano\w*|lavanda|"
    r"menta|coral|cobalto|desert|natural|c[oó]smic[oa]?|cosmic|mocha|glacier|charcoal|awesome|teal|ultramarine|"
    r"sage|lavender|mist|jet|onyx|icy|graphite|deep\s+blue|sky\s+blue|cloud|light|dark|tit[aá]n"
)
_COLOR_TAIL = re.compile(rf"\s(-\s)?({_COLORS})\b.*$", re.I)
_CODES = re.compile(
    r"\b([A-Z]{1,3}\d{3,5}[A-Z]{0,3}|LL|CPO|\d{5,}(-\d+)?|(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]+-?[A-Z0-9-]{5,})\b"
)
_NOISE_WORDS = re.compile(
    r"\b(ver\s+detalles?|en\s+stock|nuevo|esim|con\s+esim|global|fifa(\s+edition)?|gar|edici[oó]n\s+especial)\b", re.I
)
_TAIL = re.compile(r"\s(chip|pantalla|oled|super\s+retina|procesador|c[aá]mara|bater[ií]a|color)\b.*$", re.I)
_KEEP_UPPER = {"5G", "4G", "FE", "GB", "TB", "XL", "SE", "II", "III", "LTE", "AI"}


def _title(word: str) -> str:
    if word.isupper() and len(word) > 2 and word not in _KEEP_UPPER and not any(ch.isdigit() for ch in word):
        return word.capitalize()
    return word


def clean_card_name(raw: str, brand: Brand | None = None) -> tuple[str, str | None]:
    """'Honor 600 ELITE 5G + Auriculares honor X7 Lite' -> ('Honor 600 Elite', 'Auriculares honor X7 Lite')."""
    gift = None
    name = raw.split("|")[0]
    if " + " in name:
        name, gift = name.split(" + ", 1)
        gift = clean_gift(gift) if gift.strip() else None
    name = _NOISE_WORDS.sub(" ", _PREFIX.sub("", name))
    name = _TAIL.sub("", name)
    name = _CODES.sub(" ", name)
    name = re.sub(r"\s*/\s*", " ", name)
    name = re.sub(r"^(samsung|apple)\s+", "", name.strip(), flags=re.I)
    name = re.sub(r"^motorola\s+(?=(moto|razr|edge|g\d))", "", name, flags=re.I)
    name = re.sub(r"\bpro\s?max\b", "Pro Max", name, flags=re.I)
    name = _SPEC_TOKENS.sub(" ", name)
    name = _COLOR_TAIL.sub("", f" {name.strip()}").strip()
    name = re.sub(r"\s(64|128|256|512|1024)\s*$", "", name)  # bare storage at the end ("iPhone 18 PRO 256")
    name = re.sub(r"\s-(\s|$)|\s(GB|TB)\b", " ", f"{name} ", flags=re.I)  # dangling "- GB" left by removals
    name = " ".join(_title(w) for w in name.split()).strip(" -–,")
    name = re.sub(r"\bfe\b", "FE", name, flags=re.I)
    name = clean_model_name(name)
    if brand == Brand.HONOR and name and not re.match(r"honor\b", name, re.I):
        name = f"Honor {name}"
    elif brand == Brand.MOTOROLA and name and not re.match(r"(moto|razr|edge|motorola)\b", name, re.I):
        name = f"Moto {name}"
    elif (
        brand == Brand.SAMSUNG
        and name
        and not re.match(r"galaxy\b", name, re.I)
        and re.match(r"[zsamx]\s?\w", name, re.I)
    ):
        name = f"Galaxy {name}"
    elif brand == Brand.APPLE and name and re.match(r"\d", name):
        name = f"iPhone {name}"
    return name, gift


_GIFT_PREFIX = re.compile(r"^\s*(incluye|gratis|de\s+regalo|regalo|obsequio|bundle|lleva\s+gratis)\s*:?\s*", re.I)


def clean_gift(text: str) -> str:
    """'Incluye Watch8' -> 'Watch8' (the row text already says 'Bundle Promotion: Gift ...')."""
    t = re.sub(r"\s+", " ", text).strip()
    t = _GIFT_PREFIX.sub("", t) or t
    t = _NOISE_WORDS.sub(" ", _CODES.sub(" ", t))
    return " ".join(_title(w) for w in t.split())[:80]


def _take_pct(out: Parsed, text: str, previous: str) -> None:
    """A discount percentage; star ratings ("Valoración: 0%") and 0% are never discounts."""
    pm = PCT.search(text)
    if pm and out.discount_pct is None and not RATING.search(text) and not RATING.search(previous):
        v = float(pm.group(1))
        if v > 0:
            out.discount_pct = v


def parse_card(card: RawCard, brand: Brand | None, preferred_installments: int | None = None) -> Parsed:
    out = Parsed()
    out.model_name, out.storage, name_gift = model_from(card, brand)
    if name_gift:
        out.bundle_gift = name_gift
    lines: list[TextLine] = card.lines or [TextLine(text=t) for t in card.text.splitlines() if t.strip()]
    amounts: list[tuple[str | None, float, TextLine]] = []
    prev_label: str | None = None
    from_pending = False
    struck_values = {parse_amount(m.group(2)) for t in card.struck for m in MONEY.finditer(t)}
    # every "N x $amount" plan on the card (Bristol shows 1x ... 18x); amounts inside a plan
    # are installments, never the price
    plans: list[tuple[int, float]] = []
    plan_spans: dict[int, list[tuple[int, int]]] = {}
    for li, ln in enumerate(lines):
        for tm in TIMES.finditer(ln.text):
            v = parse_amount(tm.group(2))
            if v is not None:
                plan_spans.setdefault(li, []).append(tm.span())
                # a struck plan ("15x Gs. 327.000" crossed out next to "15x Gs. 310.000") is the old one
                if not (ln.strike or v in struck_values):
                    plans.append((int(tm.group(1)), v))
    for li, ln in enumerate(lines):
        text = ln.text
        if INSTALLMENTS.search(text) and out.installments_n is None:
            out.installments_n = int(INSTALLMENTS.search(text).group(1))
            if re.search(r"renueva|renovaci", text, re.I):
                out.notes.append("renewal_term")  # a plan-renewal term, not a financing plan
        found = list(MONEY.finditer(text))
        if not found:
            lab = _label_of(text)
            # a label line only applies to the amount on the *next* line ("ignore" words such
            # as "Envío gratis" only neutralise amounts on their own line)
            prev_label = lab if lab != "ignore" else None
            if GIFT.search(text) and not out.bundle_gift:
                out.bundle_gift = clean_gift(text)
            if BADGE.search(text) and not out.badge_text:
                out.badge_text = text.strip()[:60]
            _take_pct(out, text, lines[li - 1].text if li else "")
            if FROM.search(text):
                from_pending = True
            continue
        last_end = 0
        spans = plan_spans.get(li, [])
        found = [m for m in found if not any(a <= m.start() < b for a, b in spans)]
        for i, m in enumerate(found):
            value = parse_amount(m.group(2))
            if value is None or value < 0:
                last_end = m.end()
                continue
            # context of *this* amount only: text since the previous amount, then the text
            # right after it up to the next amount ("B/.35/mes")
            before_seg = text[last_end : m.start()]
            after_seg = text[m.end() : found[i + 1].start() if i + 1 < len(found) else len(text)]
            label = _label_of(before_seg)
            if label is None and not before_seg.strip():
                label = prev_label if i == 0 else None
                prev_label = None  # consumed
            if label is None:
                label = _label_of(after_seg)
            if ln.strike or value in struck_values:
                label = "before"
            if label in (None, "final") and (FROM.search(before_seg) or from_pending):
                out.price_label = "From"
            from_pending = False
            amounts.append((label, value, ln))
            last_end = m.end()
        prev_label = None
        _take_pct(out, text, lines[li - 1].text if li else "")

    real_plans = [pl for pl in plans if pl[0] > 1]
    if real_plans:
        pick = next((pl for pl in real_plans if pl[0] == preferred_installments), None) or max(
            real_plans, key=lambda pl: pl[0]
        )
        out.installments_n, out.monthly_payment = pick

    def first(label: str) -> float | None:
        return next((v for lab, v, _ in amounts if lab == label), None)

    out.downpayment = first("downpayment")
    out.monthly_payment = out.monthly_payment or first("monthly")
    out.price_before = first("before")
    out.coupon_price = first("coupon")
    out.price_final = first("final")
    if out.price_final is None:
        # unlabeled amounts: the most prominent one (largest font, bold) wins
        free = [(v, ln) for lab, v, ln in amounts if lab is None]
        if free:
            # first of the most prominent (sale price usually comes before the list price)
            out.price_final = max(free, key=lambda t: (t[1].size, t[1].bold))[0]
    struck = any(lab == "before" and (ln.strike or v in struck_values) for lab, v, ln in amounts)
    if out.price_final is None and out.price_before is not None and not struck:
        # only the regular price is shown ("Precio habitual"): that is the price, no discount.
        # A struck-through amount alone is an old price: the current one stays unknown.
        out.price_final, out.price_before = out.price_before, None
    if out.price_before is not None and out.price_final is not None:
        if out.price_before <= out.price_final:
            out.notes.append("before<=final: dropped")
            out.price_before = None
        elif out.discount_pct is None:
            out.discount_pct = round((1 - out.price_final / out.price_before) * 100)
    out.out_of_stock = bool(OUT_OF_STOCK.search(card.text))
    if OPEN_BOX.search(card.text):
        out.condition = "open-box"
    elif REFURB.search(card.text):
        out.condition = "refurbished"
    return out
