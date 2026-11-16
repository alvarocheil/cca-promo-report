"""CLI: validate-config | check-access | discover (later: capture, extract, review, build, run-all)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import status_report
from .access import Prober
from .config import ConfigError, Project, Settings
from .discover import discover_retailer, discovery_error, write_proposal
from .models import AccessResult, DiscoveryResult

for _stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(add_completion=False, no_args_is_help=True, help="CCA Consolidated Competitors Report generator.")
console = Console()

CountryOpt = Annotated[list[str] | None, typer.Option("--country", "-c", help="PA, CR, EC, PY, UY")]
RetailerOpt = Annotated[list[str] | None, typer.Option("--retailer", "-r", help="slug, e.g. cr_monge")]


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _project() -> Project:
    project = Project()
    try:
        _ = project.report, project.retailers
    except ConfigError as exc:
        console.print(f"[red]Config error[/red]\n{exc}")
        raise typer.Exit(2) from exc
    return project


@app.command("validate-config")
def validate_config() -> None:
    """Validate report.yaml and every retailer YAML."""
    p = _project()
    t = Table("País", "Slug", "Retailer", "Marcas", "Modo", "URLs", "Estado")
    for r in p.retailers:
        modes = sorted({b.mode.value for b in r.brands.values()})
        n_urls = sum(len(b.urls) for b in r.brands.values())
        flags = "manual_only" if r.manual_only else ("disabled" if not r.enabled else "")
        t.add_row(
            r.country.value,
            r.slug,
            r.retailer_name,
            ",".join(b.value[0] for b in r.brands),
            ",".join(modes),
            str(n_urls),
            flags + (" ⚠ discover" if r.discover else ""),
        )
    console.print(t)
    console.print(
        f"[green]OK[/green] {len(p.retailers)} retailers · week {p.report.week} · cut-off {p.report.cutoff_label}"
    )


@app.command("check-access")
def check_access(country: CountryOpt = None, retailer: RetailerOpt = None) -> None:
    """Probe robots.txt, plain HTTP and a real browser for every configured URL."""
    p = _project()
    retailers = [r for r in p.select(country, retailer) if r.enabled]
    out = p.run_dir("discovery")
    prober = Prober(p, Settings(), out)

    async def go() -> list[AccessResult]:
        nested = await prober.run(retailers, prober.check_retailer, prober.error_results)
        return [a for lst in nested for a in lst]

    results = asyncio.run(go())
    _merge_json(out / "access.json", [r.model_dump(mode="json") for r in results])
    _write_status(p)
    console.print(f"[green]Done[/green] {len(results)} URLs → {out / 'status.md'}")


@app.command()
def discover(country: CountryOpt = None, retailer: RetailerOpt = None) -> None:
    """Navigate the 'Celulares' menus of retailers with a discover block and propose URLs."""
    p = _project()
    retailers = [r for r in p.select(country, retailer) if r.discover and r.enabled]
    out = p.run_dir("discovery")
    prober = Prober(p, Settings(), out)
    results = asyncio.run(prober.run(retailers, lambda r: discover_retailer(prober, r), discovery_error))
    for res in results:
        write_proposal(res, out / "proposals")
    _merge_json(out / "discovery.json", [r.model_dump(mode="json") for r in results])
    _write_status(p)
    console.print(f"[green]Done[/green] {len(results)} retailers → {out / 'proposals'}")


@app.command()
def sample(
    fixture: Annotated[Path, typer.Option(help="report data JSON")] = Path("tests/fixtures/w44_sample.json"),
    out: Annotated[str, typer.Option(help="run folder name under runs/")] = "sample",
) -> None:
    """Phase 1: render the W44 fixture to runs/<out>/report.pdf."""
    from .render.document import DocumentBuilder, ReportData
    from .render.pdf import RenderError, build_pdf

    p = _project()
    data = ReportData.model_validate_json(fixture.read_text(encoding="utf-8"))
    run = p.run_dir(out)
    builder = DocumentBuilder(p, data, run / "screenshots")
    week = int(data.week.split("-W")[1])
    try:
        outcome, flags = asyncio.run(build_pdf(builder, run / "report.pdf", week))
    except RenderError as exc:
        console.print(f"[red]Render failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    pdf = run / f"CCA_Consolidated_Competitors_Report-Week{week:02d}.pdf"
    pdf.write_bytes(outcome.pdf.read_bytes())
    console.print(f"[green]PDF[/green] {pdf} · {outcome.pages} pages · fit rounds {outcome.rounds}")
    if outcome.overflow:
        console.print(f"[red]Overflow left:[/red] {outcome.overflow}")
    if flags:
        console.print(f"[yellow]{len(flags)} review flags[/yellow]")
        for f in flags:
            console.print(f"  • {f}")


WeekOpt = Annotated[str | None, typer.Option("--week", "-w", help="ISO week, default report.yaml")]


def _week(p: Project, week: str | None) -> tuple[str, Path]:
    w = week or p.report.week
    return w, p.run_dir(w)


@app.command()
def capture(week: WeekOpt = None, country: CountryOpt = None, retailer: RetailerOpt = None) -> None:
    """Visit retailers (robots.txt + 3 s per domain), screenshot product cards, save HTML/JSON."""
    from datetime import datetime

    from .capture.runner import Capturer, failed_groups, merge_groups
    from .models import CaptureRun

    p = _project()
    w, run = _week(p, week)
    retailers = [r for r in p.select(country, retailer) if r.enabled]
    prober = Prober(p, Settings(), run)
    cap = Capturer(p, prober, run)
    started = datetime.now().astimezone()
    nested = asyncio.run(prober.run(retailers, cap.capture_retailer, failed_groups))
    path = run / "capture.json"
    old = CaptureRun.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else None
    groups = [g for lst in nested for g in lst]
    result = CaptureRun(
        week=w,
        started_at=old.started_at if old else started,
        finished_at=datetime.now().astimezone(),
        groups=merge_groups(old.groups if old else [], groups),
    )
    path.write_text(result.model_dump_json(indent=1), encoding="utf-8")
    t = Table("País", "Retailer", "Marca", "Modo", "Páginas", "Tarjetas", "Estado")
    for g in groups:
        cards = sum(len(pg.cards) for pg in g.pages)
        states = ",".join(sorted({pg.status.value for pg in g.pages})) or ("manual" if g.manual_dir else "-")
        t.add_row(
            g.country.value,
            g.retailer,
            g.brand.value if g.brand else "",
            g.mode.value,
            str(len(g.pages)),
            str(cards),
            states,
        )
    console.print(t)
    console.print(f"[green]capture.json[/green] {path}")


@app.command()
def extract(week: WeekOpt = None) -> None:
    """Offline: capture.json -> data.json (products, selection, validations, slides)."""
    from .extract.pipeline import Extractor
    from .models import CaptureRun

    p = _project()
    _, run = _week(p, week)
    cap = CaptureRun.model_validate_json((run / "capture.json").read_text(encoding="utf-8"))
    data = Extractor(p, run).run(cap)
    (run / "data.json").write_text(data.model_dump_json(indent=1), encoding="utf-8")
    t = Table("País", "Retailer", "Marca", "Estado", "Productos", "Detalle")
    for s in data.status:
        t.add_row(s.country, s.retailer, s.brand, s.status, str(s.products), s.detail[:70])
    console.print(t)
    console.print(f"[green]data.json[/green] {run / 'data.json'} · {len(data.groups)} slides")


@app.command()
def review(
    week: WeekOpt = None, force: Annotated[bool, typer.Option(help="overwrite an edited review.xlsx")] = False
) -> None:
    """data.json -> review.xlsx (one sheet per country + Slides + Insights + Status)."""
    from .extract.pipeline import RunData
    from .insights import build_insights
    from .review import export_review

    p = _project()
    _, run = _week(p, week)
    out = run / "review.xlsx"
    if out.exists() and not force:
        console.print(f"[yellow]{out} already exists (may contain edits). Use --force to regenerate.[/yellow]")
        raise typer.Exit(1)
    data = RunData.model_validate_json((run / "data.json").read_text(encoding="utf-8"))
    with console.status(f"Insights ({p.report.insights_provider})..."):
        insights, notes, used = build_insights(p, data, run)
    (run / "insights_notes.json").write_text(
        json.dumps({"provider": used, "notes": notes}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    from .review import data_fingerprint

    export_review(
        p, data, insights, out, [s.model_dump() for s in data.status], fingerprint=data_fingerprint(run / "data.json")
    )
    console.print(f"[green]review.xlsx[/green] {out} · insights: {used}")
    for n in notes:
        console.print(f"  · {n}")


@app.command()
def build(
    week: WeekOpt = None,
    allow_stale: Annotated[bool, typer.Option(help="build even if review.xlsx predates data.json")] = False,
) -> None:
    """review.xlsx (source of truth) -> CCA_Consolidated_Competitors_Report-WeekWW.pdf."""
    from .extract.pipeline import RunData
    from .review import import_review

    p = _project()
    w, run = _week(p, week)
    base = RunData.model_validate_json((run / "data.json").read_text(encoding="utf-8"))
    from .extract.pipeline import recompose_listings
    from .review import ReviewError, data_fingerprint, review_fingerprint
    from .run_report import write_run_report

    try:
        fp_review, fp_data = review_fingerprint(run / "review.xlsx"), data_fingerprint(run / "data.json")
    except ReviewError as exc:
        console.print(f"[red]review.xlsx:[/red] {exc}")
        raise typer.Exit(1) from exc
    if fp_review != fp_data and not allow_stale:
        console.print(
            "[red]review.xlsx was generated from an older extraction (data.json changed).[/red] "
            "Regenerate it with `review --force` (re-apply your edits) or pass --allow-stale."
        )
        raise typer.Exit(1)
    try:
        data = import_review(run / "review.xlsx", base)
    except ReviewError as exc:
        console.print(f"[red]review.xlsx:[/red] {exc}")
        raise typer.Exit(1) from exc
    recompose_listings(data, run)
    data.cutoff_date = p.report.cutoff_for(w)
    pdf, flags = _render(p, data, run, int(w.split("-W")[1]))
    # what the PDF was built from: `send` uses it, so it never re-reads (possibly DRM-locked) Excel
    (run / "report_data.json").write_text(data.model_dump_json(indent=1), encoding="utf-8")
    (run / "build_flags.json").write_text(json.dumps(flags, ensure_ascii=False), encoding="utf-8")
    report = write_run_report(p, w, run, _load_steps(run), flags, pdf)
    console.print(f"[green]run_report.md[/green] {report}")


def _load_steps(run: Path) -> list[tuple[str, float, str]]:
    path = run / "steps.json"
    return [tuple(x) for x in json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []


@app.command("run-all")
def run_all(
    week: WeekOpt = None,
    country: CountryOpt = None,
    retailer: RetailerOpt = None,
    pause: Annotated[bool, typer.Option("--pause/--no-pause", help="stop for the Excel review before build")] = True,
    skip_capture: Annotated[bool, typer.Option(help="reuse capture.json (no network)")] = False,
    force_review: Annotated[bool, typer.Option(help="regenerate review.xlsx even if it was edited")] = False,
) -> None:
    """capture -> extract -> review.xlsx -> (pause for the human review) -> build PDF + run_report.md."""
    import time

    from .run_report import write_run_report

    p = _project()
    w, run = _week(p, week)
    steps: list[tuple[str, float, str]] = []

    def save() -> None:
        (run / "steps.json").write_text(json.dumps(steps, ensure_ascii=False), encoding="utf-8")

    def step(name: str, fn, result: str = "ok") -> None:
        t0 = time.monotonic()
        console.rule(f"[bold]{name}")
        try:
            fn()
        except BaseException as exc:  # typer.Exit included: record, report, re-raise
            code = getattr(exc, "exit_code", None)
            steps.append((name, time.monotonic() - t0, f"error ({type(exc).__name__}{f' {code}' if code else ''})"))
            save()
            write_run_report(p, w, run, steps)
            raise
        steps.append((name, time.monotonic() - t0, result))
        save()

    save()  # a new run starts a new step log

    if skip_capture:
        steps.append(("capture", 0.0, "omitido (--skip-capture)"))
    else:
        step("capture", lambda: capture(w, country, retailer))
    step("extract", lambda: extract(w))
    from .review import ReviewError, data_fingerprint, review_fingerprint

    xlsx = run / "review.xlsx"
    try:
        fresh = review_fingerprint(xlsx) == data_fingerprint(run / "data.json")
    except ReviewError as exc:  # DRM-locked workbook: keep it as a backup, regenerate
        console.print(f"[yellow]{exc}[/yellow]")
        fresh = False
    if xlsx.exists() and fresh and not force_review:
        steps.append(("review", 0.0, "review.xlsx existente conservado (misma extracción)"))
        save()
    else:
        if xlsx.exists():  # stale workbook: keep a copy so edits can be re-applied, never mix data
            from datetime import datetime

            backup = run / f"review_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
            xlsx.replace(backup)
            console.print(f"[yellow]review.xlsx anterior guardado como {backup.name} (extracción nueva)[/yellow]")
        step("review", lambda: review(w, force=True))
    if pause:
        if sys.stdin.isatty():
            typer.confirm(f"Revisa y guarda {run / 'review.xlsx'}. ¿Generar el PDF ahora?", default=True, abort=True)
        else:
            report = write_run_report(p, w, run, steps)
            console.print(
                f"[yellow]Revisión pendiente:[/yellow] edita {run / 'review.xlsx'} y ejecuta "
                f"`python -m uv run cca-report build --week {w}` · {report}"
            )
            return
    step("build", lambda: build(w))
    pdf = run / f"CCA_Consolidated_Competitors_Report-Week{int(w.split('-W')[1]):02d}.pdf"
    write_run_report(
        p,
        w,
        run,
        steps,
        json.loads((run / "build_flags.json").read_text(encoding="utf-8"))
        if (run / "build_flags.json").exists()
        else None,
        pdf,
    )


def _pdf_path(run: Path, w: str) -> Path:
    return run / f"CCA_Consolidated_Competitors_Report-Week{int(w.split('-W')[1]):02d}.pdf"


def _compose_email(p: Project, w: str, run: Path, to: list[str] | None):
    from .extract.pipeline import RunData
    from .mail import compose, default_address
    from .render.document import ReportData

    built, raw, pdf = run / "report_data.json", run / "data.json", _pdf_path(run, w)
    if not (built.exists() and raw.exists() and pdf.exists()):
        console.print(f"[red]Nothing to send for {w}:[/red] run `run-all` / `build` first.")
        raise typer.Exit(1)
    xlsx = run / "review.xlsx"
    newest_input = max(raw.stat().st_mtime, xlsx.stat().st_mtime if xlsx.exists() else 0)
    if built.stat().st_mtime < newest_input:
        console.print("[red]The PDF is older than data.json / review.xlsx[/red]: run `build` before sending.")
        raise typer.Exit(1)
    data = ReportData.model_validate_json(built.read_text(encoding="utf-8"))
    base = RunData.model_validate_json(raw.read_text(encoding="utf-8"))
    cfg = p.report.mail.model_copy()
    if to:
        cfg.to = list(to)
    if not cfg.to:
        cfg.to = [default_address(cfg.account)]  # first test: send it to the Outlook user
    return compose(p, data, [s.model_dump() for s in base.status], pdf, cfg)


ToOpt = Annotated[list[str] | None, typer.Option("--to", help="recipient (repeatable); default mail.to")]


@app.command()
def send(
    week: WeekOpt = None,
    to: ToOpt = None,
    dry_run: Annotated[bool, typer.Option(help="only write email_preview.html")] = False,
    resend: Annotated[bool, typer.Option(help="send even if this week was already sent")] = False,
) -> None:
    """E-mail the PDF with each country's insights in the body (local Outlook profile)."""
    from .mail import MailError, record_sent, save_preview, send_outlook

    p = _project()
    w, run = _week(p, week)
    if (run / "sent.json").exists() and not (resend or dry_run):
        console.print(f"[yellow]{w} was already sent[/yellow] ({run / 'sent.json'}). Use --resend to send again.")
        return
    email = _compose_email(p, w, run, to)
    preview = save_preview(email, run / "email_preview.html")
    console.print(
        f"[bold]{email.subject}[/bold]\n  to: {', '.join(email.to)}"
        + (f" · cc: {', '.join(email.cc)}" if email.cc else "")
        + f"\n  attachments: {', '.join(a.name for a in email.attachments) or '-'} · preview {preview}"
    )
    for n in email.notes:
        console.print(f"  [yellow]· {n}[/yellow]")
    if dry_run:
        return
    try:
        send_outlook(email, p.report.mail.account)
    except MailError as exc:
        console.print(f"[red]E-mail not sent:[/red] {exc}")
        raise typer.Exit(1) from exc
    record_sent(run, email)
    console.print("[green]E-mail sent[/green]")


