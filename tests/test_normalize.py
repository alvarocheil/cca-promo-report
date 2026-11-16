import pytest

from cca_report.models import Brand, CountryCode, Product
from cca_report.normalize import (
    clean_model_name,
    duplicate_columns,
    money,
    parse_amount,
    product_flags,
    promo_type_ok,
    row_texts,
)


def P(**kw) -> Product:
    base = dict(country=CountryCode.PA, retailer="X", brand=Brand.SAMSUNG, model_name="Galaxy A17", currency="USD")
    return Product(**{**base, **kw})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("₡1.209.900", 1_209_900),  # dot thousands (CR sites)
        ("₡1,329,900.00", 1_329_900),
        ("PYG 2.299.000", 2_299_000),
        ("USD $1,240.00", 1240.0),
        ("$ 802,33", 802.33),  # comma decimal
        ("1.240,50", 1240.5),
        ("USD 798.33", 798.33),
        ("Gs. 15 x 230.000", 15),  # first number wins
        ("no price", None),
        (None, None),
        (579, 579.0),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_parse_amount_pyg_never_decimal():
    assert parse_amount("2.29", decimals_allowed=False) == 229


@pytest.mark.parametrize(
    ("raw", "fixed"),
    [("¡Phone 17 Pro", "iPhone 17 Pro"), ("IPHONE 17e", "iPhone 17e"), ("galaxy  s26", "Galaxy s26")],
)
def test_clean_model_name(raw, fixed):
    assert clean_model_name(raw) == fixed


def test_money_styles():
    assert money(1240, "usd_long") == "USD $1,240.00"
    assert money(1240, "usd_short") == "$1,240.00"
    assert money(1329900, "crc") == "₡1,329,900.00"
    assert money(2299000, "pyg") == "PYG 2,299,000"
    assert money(649, "usd_uy") == "USD 649"
    assert money(798.33, "usd_uy") == "USD 798.33"
    assert money(1199, "usd_uy") == "USD 1,199"
    assert money(3665, "uyu") == "UYU 3,665"


# --------------------------------------------------------------- §5 table, row by row
def test_pa_operator():
    t = row_texts(P(downpayment=352, monthly_payment=37, installments_n=24, price_final=1240), "pa_operator")
    assert t.row2 == ["Downpayment: USD $352.00", "Monthly payment: USD $37.00 x 24 cuotas"]
    assert t.row3 == "$1,240.00"
    t = row_texts(P(price_final=355, price_label="Payment in full"), "pa_operator")
    assert t.row3 == "Payment in full $355.00"


def test_pa_retailer_discount_and_no_discount():
    t = row_texts(P(price_before=269.95, price_final=199.95, monthly_payment=11.89, installments_n=18), "pa_retailer")
    assert t.row2 == ["Before: USD $269.95 / After: USD $199.95", "Monthly payment: USD $11.89 x 18 cuotas"]
    assert t.row3 == "$199.95"
    t = row_texts(P(price_final=1749.6, monthly_payment=48.6, installments_n=36), "pa_retailer")
    assert t.row2 == ["Downpayment: USD $0.00", "Monthly payment: USD $48.60 x 36 meses"]


def test_cr_default():
    p = P(country=CountryCode.CR, currency="CRC", price_label="Pre Paid", price_final=1329900, monthly_payment=55412.5)
    t = row_texts(p, "cr_default")
    assert t.row2 == ["Pre Paid ₡1,329,900.00", "Monthly payment ₡55,412.50"]
    assert t.row3 == "₡1,329,900.00"
    t = row_texts(P(currency="CRC", price_final=477000, bundle_gift="Galaxy Buds3 FE"), "cr_default")
    assert t.row2 == ["Bundle Promotion: Gift Galaxy Buds3 FE"]


def test_ec_formats():
    assert row_texts(P(price_final=1368.18, bundle_gift="Fast Charger 25W + case"), "ec_claro").row2 == [
        "Fast Charger 25W + case"
    ]
    t = row_texts(P(price_final=802.33, installments_n=18), "ec_tigo")
    assert t.row2 == ["Renewal up to 18 months for the post paid plan of: $802.33"]
    assert t.row3 == "$802.33"


