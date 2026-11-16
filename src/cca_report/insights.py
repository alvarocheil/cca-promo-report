"""Insights table Brand x Retailer (+ optional "Key insights" bullets).

Two providers, both fed only with the extracted (and later reviewed) data:

* ``rules``: deterministic texts from promo fields;
* ``codex``: ``codex exec`` in read-only mode, in an empty folder, with a JSON output schema.
  Every Codex cell is verified: pairs without data are forced to "No information available."
  and a cell quoting a number that is not in the data is replaced by the rule text.

Whatever the provider, the texts land in the editable Insights sheet of review.xlsx.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Project
from .models import BRAND_ORDER, RetailerType
from .render.document import GroupIn, ReportData

log = logging.getLogger(__name__)
NO_INFO = "No information available."


# --------------------------------------------------------------------------- rules
def _names(models: list[str], n: int = 3) -> str:
    short = [m.replace("Galaxy ", "", 1) if i else m for i, m in enumerate(models[:n])]
    if len(short) <= 1:
        return "".join(short)
    return ", ".join(short[:-1]) + " and " + short[-1]


def rule_text(g: GroupIn) -> str:
    """12-25 words, keyword in bold (spec §3.3)."""
    if g.kind == "branding":
        return "**Branding campaign:** Home page communication featuring the brand."
    prods = g.products
    if not any(p.get("price_final") is not None for p in prods):
        return NO_INFO  # manual rows still empty: never describe data we do not have
    models = [p.get("model_name", "") for p in prods if p.get("model_name")]
    parts: list[str] = []
    gifts = [p for p in prods if p.get("bundle_gift")]
    if gifts:
        parts.append(f"**Bundle promotion:** {gifts[0]['bundle_gift']} with the purchase of {gifts[0]['model_name']}.")
    discounts = [p.get("discount_pct") or 0 for p in prods]
    if any(discounts):
        parts.append(
            f"**Price discounts** of up to {max(discounts):.0f}% on selected models, including {_names(models)}."
        )
    months = [
        p.get("installments_n") for p in prods if p.get("monthly_payment") is not None and p.get("installments_n")
    ]
    if months:
        financed = [p for p in prods if p.get("monthly_payment") is not None]
        # only claim "no downpayment" when every financed product shows an explicit 0
        no_down = all(p.get("downpayment") is not None and p["downpayment"] == 0 for p in financed)
        extra = " with no downpayment" if no_down else ""
        parts.append(f"**Monthly installment plans** of up to {max(months)} months{extra}.")
    if not parts and g.promo_types:  # promotion fixed by config (e.g. Tigo EC plan-renewal "Special discount")
        parts.append(f"**{', '.join(g.promo_types)}** on selected models, including {_names(models)}.")
    if not parts:  # spec §4: no promotion -> "No information available."
        return NO_INFO
    text = " ".join(parts)
    words = text.split()
    return text if len(words) <= 30 else " ".join(words[:28]).rstrip(",") + "."


def rule_insights(project: Project, data: ReportData) -> dict[str, dict[str, dict[str, str]]]:
    out: dict[str, dict[str, dict[str, str]]] = {}
    by_name = {r.retailer_name.casefold(): r for r in project.retailers}
    for c in project.report.countries:
        groups = [g for g in data.groups if g.country == c.code]
        if not groups:
            continue
        retailers = [
            name
            for name in (c.retailer_order or dict.fromkeys(g.retailer for g in groups))
            if not (by_name.get(name.casefold()) and by_name[name.casefold()].type == RetailerType.BRAND)
        ]
        table: dict[str, dict[str, str]] = {}
        for b in BRAND_ORDER:
            row = {}
            for rname in retailers:
                gs = [g for g in groups if g.brand == b and g.retailer.casefold() == rname.casefold()]
                texts = [rule_text(g) for g in gs]
                row[rname] = " ".join(t for t in texts if t != NO_INFO) or NO_INFO
            table[b.value] = row
        table["(subtitle)"] = {"-": c.event_caption}
        out[c.code.value] = table
    return out


# --------------------------------------------------------------------------- Codex
PROMPT = """You write the "Insights" table of a weekly competitor smartphone promotions report (Central America).
Language: English. Audience: brand managers. Use ONLY the JSON data below: never invent models, numbers,
dates or promotions. Do not read files or run commands; answer directly.

