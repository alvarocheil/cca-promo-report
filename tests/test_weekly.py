"""Phase 6: automatic week, manual prices, DRM detection, image compression, e-mail body."""

from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from cca_report.config import Project
from cca_report.extract.pipeline import ManualPricesError, read_manual_prices
from cca_report.mail import NO_INFO, compose
from cca_report.models import MailConfig, ReportConfig, friday_of
from cca_report.render.document import ReportData
from cca_report.render.pdf import compress_image
from cca_report.review import ReviewError, drm_protected, review_fingerprint

FIXTURE = Path(__file__).parent / "fixtures" / "w44_sample.json"
COUNTRY = {
    "code": "PA",
    "name": "Panama",
    "currency": "USD",
    "locale": "es-PA",
    "timezone": "America/Panama",
    "group_by": "brand",
}


def load() -> ReportData:
    return ReportData.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_auto_week_and_friday_cutoff():
    cfg = ReportConfig.model_validate({"week": "auto", "cutoff_date": "auto", "countries": [COUNTRY]})
    assert cfg.cutoff_date == friday_of(cfg.week)
    assert cfg.cutoff_auto
    assert cfg.cutoff_for("2026-W45") == date(2026, 11, 6)  # another week: its own Friday


def test_fixed_cutoff_is_kept_for_its_week():
    cfg = ReportConfig.model_validate({"week": "2026-W45", "cutoff_date": "2026-11-05", "countries": [COUNTRY]})
    assert cfg.cutoff_for("2026-W45") == date(2026, 11, 5)
    assert cfg.cutoff_for("2026-W46") == date(2026, 11, 13)


def test_send_at_format():
    with pytest.raises(ValueError):
        MailConfig(send_at="10am")


def test_manual_prices_semicolon_and_spanish_numbers(tmp_path):
    (tmp_path / "precios.csv").write_text(
        "archivo;precio_final;precio_antes;cuota_mensual;cuotas;regalo;promo\n"
        "Galaxy S26 Ultra.png;1.099,00;1.299,00;;;Galaxy Buds;\n"
        "iPhone 17;₡650.000;;₡54.167;12;;Special discount\n",
        encoding="utf-8-sig",
    )
    prices = read_manual_prices(tmp_path)
    s26 = prices["galaxy s26 ultra"]
    assert s26["price_final"] == 1099.0 and s26["price_before"] == 1299.0
    assert s26["discount_pct"] == 15 and s26["bundle_gift"] == "Galaxy Buds"
    ip = prices["iphone 17"]
    assert ip["monthly_payment"] == 54167.0 and ip["installments_n"] == 12
    assert ip["promo_type"] == "Special discount"


def test_manual_prices_invalid_value_and_drm(tmp_path):
    (tmp_path / "precios.csv").write_text("archivo,precio_final\nA.png,abc\n", encoding="utf-8")
    with pytest.raises(ManualPricesError, match="fila 2"):
        read_manual_prices(tmp_path)
    (tmp_path / "precios.csv").write_bytes(b"<## NASCA DRM FILE - VER1.00 ##>\x06\xcc")
    with pytest.raises(ManualPricesError, match="DRM"):
        read_manual_prices(tmp_path)
    assert read_manual_prices(tmp_path / "missing") == {}


def test_drm_workbook_gives_clear_error(tmp_path):
    xlsx = tmp_path / "review.xlsx"
    xlsx.write_bytes(b"<## NASCA DRM FILE - VER1.00 ##>" + b"\0" * 64)
    assert drm_protected(xlsx)
    with pytest.raises(ReviewError, match="DRM"):
        review_fingerprint(xlsx)


def test_compress_image_keeps_original(tmp_path):
    src = tmp_path / "card.png"
    Image.effect_noise((2400, 1600), 80).convert("RGB").save(src)
    before = src.read_bytes()
    out = compress_image(src, tmp_path / "cache")
    assert out != src and out.suffix == ".jpg"
    assert out.stat().st_size < src.stat().st_size
    with Image.open(out) as im:
        assert max(im.size) <= 1400
    assert src.read_bytes() == before  # traceability: the screenshot itself is untouched
    assert compress_image(src, tmp_path / "cache") == out  # cached


def test_email_body_has_every_country_and_escapes(tmp_path):
    data = load()
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.7 tiny")
    cfg = MailConfig(to=["me@example.com"])
    status = [
        {"country": "PA", "retailer": "TIGO", "brand": "Samsung", "status": "blocked", "products": 0},
        {"country": "CR", "retailer": "MONGE", "brand": "Apple", "status": "ok", "products": 4},
    ]
    email = compose(Project(), data, status, pdf, cfg)
    assert "Week 44" in email.subject
    for name in ("Panama", "Costa Rica", "Ecuador", "Paraguay", "Uruguay"):
        assert name in email.html
    assert "<strong>" in email.html or NO_INFO in email.html
    assert "<script" not in email.html
    assert "TIGO · Samsung" in email.html and "blocked" in email.html
    assert email.attachments == [pdf]


def test_email_skips_oversized_pdf(tmp_path):
    pdf = tmp_path / "big.pdf"
    pdf.write_bytes(b"0" * 2_000_000)
    email = compose(Project(), load(), [], pdf, MailConfig(to=["x@example.com"], max_attachment_mb=1))
    assert email.attachments == [] and "exceeds" in email.notes[0]
    no_status = compose(Project(), load(), [], pdf, MailConfig(to=["x@example.com"], include_run_status=False))
    assert "Coverage (internal)" not in no_status.html


def test_manual_prices_rejects_partial_numbers_and_extra_columns(tmp_path):
    csv_path = tmp_path / "precios.csv"
    csv_path.write_text("archivo,precio_final,cuotas\nA.png,1099,95,12\n", encoding="utf-8")
    with pytest.raises(ManualPricesError, match="más valores"):
        read_manual_prices(tmp_path)
    csv_path.write_text("archivo;precio_final\nA.png;100abc\n", encoding="utf-8")
    with pytest.raises(ManualPricesError, match="precio_final"):
        read_manual_prices(tmp_path)
    csv_path.write_text("archivo;cuotas\nA.png;12.5\n", encoding="utf-8")
    with pytest.raises(ManualPricesError, match="cuotas"):
        read_manual_prices(tmp_path)
    csv_path.write_text("archivo;precio_final\nA.png;USD $1.299,00\n", encoding="cp1252")
    assert read_manual_prices(tmp_path)["a"]["price_final"] == 1299.0