def test_py_default():
    p = P(
        currency="PYG",
        price_before=2669000,
        discount_pct=14,
        monthly_payment=230000,
        installments_n=15,
        price_final=2299000,
    )
    t = row_texts(p, "py_default")
    assert t.row2 == ["Was 2,669,000 · 14%", "15 × PYG 230,000"]
    assert t.row3 == "PYG 2,299,000 cash price"


def test_uy_formats():
    t = row_texts(P(price_before=649, discount_pct=10, price_final=579), "uy_cover")
    assert t.row2 == ["10% advertised. Was USD 649"] and t.row3 == "USD 579"
    assert row_texts(P(price_final=1269, price_label="From"), "uy_cover").row3 == "From USD 1,269"
    assert row_texts(P(price_final=900, condition="open-box"), "uy_cover").row2 == ["Open-box, lightly used"]
    t = row_texts(
        P(
            discount_pct=36,
            coupon_price=798.33,
            monthly_payment=3665,
            installments_n=10,
            secondary_currency="UYU",
            price_final=885.29,
        ),
        "uy_ml",
    )
    assert t.row2 == ["36%. With coupon USD 798.33", "10 × UYU 3,665 interest-free"]
    assert t.row3 == "USD 885.29"


def test_unknown_row_format():
    with pytest.raises(ValueError, match="unknown row_format"):
        row_texts(P(price_final=1), "xx")


# --------------------------------------------------------------- validations (W44 cases)
def test_installments_must_match_price():
    ok = P(downpayment=352, monthly_payment=37, installments_n=24, price_final=1240)
    bad = P(downpayment=245.55, monthly_payment=35, installments_n=24, price_final=1585.55)  # real W44 error
    assert product_flags(ok, "pa_operator") == []
    assert any(f.startswith("installments_mismatch") for f in product_flags(bad, "pa_operator"))


def test_retailer_financing_surcharge_allowed():
    p = P(price_before=269.95, price_final=199.95, monthly_payment=11.89, installments_n=18)  # +7%
    assert product_flags(p, "pa_retailer") == []


def test_monthly_equal_downpayment_flagged():
    p = P(downpayment=144.95, monthly_payment=144.95, installments_n=24, price_final=3623.75)
    assert "monthly_equals_downpayment" in product_flags(p, "pa_operator")


def test_brand_mismatch_flagged():
    p = P(brand=Brand.HONOR, model_name="Galaxy Magic8 Pro", price_final=999)
    assert "brand_mismatch:Samsung" in product_flags(p, "pa_operator")


def test_foreign_currency_installments_not_checked():
    p = P(monthly_payment=3665, installments_n=10, secondary_currency="UYU", price_final=885.29)
    assert product_flags(p, "uy_ml") == []


def test_placeholder_promo_type_and_duplicates():
    assert not promo_type_ok("XXXXX")
    assert not promo_type_ok("")
    assert promo_type_ok("Monthly Payment")
    cols = [("A", ("x",), "$1"), ("B", ("y",), "$2"), ("A", ("x",), "$1")]
    assert duplicate_columns(cols) == {2}


def test_parse_amount_does_not_glue_separate_numbers():
    assert parse_amount("1,240 18 cuotas") == 1240
    assert parse_amount("₡ 1 329 900") == 1_329_900


def test_zero_values_are_validated():
    p = P(downpayment=0, monthly_payment=0, installments_n=12, price_final=120)
    assert any(f.startswith("installments_mismatch") for f in product_flags(p, "pa_operator"))
    assert "invalid_installments_n" in product_flags(P(installments_n=0, price_final=1), "pa_operator")


def test_pa_retailer_without_promo_does_not_invent_downpayment():
    t = row_texts(P(price_final=1399.95), "pa_retailer")
    assert t.row2 == ["Regular price"] and t.row3 == "$1,399.95"
