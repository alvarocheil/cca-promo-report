"""Markdown status table for Phase 0 (runs/discovery/status.md)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

from .models import AccessResult, AccessStatus, DiscoveryResult

ICON = {
    AccessStatus.OK: "✅ ok",
    AccessStatus.JS_HEAVY: "🟡 js_heavy",
    AccessStatus.BLOCKED: "⛔ blocked",
    AccessStatus.ROBOTS: "🚫 robots",
    AccessStatus.ERROR: "❌ error",
    AccessStatus.MANUAL: "✋ manual",
}
SEVERITY = [
    AccessStatus.ROBOTS,
    AccessStatus.BLOCKED,
    AccessStatus.ERROR,
    AccessStatus.JS_HEAVY,
    AccessStatus.MANUAL,
    AccessStatus.OK,
]


def retailer_status(results: list[AccessResult]) -> AccessStatus:
    """Worst status among the base URL and its category pages."""
    found = {r.status for r in results}
    return next((s for s in SEVERITY if s in found), AccessStatus.ERROR)


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render(access: list[AccessResult], discovery: list[DiscoveryResult]) -> str:
    lines = [
        "# Phase 0 — Access & discovery status",
        "",
        f"Generated {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}",
        "",
    ]
    if access:
        by_slug: dict[str, list[AccessResult]] = {}
        for a in access:
            by_slug.setdefault(a.slug, []).append(a)
        counts = Counter(retailer_status(v) for v in by_slug.values())
        lines += [
            "## Summary by retailer",
            "",
            " · ".join(f"{ICON[s]}: {n}" for s, n in counts.most_common()),
            "",
            "| País | Retailer | Estado | URLs ok/total | Nota |",
            "|---|---|---|---|---|",
        ]
        for _slug, rows in by_slug.items():
            st = retailer_status(rows)
            ok = sum(r.status in (AccessStatus.OK, AccessStatus.JS_HEAVY) for r in rows)
            worst = next((r for r in rows if r.status == st), rows[0])
            lines.append(
                f"| {rows[0].country} | {_cell(rows[0].retailer)} | {ICON[st]} | "
                f"{ok}/{len(rows)} | {_cell(worst.detail)} |"
            )
        lines += [
            "",
            "## Detail by URL",
            "",
            "| Retailer | URL | Estado | HTTP simple | Navegador | Texto estático/render | "
            "Menciones S/A/H/M | Captura |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for a in access:
            hits = (
                "/".join(str(a.brand_hits.get(k, 0)) for k in ("Samsung", "Apple", "Honor", "Motorola"))
                if a.brand_hits
                else "–"
            )
            shot = f"[png]({a.screenshot})" if a.screenshot else "–"
            lines.append(
                f"| {a.slug} | {_cell(a.url)} | {ICON[a.status]} | {a.http_status or '–'} | "
                f"{a.browser_status or '–'} | {a.static_text_chars or 0}/{a.rendered_text_chars or 0} "
                f"| {hits} | {shot} |"
            )
    if discovery:
        lines += [
            "",
            "## Proposed URLs (discover)",
            "",
            "Not applied to `config/retailers/` until approved.",
            "",
        ]
        for d in discovery:
            lines += [f"### {d.country} · {d.retailer} — {ICON[d.status]}", ""]
            if d.detail:
                lines += [f"_{d.detail.strip()}_", ""]
            if d.category_candidates:
                lines.append("**Category candidates**")
                lines += [f"- `{c.score:+.1f}` {c.url} — {_cell(c.text) or '(no text)'}" for c in d.category_candidates]
                lines.append("")
            for b, lst in d.brand_candidates.items():
                lines.append(f"**{b.value}:** " + " · ".join(c.url for c in lst))
            lines.append("")
    return "\n".join(lines) + "\n"


def write(path: Path, access: list[AccessResult], discovery: list[DiscoveryResult]) -> Path:
    path.write_text(render(access, discovery), encoding="utf-8")
    return path
