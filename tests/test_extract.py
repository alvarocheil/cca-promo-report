"""Extraction tests built from real card texts seen on the pilot sites (Phase 2)."""

from datetime import date
from pathlib import Path

from cca_report.config import Project
from cca_report.extract import dom, shopify
from cca_report.extract.pipeline import infer_promo_types
from cca_report.models import Brand, RawCard, SelectionConfig, TextLine
from cca_report.render.document import GroupIn, ReportData
from cca_report.review import export_review, import_review, slide_key
from cca_report.select import Candidate, choose, model_key


def card(lines: list[str | tuple[str, bool]], name: str = "") -> RawCard:
    tl = [TextLine(text=t, strike=False) if isinstance(t, str) else TextLine(text=t[0], strike=t[1]) for t in lines]
    return RawCard(index=0, text="\n".join(x.text for x in tl), lines=tl, name=name)


def test_mas_movil_card():
    c = card(
        [
            "APPLE",
            "iPhone 18 Pro Max",
            "Pago contado",
            "B/.1,485.00",
            "Precio varía",
            "de acuerdo",
            "al plan",
            "Pago cuotas",
            "B/.39.00",
            "24 meses",
            "Abono inicial",
            "B/.549.00",
            "Lo quiero",
        ]
    )
    p = dom.parse_card(c, Brand.APPLE)
    assert p.model_name == "iPhone 18 Pro Max"
    assert (p.price_final, p.monthly_payment, p.installments_n, p.downpayment) == (1485.0, 39.0, 24, 549.0)


def test_tigo_card_ignores_plan_prices():
    c = card(
        [
            "Apple",
            "iPhone 18 Pro Max 256GB",
            "Precio Postpago",
            "B/.1685.95",
            "Plan B./22.3",
            "Plan B./25.87",
            "Plan",
            "Detalles del plan",
            "24 cuotas de",
            "B/.35/mes",
            "Abono B/.845.95",
            "Lo quiero",
        ],
        name="iPhone 18 Pro Max 256GB",
    )
    p = dom.parse_card(c, Brand.APPLE)
    assert p.model_name == "iPhone 18 Pro Max"
    assert p.storage == "256 GB"
    assert (p.price_final, p.monthly_payment, p.installments_n, p.downpayment) == (1685.95, 35.0, 24, 845.95)


def test_panafoto_card_struck_price():
    c = card(["SAMSUNG", "Galaxy A17 | 128 GB | 6 GB RAM | Azul", "Ahora", "$ 208.95", ("Antes $ 269.95", True)])
    p = dom.parse_card(c, Brand.SAMSUNG)
    assert p.model_name == "Galaxy A17"
    assert (p.price_final, p.price_before, p.discount_pct) == (208.95, 269.95, 23)


def test_cover_company_dom_fallback_comma_decimals():
    c = card(
        [
            "Samsung Galaxy Z Fold 8 Ultra 5G 512GB / 12GB",
            "Precio rebajado",
            "USD 2.599,00",
            "Precio habitual",
            "USD 2.999,00",
            "VIOLETA",
        ]
    )
    p = dom.parse_card(c, Brand.SAMSUNG)
    assert p.model_name.startswith("Galaxy Z Fold 8 Ultra")
    assert (p.price_final, p.price_before) == (2599.0, 2999.0)


def test_open_box_detected():
    c = card(["Apple iPhone 15 128GB Open Box Seminuevos", "Precio rebajado", "USD 699,00"])
    p = dom.parse_card(c, Brand.APPLE)
    assert p.condition == "open-box" and p.model_name == "iPhone 15"


def test_shopify_product():
    prod = {
        "title": "Samsung Galaxy A27 5G 256GB / 8GB RAM Dual SIM",
        "variants": [
            {"price": "459.00", "compare_at_price": "499.00", "available": True},
            {"price": "479.00", "compare_at_price": "499.00", "available": True},
        ],
        "options": [{"name": "Color", "values": ["NEGRO", "ROSA"]}],
    }
    p = shopify.parse_product(prod)
    assert (p.price_final, p.price_before, p.discount_pct) == (459.0, 499.0, 8)
    assert "from_price" in p.notes and p.storage == "256 GB"


