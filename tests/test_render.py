import asyncio
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from cca_report.config import Project
from cca_report.render.document import DocumentBuilder, ReportData
from cca_report.render.pdf import build_pdf, md_bold

FIXTURE = Path(__file__).parent / "fixtures" / "w44_sample.json"


def load(**overrides) -> ReportData:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data.update(overrides)
    return ReportData.model_validate(data)


def test_document_order_and_counts(tmp_path):
    res = DocumentBuilder(Project(), load(), tmp_path).build()
    kinds = [s.kind for s in res.slides]
    assert kinds[0] == "cover"
    assert kinds.count("divider") == 5 and kinds.count("insights") == 2
    assert len(res.slides) == 15
    # PA detail slides follow MAS MOVIL, TIGO, PANAFOTO order (group_by brand)
    pa = [s.retailer for s in res.slides if s.country == "PA" and s.kind == "detail"]
    assert pa == ["TIGO", "PANAFOTO"]
    assert res.slides[0].date_label == "October 30th, 2026"


def test_missing_insights_cells_say_no_information(tmp_path):
    res = DocumentBuilder(Project(), load(), tmp_path).build()
    cr = next(s for s in res.slides if s.id == "ins-CR")
    honor = dict(cr.insights.rows)["Honor"]
    assert honor == ["No information available."] * 5


def _group(n: int, country="CR", **kw):
    return {
        "country": country,
        "retailer": "MONGE",
        "brand": "Samsung",
        "promo_types": ["Price Discount"],
        "capture": "cards",
        "row_format": "cr_default" if country == "CR" else "py_default",
        "products": [{"model_name": f"Galaxy A{10 + i}", "price_final": 100000 + i} for i in range(n)],
        **kw,
    }


def test_pagination_more_than_six_products_is_balanced(tmp_path):
    data = load(groups=[_group(8)], countries={})
    slides = [s for s in DocumentBuilder(Project(), data, tmp_path).build().slides if s.kind == "detail"]
    assert [len(s.columns) for s in slides] == [4, 4]
    assert [s.page_label for s in slides] == ["1/2", "2/2"]


def test_paraguay_three_per_page(tmp_path):
    data = load(groups=[_group(5, country="PY")], countries={})
    slides = [s for s in DocumentBuilder(Project(), data, tmp_path).build().slides if s.kind == "detail"]
    assert [len(s.columns) for s in slides] == [3, 2]


def test_group_by_retailer_orders_retailer_first(tmp_path):
    groups = [
        {**_group(1, country="PY"), "retailer": "Tupi", "brand": "Honor"},
        {**_group(1, country="PY"), "retailer": "Bristol", "brand": "Apple"},
        {**_group(1, country="PY"), "retailer": "Tupi", "brand": "Samsung"},
    ]
    data = load(groups=groups, countries={"PY": {"retailer_order": ["Tupi", "Bristol"]}})
    slides = [s for s in DocumentBuilder(Project(), data, tmp_path).build().slides if s.kind == "detail"]
    assert [(s.retailer, s.heading_right) for s in slides] == [
        ("Tupi", "Samsung"),
        ("Tupi", "Honor"),
        ("Bristol", "Apple"),
    ]


def test_md_bold_escapes_html():
    assert str(md_bold("**Bundle promotion:** <b>x</b>")) == "<strong>Bundle promotion:</strong> &lt;b&gt;x&lt;/b&gt;"


@pytest.mark.slow
def test_pdf_render_no_overflow_and_bookmarks(tmp_path):
    long_text = "Bundle Promotion: Gift Galaxy Buds3 FE, 45W charger, protective case and screen protector"
    stress = _group(6)
    for p in stress["products"]:
        p.update(bundle_gift=long_text, badge_text=long_text, monthly_payment=55412.5, installments_n=24)
    data = load()
    data.groups.append(type(data.groups[0]).model_validate(stress))
    builder = DocumentBuilder(Project(), data, tmp_path / "shots")
    outcome, _ = asyncio.run(build_pdf(builder, tmp_path / "report.pdf", 44))
    assert outcome.overflow == []
    reader = PdfReader(str(outcome.pdf))
    assert len(reader.pages) == outcome.pages
    w, h = float(reader.pages[0].mediabox.width) / 72, float(reader.pages[0].mediabox.height) / 72
    assert (round(w, 2), round(h, 2)) == (13.33, 7.5)
    assert [o.title for o in reader.outline if not isinstance(o, list)] == [
        "PANAMA",
        "COSTA RICA",
        "ECUADOR",
        "PARAGUAY",
        "URUGUAY",
    ]


def test_rebuild_is_idempotent(tmp_path):
    builder = DocumentBuilder(Project(), load(), tmp_path)
    first = builder.build().model_dump()
    assert builder.build().model_dump() == first
    assert load().countries["CR"].insights.retailers == ["CLARO", "LIBERTY", "MONGE", "GOLLO", "KOLBI"]


def test_each_promo_type_is_validated(tmp_path):
    g = _group(1, promo_types=["Monthly Payment", "XXXXX"])
    res = DocumentBuilder(Project(), load(groups=[g], countries={}), tmp_path).build()
    assert any("promotion type" in f for f in res.flags)


def test_product_cannot_override_group_brand(tmp_path):
    g = _group(1)
    g["products"][0].update(brand="Apple", model_name="iPhone 17")
    res = DocumentBuilder(Project(), load(groups=[g], countries={}), tmp_path).build()
    col = next(s for s in res.slides if s.kind == "detail").columns[0]
    assert "group_conflict:brand" in col.flags and "brand_mismatch:Apple" in col.flags


@pytest.mark.parametrize(
    "bad", ["https://evil.test/x.png", "file:///C:/Windows/win.ini", "data:image/png;base64,AA", "../../outside.png"]
)
def test_image_paths_are_restricted(bad):
    from cca_report.render.pdf import RenderError, _to_uri

    with pytest.raises(RenderError):
        _to_uri(bad)


@pytest.mark.slow
def test_overflow_forces_multiple_split_rounds(tmp_path):
    huge = " ".join(["Bundle Promotion: Gift Galaxy Buds3 FE plus charger and case"] * 7)
    g = _group(6)
    for p in g["products"]:
        p["bundle_gift"] = huge
    builder = DocumentBuilder(Project(), load(groups=[g], countries={}), tmp_path / "shots")
    outcome, _ = asyncio.run(build_pdf(builder, tmp_path / "r.pdf", 44))
    assert outcome.rounds >= 2 and outcome.overflow == []
    detail = [s for s in builder.build().slides if s.kind == "detail"]
    assert sum(len(s.columns) for s in detail) == 6 and len(detail) >= 2  # no product lost


@pytest.mark.slow
def test_unresolvable_overflow_raises(tmp_path):
    from cca_report.render.pdf import RenderError

    g = _group(1)
    g["products"][0]["bundle_gift"] = "word " * 800
    builder = DocumentBuilder(Project(), load(groups=[g], countries={}), tmp_path / "shots")
    with pytest.raises(RenderError, match="overflows"):
        asyncio.run(build_pdf(builder, tmp_path / "r.pdf", 44))