For EVERY (brand, retailer) pair listed in "pairs" write one cell of 12-25 words that summarises the
promotion types and financing seen in the data for that pair, starting with the key promotion in bold
markdown, for example: "**Bundle promotion:** Free Galaxy Buds3 FE with the purchase of Galaxy S26 FE.
Monthly installment options." Mention at most 3 models. If a pair has no data write exactly
"No information available." Any number you write (percentages, months) must appear in the data.
Describe only what the data shows: never write that something is missing, unspecified or not listed.
{bullets_rule}
Return JSON matching the schema.

DATA:
{data}
"""
BULLETS_ON = (
    'Also write 3-5 "Key insights" bullets per brand that has data (each under 20 words, comparing '
    "retailers). Brands without data get an empty items list."
)
BULLETS_OFF = "Return an empty bullets list."

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cells", "bullets"],
    "properties": {
        "cells": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["brand", "retailer", "text"],
                "properties": {
                    "brand": {"type": "string"},
                    "retailer": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
        "bullets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["brand", "items"],
                "properties": {"brand": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}},
            },
        },
    },
}
_FIELDS = (
    "model_name",
    "price_final",
    "price_before",
    "discount_pct",
    "downpayment",
    "monthly_payment",
    "installments_n",
    "coupon_price",
    "bundle_gift",
    "badge_text",
    "condition",
    "price_label",
)
_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def country_payload(project: Project, data: ReportData, code: str, retailers: list[str]) -> dict:
    groups = []
    for g in data.groups:
        if g.country.value != code or g.kind == "overview" or g.brand is None:
            continue
        groups.append(
            {
                "retailer": g.retailer,
                "brand": g.brand.value,
                "kind": g.kind,
                "promo_types": g.promo_types,
                "caption": g.caption,
                "products": [_product_facts(p, g.row_format) for p in g.products],
            }
        )
    pairs = [{"brand": b.value, "retailer": r} for b in BRAND_ORDER for r in retailers]
    country = project.country(code)  # type: ignore[arg-type]
    return {"country": country.name, "currency": country.currency, "pairs": pairs, "groups": groups}


def _product_facts(p: dict, row_format: str) -> dict:
    facts = {k: p[k] for k in _FIELDS if not (p.get(k) is None or p.get(k) == "" or p.get(k) is False)}
    if row_format == "ec_tigo" and "installments_n" in facts:  # "Renueva hasta 18 meses": not financing
        facts["plan_renewal_months"] = facts.pop("installments_n")
    return facts


_GIFT_WORDS = re.compile(r"\b(free|gift|bundle|includes?|complimentary|bonus)\b", re.I)


def _pair_ok(text: str, groups: list[dict]) -> tuple[bool, str]:
    """A cell may only use facts of its own brand x retailer groups."""
    if not numbers_supported(text, {"groups": groups}):
        return False, "number not in the data of this brand/retailer"
    has_gift = any(p.get("bundle_gift") for g in groups for p in g["products"])
    if (
        _GIFT_WORDS.search(text)
        and not has_gift
        and not any("bundle" in t.lower() for g in groups for t in g["promo_types"])
    ):
        return False, "gift/bundle claim without a gift in the data"
    return True, ""


def _norm_num(n: str) -> str:
    n = n.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n


def numbers_supported(text: str, payload: dict) -> bool:
    """Every number in a generated text must exist somewhere in the source data."""
    blob = json.dumps(payload, ensure_ascii=False)
    allowed = {_norm_num(n) for n in _NUM.findall(blob)}
    return all(_norm_num(n) in allowed for n in _NUM.findall(text))


def _codex_bin() -> str | None:
    return shutil.which("codex") or shutil.which("codex.cmd")


def _run_tree(cmd: list[str], stdin_text: str, timeout: int, workdir: Path) -> tuple[int, str]:
    """Run with a hard timeout that kills the whole process tree (codex.cmd -> node -> native on
    Windows); output goes to files so orphaned grandchildren cannot keep pipes open."""
    import os
    import sys

    log_path = workdir / "codex.log"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
        try:
            proc.communicate(stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
            else:
                import signal

                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            raise RuntimeError(f"codex timed out after {timeout} s") from None
    return proc.returncode, log_path.read_text(encoding="utf-8", errors="replace")


def run_codex(payload: dict, workdir: Path, bullets: bool, timeout: int) -> dict:
    exe = _codex_bin()
    if not exe:
        raise RuntimeError("codex CLI not found on PATH")
    workdir.mkdir(parents=True, exist_ok=True)
    schema = workdir / "schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    out = workdir / "answer.json"
    out.unlink(missing_ok=True)
    prompt = PROMPT.format(
        bullets_rule=BULLETS_ON if bullets else BULLETS_OFF, data=json.dumps(payload, ensure_ascii=False, indent=1)
    )
    (workdir / "prompt.txt").write_text(prompt, encoding="utf-8")  # traceability
    cmd = [exe, "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral", "-C", str(workdir),
           "--output-schema", str(schema), "-o", str(out), "-"]  # fmt: skip
    rc, err = _run_tree(cmd, prompt, timeout, workdir)
    if rc != 0 or not out.exists():
        raise RuntimeError(f"codex exit {rc}: {err[-300:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def merge_codex(
    table: dict[str, dict[str, str]], payload: dict, answer: dict, code: str, bullets_enabled: bool = True
) -> list[str]:
    """Apply a Codex answer onto the rule table, cell by cell, with the anti-invention checks:
    pairs without data -> NO_INFO; numbers and gift claims must come from that pair's own data."""
    notes: list[str] = []
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for g in payload["groups"]:
        by_pair.setdefault((g["brand"], g["retailer"].casefold()), []).append(g)
    for cell in answer.get("cells", []):
        brand, retailer, text = cell.get("brand"), cell.get("retailer"), str(cell.get("text", "")).strip()
        if brand not in table or retailer not in table[brand] or not text:
            continue
        groups = by_pair.get((brand, retailer.casefold()))
        if not groups:
            table[brand][retailer] = NO_INFO
            continue
        if table[brand][retailer] == NO_INFO and text != NO_INFO:
            notes.append(f"{code} {brand}/{retailer}: no promotion in the data, Codex text discarded")
            continue
        ok, why = _pair_ok(text, groups)
        if ok:
            table[brand][retailer] = text
        else:
            notes.append(f"{code} {brand}/{retailer}: {why}; rule text kept")
    if not bullets_enabled:
        return notes
    for b in answer.get("bullets", []):
        brand = b.get("brand")
        groups = [g for (gb, _), gs in by_pair.items() if gb == brand for g in gs]
        promo = brand in table and any(t != NO_INFO for k, t in table[brand].items() if not k.startswith("("))
        if not groups or not promo:
            continue  # no evidence for this brand in the country: no bullets
        items = [t.strip() for t in b.get("items", []) if t.strip() and _pair_ok(t, groups)[0]]
        for i, t in enumerate(items[:5], 1):
            table[brand][f"(bullet {i})"] = t
    return notes