def test_selection_prefers_promo_flagship_and_dedupes_colors():
    cfg = SelectionConfig()
    mk = lambda name, pos, promo=False, disc=0: Candidate(  # noqa: E731
        key=model_key(name), name=name, has_promo=promo, discount=disc, position=pos, out_of_stock=False, payload=name
    )
    cands = [
        mk("Galaxy A17", 1, True, 23),
        mk("Galaxy A17", 2, True, 20),  # same model, two colours
        mk("Galaxy Tab S10", 3, True, 30),  # tablet: excluded
        mk("Galaxy S26 Ultra", 4),
        mk("Galaxy A57", 5),
        mk("Galaxy Z Flip8", 6),
        mk("Galaxy A07", 7),
    ]
    picked = [c.payload for c in choose(cands, cfg, 3)]
    assert picked == ["Galaxy A17", "Galaxy S26 Ultra", "Galaxy Z Flip8"]


def test_infer_promo_types():
    assert infer_promo_types([{"monthly_payment": 9, "installments_n": 18}, {"price_before": 2}]) == [
        "Monthly Payment",
        "Price Discount",
    ]
    assert infer_promo_types([{"bundle_gift": "Buds"}]) == ["Bundle promotion"]
    assert infer_promo_types([{"installments_n": 18, "price_final": 802.33}]) == []  # term alone is not a payment


def test_review_roundtrip_applies_edits(tmp_path):
    from openpyxl import load_workbook

    p = Project()
    g = GroupIn(
        country="PA",
        retailer="TIGO",
        brand="Samsung",
        promo_types=["Monthly Payment"],
        capture="listing",
        row_format="pa_operator",
        products=[
            {
                "model_name": "Galaxy A57",
                "downpayment": 168.05,
                "monthly_payment": 16,
                "installments_n": 24,
                "price_final": 552,
                "trace": {"url": "https://x", "captured_at": "2026-11-06T10:00:00-05:00", "source": "dom"},
            },
            {"model_name": "Galaxy A17", "price_final": 232.95},
        ],
    )
    data = ReportData(week="2026-W45", cutoff_date=date(2026, 11, 6), groups=[g])
    path = export_review(p, data, {"PA": {"Samsung": {"TIGO": "**Monthly** plans."}}}, tmp_path / "r.xlsx")
    wb = load_workbook(path)
    ws = wb["PA"]
    head = [c.value for c in ws[1]]
    ws.cell(row=3, column=head.index("include") + 1, value=False)  # drop Galaxy A17
    ws.cell(row=2, column=head.index("price_final") + 1, value="$560.00")  # fix a price as text
    ws.cell(row=2, column=head.index("row3_override") + 1, value="Payment in full $560.00")
    wb.save(path)
    out = import_review(path, data)
    prods = out.groups[0].products
    assert len(prods) == 1 and prods[0]["price_final"] == 560.0
    assert prods[0]["row3_override"] == "Payment in full $560.00"
    assert out.countries["PA"].insights.cells[Brand.SAMSUNG]["TIGO"] == "**Monthly** plans."
    assert slide_key(out.groups[0]) == "PA|TIGO|Samsung|detail"


def test_gift_prefix_and_name_cleanup():
    assert dom.clean_gift("Incluye Watch8 Classic") == "Watch8 Classic"
    name, gift = dom.clean_card_name("Honor 600 ELITE 5G + Auriculares honor X7 Lite 256GB", Brand.HONOR)
    assert name == "Honor 600 Elite" and gift.startswith("Auriculares")
    assert dom.clean_card_name("Z FOLD 7", Brand.SAMSUNG)[0] == "Galaxy Z Fold 7"
    assert dom.clean_card_name("G06", Brand.MOTOROLA)[0] == "Moto G06"
    assert (
        dom.clean_card_name('iPhone 16 Pro Chip A18 Pro 6.3" OLED Super Retina XDR', Brand.APPLE)[0] == "iPhone 16 Pro"
    )
    assert dom._storage("Edge 70 Pro 5G 12GB/512GB") == "512 GB"


def test_regular_price_only_becomes_final():
    c = card(["Honor 600e 5G 512GB", "Precio habitual", "USD 639,00"])
    p = dom.parse_card(c, Brand.HONOR)
    assert p.price_final == 639.0 and p.price_before is None


def test_shopify_json_currency_never_published_without_visible_price():
    js = shopify.parse_product({"title": "Galaxy S26", "variants": [{"price": "38089.00", "available": True}]})
    merged = shopify.merge_with_dom(js, dom.Parsed(model_name="Galaxy S26"))
    assert merged.price_final is None and "price_not_visible" in merged.notes
    shown = dom.Parsed(model_name="Galaxy S26", price_final=929.0)
    assert shopify.merge_with_dom(js, shown).price_final == 929.0


