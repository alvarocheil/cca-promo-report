from datetime import date

from cca_report.config import Project
from cca_report.insights import NO_INFO, country_payload, merge_codex, numbers_supported, rule_insights
from cca_report.render.document import GroupIn, ReportData


def _data() -> ReportData:
    g = GroupIn(
        country="CR",
        retailer="MONGE",
        brand="Samsung",
        promo_types=["Price Discount"],
        capture="listing",
        row_format="cr_default",
        products=[
            {"model_name": "Galaxy Z Fold 7", "price_final": 999900, "price_before": 1269900, "discount_pct": 21}
        ],
    )
    return ReportData(week="2026-W45", cutoff_date=date(2026, 11, 6), groups=[g])


def test_numbers_supported():
    payload = {"groups": [{"products": [{"discount_pct": 21, "installments_n": 24, "price_final": 999900.0}]}]}
    assert numbers_supported("**Price discounts** of up to 21% with 24 months.", payload)
    assert not numbers_supported("**Price discounts** of up to 35% on selected models.", payload)


def test_merge_codex_guards_against_invention():
    p = Project()
    data = _data()
    table = rule_insights(p, data)["CR"]
    payload = country_payload(p, data, "CR", list(table["Samsung"]))
    answer = {
        "cells": [
            {"brand": "Samsung", "retailer": "MONGE", "text": "**Price discounts:** up to 21% on Galaxy Z Fold 7."},
            {"brand": "Apple", "retailer": "MONGE", "text": "**Bundle promotion:** free AirPods."},  # no data
            {"brand": "Samsung", "retailer": "CLARO", "text": "**Monthly plans** for 36 months."},  # no data
        ],
        "bullets": [{"brand": "Samsung", "items": ["Monge leads with 21% off.", "Up to 50% elsewhere."]}],
    }
    notes = merge_codex(table, payload, answer, "CR")
    assert table["Samsung"]["MONGE"].startswith("**Price discounts:** up to 21%")
    assert table["Apple"]["MONGE"] == NO_INFO
    assert table["Samsung"]["CLARO"] == NO_INFO
    assert table["Samsung"]["(bullet 1)"] == "Monge leads with 21% off."
    assert "(bullet 2)" not in table["Samsung"]  # 50% is not in the data
    assert isinstance(notes, list)


def test_merge_codex_rejects_unsupported_number_and_keeps_rule_text():
    p = Project()
    data = _data()
    table = rule_insights(p, data)["CR"]
    rule = table["Samsung"]["MONGE"]
    payload = country_payload(p, data, "CR", list(table["Samsung"]))
    notes = merge_codex(
        table,
        payload,
        {
            "cells": [{"brand": "Samsung", "retailer": "MONGE", "text": "**Price discounts** up to 40% this week."}],
            "bullets": [],
        },
        "CR",
    )
    assert table["Samsung"]["MONGE"] == rule and any("number not in the data" in n for n in notes)


def _two_brand_data() -> ReportData:
    mk = lambda brand, prods, promo: GroupIn(  # noqa: E731
        country="CR",
        retailer="MONGE",
        brand=brand,
        promo_types=promo,
        capture="listing",
        row_format="cr_default",
        products=prods,
    )
    return ReportData(
        week="2026-W45",
        cutoff_date=date(2026, 11, 6),
        groups=[
            mk(
                "Samsung",
                [{"model_name": "Galaxy S26 Ultra", "price_final": 899900, "price_before": 919900, "discount_pct": 2}],
                ["Price Discount"],
            ),
            mk(
                "Motorola",
                [{"model_name": "Moto G06", "price_final": 54900, "price_before": 79900, "discount_pct": 31}],
                ["Price Discount"],
            ),
        ],
    )


def test_number_from_another_brand_is_rejected():
    p = Project()
    data = _two_brand_data()
    table = rule_insights(p, data)["CR"]
    rule = table["Samsung"]["MONGE"]
    payload = country_payload(p, data, "CR", list(table["Samsung"]))
    merge_codex(
        table,
        payload,
        {
            "cells": [
                {"brand": "Samsung", "retailer": "MONGE", "text": "**Price discounts:** 31% off Galaxy S26 Ultra."}
            ],
            "bullets": [],
        },
        "CR",
    )
    assert table["Samsung"]["MONGE"] == rule  # 31% belongs to Motorola


def test_invented_gift_is_rejected_and_bullets_need_evidence():
    p = Project()
    data = _two_brand_data()
    table = rule_insights(p, data)["CR"]
    rule = table["Samsung"]["MONGE"]
    payload = country_payload(p, data, "CR", list(table["Samsung"]))
    merge_codex(
        table,
        payload,
        {
            "cells": [
                {"brand": "Samsung", "retailer": "MONGE", "text": "**Bundle promotion:** free Buds with S26 Ultra."}
            ],
            "bullets": [
                {"brand": "Apple", "items": ["Apple leads with iPhone deals."]},
                {"brand": "Samsung", "items": ["Monge cuts Galaxy S26 Ultra by 2%."]},
            ],
        },
        "CR",
    )
    assert table["Samsung"]["MONGE"] == rule
    assert not any(k.startswith("(bullet") for k in table["Apple"])  # no Apple data in CR
    assert table["Samsung"]["(bullet 1)"] == "Monge cuts Galaxy S26 Ultra by 2%."
    table2 = rule_insights(p, data)["CR"]
    merge_codex(
        table2,
        payload,
        {"cells": [], "bullets": [{"brand": "Samsung", "items": ["Monge cuts Galaxy S26 Ultra by 2%."]}]},
        "CR",
        bullets_enabled=False,
    )
    assert "(bullet 1)" not in table2["Samsung"]


def test_renewal_terms_are_not_sent_as_installments():
    g = GroupIn(
        country="EC",
        retailer="TIGO",
        brand="Samsung",
        promo_types=["Special discount"],
        capture="cards",
        row_format="ec_tigo",
        products=[{"model_name": "Galaxy A57", "price_final": 802.33, "installments_n": 18}],
    )
    data = ReportData(week="2026-W45", cutoff_date=date(2026, 11, 6), groups=[g])
    payload = country_payload(Project(), data, "EC", ["CLARO", "TIGO"])
    prod = payload["groups"][0]["products"][0]
    assert "installments_n" not in prod and prod["plan_renewal_months"] == 18
