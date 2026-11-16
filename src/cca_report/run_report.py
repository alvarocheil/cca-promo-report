"""runs/<week>/run_report.md: what was captured, what was blocked, what needs a human."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT, Project
from .render.document import cutoff_label

ICON = {
    "ok": "✅",
    "manual": "✋",
    "manual_pending": "📥",
    "empty": "▫️",
    "blocked": "⛔",
    "robots": "🚫",
    "error": "❌",
}


def _rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def write_run_report(
    project: Project,
    week: str,
    run: Path,
    steps: list[tuple[str, float, str]],
    flags: list[str] | None = None,
    pdf: Path | None = None,
) -> Path:
    data_path = run / "data.json"
    status = json.loads(data_path.read_text(encoding="utf-8")).get("status", []) if data_path.exists() else []
    notes_path = run / "insights_notes.json"
    ins = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else {}
    counts = Counter(s["status"] for s in status)
    products = sum(s.get("products", 0) for s in status)

    lines = [
        f"# Run report — {week}",
        "",
        f"- Generado: {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}",
        f"- Fecha de corte: {cutoff_label(project.report.cutoff_for(week))}",
        f"- Productos extraídos: **{products}** en {len(status)} combinaciones retailer × marca",
        "- Estados: " + " · ".join(f"{ICON.get(k, '')} {k}: {v}" for k, v in counts.most_common()),
        f"- Insights: {ins.get('provider', 'n/a')}",
    ]
    if pdf:
        lines.append(f"- PDF: `{_rel(pdf)}`")
    lines += ["", "## Pasos", "", "| Paso | Duración | Resultado |", "|---|---|---|"]
    lines += [f"| {name} | {secs:,.0f} s | {res} |" for name, secs, res in steps]

    todo = [s for s in status if s["status"] in ("manual_pending", "blocked", "robots", "error")]
    lines += ["", "## Requiere acción humana", ""]
    if todo:
        lines += ["| País | Retailer | Marca | Estado | Qué hacer |", "|---|---|---|---|---|"]
        for s in todo:
            lines.append(
                f"| {s['country']} | {s['retailer']} | {s['brand']} | {ICON.get(s['status'], '')} {s['status']} | "
                f"{s['detail'].replace('|', '/')} |"
            )
    else:
        lines.append("Nada pendiente.")

    lines += ["", "## Cobertura por retailer × marca", "", "| País | Retailer | Marca | Estado | Productos |",
              "|---|---|---|---|---|"]  # fmt: skip
    for s in sorted(status, key=lambda x: (x["country"], x["retailer"], x["brand"])):
        lines.append(f"| {s['country']} | {s['retailer']} | {s['brand']} | {ICON.get(s['status'], '')} "
                     f"{s['status']} | {s.get('products', 0)} |")  # fmt: skip

    if flags:
        lines += ["", f"## Validaciones a revisar ({len(flags)})", ""]
        lines += [f"- {f}" for f in flags]
    if ins.get("notes"):
        lines += ["", "## Notas de insights", ""] + [f"- {n}" for n in ins["notes"]]
    lines += [
        "",
        "## Trazabilidad",
        "",
        f"- `{_rel(run / 'capture.json')}`: URL, hora, HTML y captura de cada página/tarjeta",
        f"- `{_rel(run / 'screenshots')}` y `{_rel(run / 'html')}`",
        f"- `{_rel(run / 'review.xlsx')}`: fuente de verdad del PDF (columnas url / screenshot / html / captured_at)",
        "",
    ]
    out = run / "run_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