# ------------------------------------------------------------------ Codex review (Phase 2)
def test_each_amount_uses_its_own_label():
    p = dom.parse_card(card(["Galaxy A17", "Antes $100 Ahora $80"]), Brand.SAMSUNG)
    assert (p.price_final, p.price_before) == (80.0, 100.0)
    p = dom.parse_card(card(["Galaxy A17", "Abono $100; 24 cuotas de $20", "Precio $580"]), Brand.SAMSUNG)
    assert (p.downpayment, p.monthly_payment, p.installments_n, p.price_final) == (100.0, 20.0, 24, 580.0)


def test_label_line_applies_only_to_next_line():
    p = dom.parse_card(card(["Galaxy A17", "Abono inicial", "Envío gratis", "$ 50"]), Brand.SAMSUNG)
    assert p.downpayment is None and p.price_final == 50.0


def test_struck_price_alone_is_not_the_current_price():
    p = dom.parse_card(card(["Galaxy A17", ("Antes $100", True)]), Brand.SAMSUNG)
    assert p.price_final is None and p.price_before == 100.0


def test_zero_downpayment_is_kept_and_desde_label():
    p = dom.parse_card(
        card(["Moto G06", "Abono inicial", "B/.0.00", "Pago cuotas", "B/.9.10", "24 meses"]), Brand.MOTOROLA
    )
    assert p.downpayment == 0.0
    p = dom.parse_card(card(["Apple iPhone 16 Pro 256GB", "Desde USD 1.349,00"]), Brand.APPLE)
    assert p.price_label == "From" and p.price_final == 1349.0


def test_storage_terabyte():
    assert dom._storage("iPhone 16 Pro Max 1TB") == "1 TB"
    assert dom._storage("Razr Fold 1TB/16GB") == "1 TB"


def test_capture_crash_replaces_old_groups():
    from cca_report.capture.runner import failed_groups, merge_groups

    r = next(x for x in Project().retailers if x.slug == "pa_tigo")
    new = failed_groups(r, RuntimeError("boom"))
    assert len(new) == 4 and all(g.pages[0].status.value == "error" for g in new)
    old = [g.model_copy(update={"retailer": "OLD"}) for g in new]
    assert all(g.retailer == "TIGO" for g in merge_groups(old, new))


def _review_data():
    g = GroupIn(
        country="PA",
        retailer="TIGO",
        brand="Samsung",
        promo_types=["Monthly Payment"],
        capture="listing",
        row_format="pa_operator",
        products=[
            {
                "model_name": "=1+1",
                "price_final": 552,
                "monthly_payment": 16,
                "installments_n": 24,
                "trace": {
                    "url": "https://x",
                    "captured_at": "2026-11-06T10:00:00-05:00",
                    "html": "runs/2026-W45/html/x.html",
                    "source": "dom",
                },
            },
        ],
    )
    return ReportData(week="2026-W45", cutoff_date=date(2026, 11, 6), groups=[g])


def test_review_text_never_becomes_formula_and_html_trace_survives(tmp_path):
    from openpyxl import load_workbook

    data = _review_data()
    path = export_review(Project(), data, {}, tmp_path / "r.xlsx")
    ws = load_workbook(path)["PA"]
    head = [c.value for c in ws[1]]
    cell = ws.cell(row=2, column=head.index("model_name") + 1)
    assert cell.data_type == "s" and cell.value == "=1+1"
    prod = import_review(path, data).groups[0].products[0]
    assert prod["model_name"] == "=1+1" and prod["trace"]["html"] == "runs/2026-W45/html/x.html"


def test_review_cleared_promo_type_drops_slide_and_bad_numbers_raise(tmp_path):
    import pytest
    from openpyxl import load_workbook

    from cca_report.review import ReviewError

    data = _review_data()
    path = export_review(Project(), data, {}, tmp_path / "r.xlsx")
    wb = load_workbook(path)
    wb["Slides"].cell(row=2, column=7, value="")
    wb.save(path)
    assert import_review(path, data).groups == []

    path = export_review(Project(), data, {}, tmp_path / "r2.xlsx")
    wb = load_workbook(path)
    ws = wb["PA"]
    head = [c.value for c in ws[1]]
    ws.cell(row=2, column=head.index("price_final") + 1, value=True)
    wb.save(path)
    with pytest.raises(ReviewError, match="cell"):
        import_review(path, data)


