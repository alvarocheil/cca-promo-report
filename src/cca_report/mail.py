"""Weekly e-mail: country insights in the body + the PDF attached, sent through Outlook.

Outlook (COM) uses the profile already signed in on this PC, so no password is stored.
`compose` is pure (testable); `send_outlook` is the only part that talks to Outlook.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path

from .config import Project
from .models import BRAND_ORDER, MailConfig
from .render.document import ReportData, cutoff_label
from .render.pdf import md_bold

log = logging.getLogger(__name__)

NO_INFO = "No information available."
STATUS_LABEL = {
    "manual_pending": "pending manual input",
    "blocked": "blocked (site refuses automated access)",
    "robots_disallowed": "not allowed by robots.txt",
    "error": "capture error",
    "price_error": "precios.csv has invalid values",
}

_FONT = "font-family:Arial,sans-serif;"
_TD = "border:1px solid #d0d5dd;padding:6px 8px;vertical-align:top;font-size:13px;"
_TH = _TD + "background:#1428a0;color:#ffffff;text-align:left;font-weight:bold;"
_BRAND = _TD + "background:#f2f4f7;font-weight:bold;white-space:nowrap;"


class MailError(RuntimeError):
    pass


@dataclass(slots=True)
class Email:
    subject: str
    html: str
    to: list[str]
    cc: list[str] = field(default_factory=list)
    attachments: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # why something is missing (e.g. PDF too big)


def _cell(text: str) -> str:
    text = (text or "").strip() or NO_INFO
    if text == NO_INFO:
        return f'<td style="{_TD}color:#98a2b3;">{escape(text)}</td>'
    return f'<td style="{_TD}">{md_bold(text)}</td>'


def _country_section(name: str, ins) -> str:
    """One country: Brand x Retailer table (+ bullets for PY/UY)."""
    out = [f'<h3 style="font-family:Arial,sans-serif;color:#1428a0;margin:22px 0 8px;">{escape(name)}</h3>']
    if ins is None:
        return out[0] + f'<p style="font-size:13px;color:#98a2b3;">{NO_INFO}</p>'
    if ins.cells:
        head = "".join(f'<th style="{_TH}">{escape(r)}</th>' for r in ins.retailers)
        rows = []
        for b in BRAND_ORDER:
            if b not in ins.cells:
                continue
            cells = "".join(_cell(ins.cells[b].get(r, "")) for r in ins.retailers)
            rows.append(f'<tr><td style="{_BRAND}">{escape(b.value)}</td>{cells}</tr>')
        out.append(
            '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-family:Arial,sans-serif;'
            f'width:100%;"><tr><th style="{_TH}">Brand</th>{head}</tr>{"".join(rows)}</table>'
        )
    for b in BRAND_ORDER:
        items = ins.bullets.get(b) or []
        if items:
            lis = "".join(f'<li style="margin:2px 0;">{md_bold(t)}</li>' for t in items)
            out.append(
                f'<p style="{_FONT}font-size:13px;margin:10px 0 2px;"><b>{escape(b.value)}</b></p>'
                f'<ul style="{_FONT}font-size:13px;margin:0 0 6px 18px;padding:0;">{lis}</ul>'
            )
    return "".join(out)


def _status_section(status: list[dict]) -> str:
    gaps = [s for s in status if s.get("status") in STATUS_LABEL]
    ok = sum(1 for s in status if s.get("status") in ("ok", "manual"))
    empty = sum(1 for s in status if s.get("status") == "empty")
    products = sum(int(s.get("products") or 0) for s in status)
    lines = [
        '<hr style="border:none;border-top:1px solid #d0d5dd;margin:24px 0 10px;">',
        '<p style="font-family:Arial,sans-serif;font-size:12px;color:#475467;margin:0 0 6px;">'
        f"<b>Coverage (internal)</b> · {products} products · {ok} of {len(status)} retailer × brand "
        "combinations captured" + (f" · {empty} with no products of that brand on the site" if empty else "") + "</p>",
    ]
    if gaps:
        lis = "".join(
            f"<li>{escape(s['country'])} · {escape(s['retailer'])} · {escape(s.get('brand') or '')} — "
            f"{escape(STATUS_LABEL[s['status']])}</li>"
            for s in gaps
        )
        lines.append(f'<ul style="{_FONT}font-size:12px;color:#475467;margin:0 0 0 18px;padding:0;">{lis}</ul>')
    return "".join(lines)


def compose(project: Project, data: ReportData, status: list[dict], pdf: Path | None, cfg: MailConfig) -> Email:
    week_number = int(data.week.split("-W")[1])
    cutoff = cutoff_label(data.cutoff_date)
    subject = cfg.subject.format(week=data.week, week_number=week_number, cutoff=cutoff)
    notes: list[str] = []
    attachments: list[Path] = []
    if pdf and pdf.exists():
        size_mb = pdf.stat().st_size / 1_048_576
        if size_mb <= cfg.max_attachment_mb:
            attachments.append(pdf)
        else:
            notes.append(f"PDF not attached: {size_mb:.1f} MB exceeds the {cfg.max_attachment_mb:g} MB limit")
    else:
        notes.append("PDF not available for this run")

    sections = [
        _country_section(c.name, data.countries[c.code].insights if c.code in data.countries else None)
        for c in project.report.countries
    ]
    intro = (
        f"Please find attached the <b>Weekly Competitor's Promotions Report – MX Division</b> for "
        f"<b>Week {week_number}</b> (cut-off {escape(cutoff)})."
        if attachments
        else f"Weekly Competitor's Promotions Report – MX Division, <b>Week {week_number}</b> "
        f"(cut-off {escape(cutoff)})."
    )
    body = [
        '<div style="font-family:Arial,sans-serif;font-size:14px;color:#101828;max-width:1100px;">',
        f'<p style="margin:0 0 6px;">Hello,</p><p style="margin:0 0 14px;">{intro} '
        "Below are the key insights by country.</p>",
        *sections,
    ]
    if notes:
        body.append('<p style="font-size:12px;color:#b54708;margin:16px 0 0;">' + escape(" · ".join(notes)) + "</p>")
    if cfg.include_run_status:
        body.append(_status_section(status))
    body.append("</div>")
    return Email(
        subject=subject, html="".join(body), to=list(cfg.to), cc=list(cfg.cc), attachments=attachments, notes=notes
    )


def compose_alert(week: str, run_dir: Path, error: str, cfg: MailConfig) -> Email:
    report = run_dir / "run_report.md"
    html = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;">'
        f"<p>The weekly CCA promo report for <b>{escape(week)}</b> could not be completed.</p>"
        f'<p style="color:#b42318;">{escape(error)}</p>'
        f"<p>Run folder: {escape(str(run_dir))}</p></div>"
    )
    return Email(
        subject=f"[CCA report] Week {week} failed",
        html=html,
        to=list(cfg.alert_to or cfg.to),
        attachments=[report] if report.exists() else [],
    )


def save_preview(email: Email, path: Path) -> Path:
    meta = (
        f"<!-- subject: {escape(email.subject)} | to: {escape(', '.join(email.to))} | cc: {escape(', '.join(email.cc))}"
        f" | attachments: {escape(', '.join(a.name for a in email.attachments))} -->\n"
    )
    path.write_text(
        f'<!doctype html><meta charset="utf-8"><title>{escape(email.subject)}</title>\n{meta}{email.html}',
        encoding="utf-8",
    )
    return path


def default_address(account: str = "") -> str:
    """SMTP address of the Outlook account used to send (the 'send it to me' default)."""
    return _account(_outlook()[1], account).SmtpAddress


def _outlook():
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:  # pragma: no cover - non-Windows
        raise MailError("Outlook sending needs Windows with pywin32 installed") from exc
    pythoncom.CoInitialize()
    try:
        app = win32com.client.Dispatch("Outlook.Application")
        return app, app.GetNamespace("MAPI")
    except Exception as exc:
        raise MailError(f"Outlook is not available: {exc}") from exc


def _account(ns, wanted: str):
    accounts = [ns.Accounts.Item(i) for i in range(1, ns.Accounts.Count + 1)]
    if not accounts:
        raise MailError("Outlook has no configured account")
    if not wanted:
        return accounts[0]
    for a in accounts:
        if a.SmtpAddress.lower() == wanted.lower():
            return a
    raise MailError(f"Outlook account {wanted!r} not found ({', '.join(a.SmtpAddress for a in accounts)})")


def send_outlook(email: Email, account: str = "") -> None:
    """Same pattern as the Sprint Report (Jira_Monthly_report): build the item, pick the
    sender account and Send(); Outlook delivers it from the Outbox on its own."""
    if not email.to:
        raise MailError("no recipients (mail.to in config/report.yaml)")
    app, ns = _outlook()
    acc = None
    if account:
        try:
            acc = _account(ns, account)
        except MailError as exc:
            log.warning("%s - sending from the default account", exc)
    item = app.CreateItem(0)  # olMailItem
    item.Subject = email.subject
    item.HTMLBody = email.html
    item.To = "; ".join(email.to)
    if email.cc:
        item.CC = "; ".join(email.cc)
    for a in email.attachments:
        item.Attachments.Add(str(a.resolve()))
    if acc is not None:
        try:
            item.SendUsingAccount = acc
        except Exception:
            item._oleobj_.Invoke(64209, 0, 8, 0, acc)  # DISPID of SendUsingAccount, DISPATCH_PROPERTYPUTREF
    try:
        item.Send()
    except Exception as exc:
        raise MailError(f"Outlook refused the message: {exc}") from exc


def record_sent(run_dir: Path, email: Email) -> None:
    (run_dir / "sent.json").write_text(
        json.dumps(
            {
                "sent_at": datetime.now().astimezone().isoformat(),
                "subject": email.subject,
                "to": email.to,
                "cc": email.cc,
                "attachments": [a.name for a in email.attachments],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