@app.command()
def weekly(
    week: WeekOpt = None,
    send_mail: Annotated[bool, typer.Option("--send/--no-send", help="e-mail the report at mail.send_at")] = True,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="hold the e-mail until mail.send_at")] = True,
    recapture: Annotated[bool, typer.Option(help="capture again even if this week was captured today")] = False,
    skip_capture: Annotated[bool, typer.Option(help="reuse capture.json (no network)")] = False,
    dry_run: Annotated[bool, typer.Option(help="build everything, write email_preview.html, send nothing")] = False,
) -> None:
    """Unattended weekly run (Task Scheduler): run-all without pause -> wait for send_at -> e-mail."""
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo

    p = _project()
    w, run = _week(p, week)
    run.mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(p.report.timezone)
    cfg = p.report.mail
    console.rule(f"[bold]weekly {w} · {datetime.now(tz):%Y-%m-%d %H:%M %Z}")
    # one capture per week and day: a re-run (e.g. after fixing the e-mail) never revisits the sites
    state_path = run / "weekly_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    captured_today = state.get("captured_on") == f"{datetime.now(tz):%Y-%m-%d}"
    if captured_today and not recapture and not skip_capture:
        skip_capture = True
        console.print("[yellow]Already captured today: reusing capture.json (use --recapture to override).[/yellow]")
    if not skip_capture:  # recorded before any request: a crash later never triggers a second capture today
        state_path.write_text(
            json.dumps({"captured_on": f"{datetime.now(tz):%Y-%m-%d}", "at": datetime.now(tz).isoformat()}),
            encoding="utf-8",
        )
    alerts = send_mail and cfg.enabled and not dry_run
    try:
        run_all(w, None, None, pause=False, skip_capture=skip_capture, force_review=False)
    except BaseException as exc:
        if alerts and not isinstance(exc, KeyboardInterrupt):
            _send_alert(p, w, run, f"{type(exc).__name__}: {exc}")
        raise
    if not (send_mail and cfg.enabled):
        return
    from .models import friday_of

    hh, mm = map(int, cfg.send_at.split(":"))
    now = datetime.now(tz)
    # deadline = the report week's Friday at send_at: a late catch-up run sends right away
    fri = friday_of(w)
    target = datetime(fri.year, fri.month, fri.day, hh, mm, tzinfo=tz)
    if wait and not dry_run and now < target:
        console.print(f"Report ready. E-mail scheduled for {target:%Y-%m-%d %H:%M %Z} (waiting {target - now}).")
        time.sleep((target - now).total_seconds())
    try:
        send(w, None, dry_run=dry_run, resend=False)
    except BaseException as exc:
        if alerts and not isinstance(exc, KeyboardInterrupt):
            _send_alert(
                p, w, run, f"the report was built but the e-mail could not be sent: {type(exc).__name__}: {exc}"
            )
        raise