def test_review_percent_cell_format(tmp_path):
    from openpyxl import load_workbook

    data = _review_data()
    data.groups[0].products[0]["discount_pct"] = 10
    path = export_review(Project(), data, {}, tmp_path / "r.xlsx")
    wb = load_workbook(path)
    ws = wb["PA"]
    head = [c.value for c in ws[1]]
    c = ws.cell(row=2, column=head.index("discount_pct") + 1, value=0.1)
    c.number_format = "0%"
    wb.save(path)
    assert import_review(path, data).groups[0].products[0]["discount_pct"] == 10.0


def test_listing_recomposed_after_review(tmp_path):
    from PIL import Image

    from cca_report.config import PROJECT_ROOT
    from cca_report.extract.pipeline import recompose_listings

    shots = []
    for i, color in enumerate(("red", "blue", "green")):
        p = tmp_path / f"c{i}.png"
        Image.new("RGB", (100, 200), color).save(p)
        shots.append(p)
    g = GroupIn(
        country="PA",
        retailer="TIGO",
        brand="Samsung",
        promo_types=["Monthly Payment"],
        capture="listing",
        row_format="pa_operator",
        listing_screenshot="old.png",
        products=[{"model_name": "A", "screenshot": str(shots[2])}, {"model_name": "B", "screenshot": str(shots[0])}],
    )
    data = ReportData(week="2026-W45", cutoff_date=date(2026, 11, 6), groups=[g])
    recompose_listings(data, tmp_path)
    out = Path(data.groups[0].listing_screenshot)
    out = out if out.is_absolute() else PROJECT_ROOT / out
    img = Image.open(out)
    assert img.width == 224 and img.getpixel((10, 10)) == (0, 128, 0)  # first column = first product (green)


# ------------------------------------------------------------------ Phase 3 real cards (CR / EC / PY)
def test_bristol_plans_pick_preferred_installments():
    lines = [
        "Celular Samsung Galaxy S26 Ultra 5G 512GB 12GB RAM - Black",
        "1 x Gs. 9.479.000",
        "2 x Gs. 4.929.000",
        "15 x Gs. 929.000",
        "18 x Gs. 799.000",
        "PYG9.159.000",
    ]
    p = dom.parse_card(card(lines), Brand.SAMSUNG, preferred_installments=15)
    assert p.model_name == "Galaxy S26 Ultra"
    assert (p.price_final, p.installments_n, p.monthly_payment) == (9159000.0, 15, 929000.0)
    assert dom.parse_card(card(lines), Brand.SAMSUNG).installments_n == 18  # default: longest plan


def test_tupi_card_codes_colour_and_refurbished():
    lines = [
        "COD: 232041",
        "En Stock",
        "CELULAR APPLE IPHONE 13 PRO 128GB 5G GRAFITO CPO REACONDICIONADO LL/A2483 ver detalles",
        "Comprá al CONTADO",
        "Gs. 4.066.000",
        "Pedí en CUOTAS",
        "15x Gs. 464.000",
    ]
    p = dom.parse_card(card(lines), Brand.APPLE, preferred_installments=15)
    assert p.model_name.lower() == "iphone 13 pro" and p.condition == "refurbished"
    assert (p.price_final, p.installments_n, p.monthly_payment) == (4066000.0, 15, 464000.0)


def test_monge_struck_amount_on_shared_line():
    c = card(["Celular 5G Apple iPhone 18 Pro Max Plata 12GB RAM 256GB", "Valoración:", "0%", "₡ 899.900 ₡ 1.069.900"])
    c.struck = ["₡ 1.069.900"]
    p = dom.parse_card(c, Brand.APPLE)
    assert p.model_name == "iPhone 18 Pro Max" and p.storage == "256 GB"
    assert (p.price_final, p.price_before, p.discount_pct) == (899900.0, 1069900.0, 16)


def test_claro_cr_installments_from_label():
    c = card(
        [
            "Samsung Galaxy S26",
            "Con ESIM",
            "Precio del celular a cuotas",
            "Desde",
            "₡20,375.00a 24 meses",
            "Precio de contado",
            "₡489,000.00",
            "1 color",
            "Comprar",
        ]
    )
    p = dom.parse_card(c, Brand.SAMSUNG)
    assert (p.monthly_payment, p.installments_n, p.price_final) == (20375.0, 24, 489000.0)
    assert p.price_label is None  # "Desde" belongs to the installment, not to the cash price