def codex_insights(project: Project, data: ReportData, run_dir: Path) -> tuple[dict, list[str]]:
    base = rule_insights(project, data)
    notes: list[str] = []
    timeout = project.report.codex_timeout_seconds

    def one(code: str) -> tuple[str, dict, dict]:
        retailers = list(base[code][BRAND_ORDER[0].value])
        payload = country_payload(project, data, code, retailers)
        bullets = project.country(code).insights_bullets  # type: ignore[arg-type]
        return code, payload, run_codex(payload, run_dir / "insights" / code, bullets, timeout)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {code: pool.submit(one, code) for code in base}
        for code, fut in futures.items():
            try:
                _, payload, answer = fut.result()
            except Exception as exc:
                notes.append(f"{code}: Codex failed ({type(exc).__name__}: {str(exc)[:160]}); rules used")
                continue
            bullets = project.country(code).insights_bullets  # type: ignore[arg-type]
            notes += merge_codex(base[code], payload, answer, code, bullets)
            notes.append(f"{code}: insights written by Codex and verified against the data")
    return base, notes


def build_insights(project: Project, data: ReportData, run_dir: Path) -> tuple[dict, list[str], str]:
    """Returns (table, notes, provider actually used)."""
    if project.report.insights_provider == "codex":
        table, notes = codex_insights(project, data, run_dir)
        ok = sum("written by Codex" in n for n in notes)
        used = "codex" if ok == len(table) else ("codex + rules" if ok else "rules (Codex unavailable)")
        return table, notes, used
    return rule_insights(project, data), [], "rules"