def _send_alert(p: Project, w: str, run: Path, error: str) -> None:
    from .mail import MailError, compose_alert, default_address, send_outlook

    try:
        cfg = p.report.mail.model_copy()
        if not (cfg.alert_to or cfg.to):
            cfg.alert_to = [default_address(cfg.account)]
        send_outlook(compose_alert(w, run, error, cfg), cfg.account)
        console.print("[yellow]Failure alert e-mailed.[/yellow]")
    except (MailError, Exception) as exc:
        console.print(f"[red]Could not e-mail the failure alert:[/red] {exc}")


@app.command("manual-folders")
def manual_folders(week: WeekOpt = None) -> None:
    """No network: create runs/<week>/manual/ folders (LEEME.txt + precios.csv) for manual retailers
    and for the combinations blocked in the previous week, to fill them before the weekly run."""
    import re

    from .capture.runner import Capturer
    from .extract.pipeline import RunData
    from .models import ExtractMode

    p = _project()
    w, run = _week(p, week)
    prev = sorted(
        d
        for d in p.runs_dir.glob("*-W*")
        if re.fullmatch(r"\d{4}-W\d{2}", d.name) and d.name < w and (d / "data.json").exists()
    )
    gaps: dict[tuple[str, str, str], str] = {}
    if prev:
        last = RunData.model_validate_json((prev[-1] / "data.json").read_text(encoding="utf-8"))
        gaps = {
            (s.country, s.retailer, s.brand): f"{s.status} in {prev[-1].name}: {s.detail[:120]}"
            for s in last.status
            if s.status in ("blocked", "robots_disallowed", "error")
        }
    cap = Capturer(p, None, run)  # type: ignore[arg-type] - manual folders never touch the network
    t = Table("País", "Retailer", "Marca", "Motivo", "Carpeta")
    for r in p.retailers:
        if not r.enabled:
            continue
        if r.campaign_overview:
            g = cap._campaign_group(r)
            t.add_row(r.country.value, r.retailer_name, "Campaign", "campaign overview", g.manual_dir or "")
        for b, c in r.brands.items():
            if not c.enabled:
                continue
            why = (
                "manual_only"
                if r.manual_only
                else "mode: manual"
                if c.mode == ExtractMode.MANUAL
                else gaps.get((r.country.value, r.retailer_name, b.value))
            )
            if why:
                g = cap._manual_group(r, b, c, why)
                t.add_row(r.country.value, r.retailer_name, b.value, why[:60], g.manual_dir or "")
    console.print(t)
    console.print(
        "Deja las capturas (una por producto) y completa [bold]precios.csv[/bold] en cada carpeta antes de la corrida."
    )