def test_inverfin_promo_and_regular():
    c = card(
        [
            "-25%",
            "Ver Detalles",
            "CELULAR HONOR PLAY 10 64GB",
            "Precio de venta",
            "Gs. 765.000 PROMO",
            "Precio habitual",
            "Gs. 1.020.000 Contado",
            "18 x Gs. 67.000",
            "15 x Gs. 78.000",
        ]
    )
    p = dom.parse_card(c, Brand.HONOR, preferred_installments=15)
    assert p.model_name == "Honor Play 10"
    assert (p.price_final, p.price_before, p.discount_pct, p.monthly_payment) == (765000.0, 1020000.0, 25, 78000.0)


def test_phone_filter():
    cfg = SelectionConfig()
    from cca_report.select import is_phone

    assert is_phone("Celular Samsung Galaxy A17", cfg)
    assert is_phone("Honor 600", cfg) and is_phone("Moto G06", cfg) and is_phone("iPhone 16e", cfg)
    assert is_phone("Galaxy Z Fold8 SAMSUNG", cfg) and not is_phone("Galaxy Watch8 Classic", cfg)
    assert not is_phone("Lavarropas Samsung 19Kg Carga Superior", cfg)
    assert not is_phone("Apple Watch Series 12 GPS 46 mm", cfg)
    assert not is_phone("Tablet Honor Pad X8b Wi-Fi", cfg)


def test_struck_installment_plan_is_ignored():
    c = card(
        [
            "CELULAR SAMSUNG GALAXY A37 8GB/256GB",
            "Pedí en CUOTAS",
            "15x Gs. 327.000",
            "15x Gs. 310.000",
            "Comprá al CONTADO",
            "Gs. 2.899.000",
        ]
    )
    c.struck = ["15x Gs. 327.000"]
    p = dom.parse_card(c, Brand.SAMSUNG, preferred_installments=15)
    assert (p.monthly_payment, p.price_final) == (310000.0, 2899000.0)


def test_struck_class_value_never_becomes_current_price():
    c = card(["Galaxy A17", "$100"])
    c.struck = ["$100"]
    p = dom.parse_card(c, Brand.SAMSUNG)
    assert p.price_final is None and p.price_before == 100.0


def test_storage_slash_notation():
    assert dom._storage("Honor Magic 7 Lite 512/12GB") == "512 GB"


def test_phone_with_bundled_accessory_is_kept():
    from cca_report.select import is_phone

    name, gift = dom.clean_card_name("Apple iPhone 15 128GB + Airpods 2 Gen", Brand.APPLE)
    assert gift and is_phone(f"{name} Apple iPhone 15 128GB", SelectionConfig())


def test_zero_downpayment_survives_pipeline_product_dict():
    from cca_report.extract.pipeline import Extractor

    parsed = dom.parse_card(
        card(["Moto G06", "Abono inicial", "B/.0.00", "Pago cuotas", "B/.9.10", "24 meses"]), Brand.MOTOROLA
    )
    page = type(
        "P", (), {"final_url": "https://x", "url": "https://x", "captured_at": date(2026, 11, 6), "html": None}
    )()
    page.captured_at = __import__("datetime").datetime(2026, 11, 6, 10, 0)
    d = Extractor(Project(), Path("."))._product(
        parsed,
        card(["Moto G06"]),
        page,
        dom.ExtractMode.DOM
        if hasattr(dom, "ExtractMode")
        else __import__("cca_report.models", fromlist=["ExtractMode"]).ExtractMode.DOM,
        False,
        "pa_operator",
    )
    assert d["downpayment"] == 0.0


def test_build_refuses_stale_review(tmp_path):
    from cca_report.review import data_fingerprint, review_fingerprint

    data = _review_data()
    dj = tmp_path / "data.json"
    dj.write_text(data.model_dump_json(), encoding="utf-8")
    path = export_review(Project(), data, {}, tmp_path / "r.xlsx", fingerprint=data_fingerprint(dj))
    assert review_fingerprint(path) == data_fingerprint(dj)
    dj.write_text(data.model_dump_json() + " ", encoding="utf-8")
    assert review_fingerprint(path) != data_fingerprint(dj)