def _render(p: Project, data, run: Path, week: int) -> tuple[Path, list[str]]:
    from .render.document import DocumentBuilder
    from .render.pdf import RenderError, build_pdf

    builder = DocumentBuilder(p, data, run / "screenshots" / "placeholders")
    try:
        outcome, flags = asyncio.run(build_pdf(builder, run / "report.pdf", week))
    except RenderError as exc:
        console.print(f"[red]Render failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    pdf = run / f"CCA_Consolidated_Competitors_Report-Week{week:02d}.pdf"
    pdf.write_bytes(outcome.pdf.read_bytes())
    console.print(f"[green]PDF[/green] {pdf} · {outcome.pages} pages · fit rounds {outcome.rounds}")
    if flags:
        console.print(f"[yellow]{len(flags)} review flags[/yellow]")
        for f in flags:
            console.print(f"  • {f}")
    return pdf, flags


def _merge_json(path: Path, new_rows: list[dict]) -> None:
    """Replace rows of the retailers just processed, keep the others (subset runs)."""
    fresh = {r["slug"] for r in new_rows}
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    rows = [r for r in old if r["slug"] not in fresh] + new_rows
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_status(p: Project) -> None:
    out = p.run_dir("discovery")
    access, disc = [], []
    if (out / "access.json").exists():
        access = [AccessResult.model_validate(x) for x in json.loads((out / "access.json").read_text(encoding="utf-8"))]
    if (out / "discovery.json").exists():
        disc = [
            DiscoveryResult.model_validate(x) for x in json.loads((out / "discovery.json").read_text(encoding="utf-8"))
        ]
    status_report.write(out / "status.md", access, disc)


if __name__ == "__main__":  # pragma: no cover
    app()
