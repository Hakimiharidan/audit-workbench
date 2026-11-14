"""Excel workpaper. The tool never writes into review, disposition or sign-off fields."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

DISPOSITIONS = ["Error", "Irregularity — inquire", "Needs client query", "False positive", "Accepted"]
REVIEW_COLUMNS = ["Disposition", "Reviewer note", "Reviewed by", "Date"]
SIGNOFF_FIELDS = ["Prepared by", "Prepared date", "Reviewed by", "Review date", "Conclusion"]
FORMULA_START = ("=", "+", "-", "@")
HEAD = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="305496")
REVIEW_FILL = PatternFill("solid", fgColor="FFF2CC")
STATUS_FILL = {"fail": "F8CBAD", "warn": "FFE699", "pass": "C6EFCE", "info": None,
               "failed": "F8CBAD", "skipped": "FFE699", "ok": "C6EFCE"}
DISCLAIMER = ("This workpaper supports audit procedures; it does not replace professional judgment. "
              "The tool flags and ranks exceptions only. Dispositions, conclusions and sign-off are made by "
              "people. No warranty.")


def _set(ws, row, col, value):
    """Write a value; text that looks like a formula stays literal text."""
    if isinstance(value, (list, dict)):
        value = str(value)
    cell = ws.cell(row=row, column=col)
    cell.value = value
    if isinstance(value, str) and value.startswith(FORMULA_START):
        cell.data_type = "s"
    elif isinstance(value, Decimal):
        cell.number_format = "#,##0.00" if value == value.quantize(Decimal("0.01")) else "0.000000"
    elif isinstance(value, dt.date):
        cell.number_format = "yyyy-mm-dd"
    return cell


def _header(ws, row, names, fills=None):
    for i, n in enumerate(names, start=1):
        c = _set(ws, row, i, n)
        c.font, c.fill = HEAD, (fills or {}).get(n, HEAD_FILL)
        c.alignment = Alignment(wrap_text=True, vertical="top")


def _widths(ws, widths=None, default=16, maximum=60):
    for col in range(1, ws.max_column + 1):
        letter = get_column_letter(col)
        if widths and col <= len(widths):
            ws.column_dimensions[letter].width = widths[col - 1]
            continue
        longest = max((len(str(c.value)) for c in ws[letter][:200] if c.value is not None), default=8)
        ws.column_dimensions[letter].width = max(10, min(maximum, longest + 2, default * 3))


def _fill_status(cell, status):
    color = STATUS_FILL.get(status)
    if color:
        cell.fill = PatternFill("solid", fgColor=color)


def write_workpaper(path: Path, ctx: dict) -> None:
    wb = Workbook()
    _cover(wb.active, ctx)
    _integrity(wb.create_sheet("Integrity"), ctx)
    _summary(wb.create_sheet("Summary"), ctx)
    for run in ctx["runs"]:
        _run_sheet(wb.create_sheet(run["run_id"]), run)
    _limitations(wb.create_sheet("Limitations"), ctx)
    _runlog(wb.create_sheet("Run log"), ctx)
    wb.properties.creator = f"audit-workbench {ctx['version']}"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _cover(ws, ctx):
    ws.title = "Cover"
    m = ctx["materiality"]
    rows = [
        ("Audit Analytics Workbench — workpaper", None),
        ("Engagement", ctx.get("engagement") or None),
        ("Objective", "Check the integrity of the mapped extract, then run the data-analytics tests listed on "
                      "the Summary sheet. Exceptions are flagged and ranked for human review."),
        ("Run ID", ctx["run_id"]),
        ("Tool version", ctx["version"]),
        ("Run date (UTC)", ctx["timestamp"]),
        ("Mapping file", ctx["mapping_path"]),
        ("Config file", ctx["config_path"]),
        ("Overall materiality", m.overall),
        ("Performance materiality", m.performance),
        ("Clearly trivial threshold", m.trivial),
        ("Integrity gate", ctx["gate"].status),
        ("--force used", "YES — tests ran although the integrity gate failed" if ctx["force_used"] else "no"),
        (None, None),
    ]
    for r, (k, v) in enumerate(rows, start=1):
        _set(ws, r, 1, k).font = Font(bold=True, size=14 if r == 1 else 11)
        _set(ws, r, 2, v)
    r = len(rows) + 1
    _header(ws, r, ["Input file", "SHA-256", "Size (bytes)", "Modified (UTC)"])
    for f in ctx["files"]:
        r += 1
        for i, v in enumerate([f.path, f.sha256, f.size, f.modified], start=1):
            _set(ws, r, i, v)
    r += 2
    _set(ws, r, 1, "Sign-off (completed by people, never by the tool)").font = Font(bold=True)
    for name in SIGNOFF_FIELDS:
        r += 1
        _set(ws, r, 1, name).font = Font(bold=True)
        ws.cell(row=r, column=2).fill = REVIEW_FILL          # left blank on purpose
    r += 2
    _set(ws, r, 1, "Disclaimer").font = Font(bold=True)
    _set(ws, r, 2, DISCLAIMER).alignment = Alignment(wrap_text=True)
    _widths(ws, [30, 70, 14, 26])


def _integrity(ws, ctx):
    g = ctx["gate"]
    _set(ws, 1, 1, f"Integrity gate status: {g.status.upper()}").font = Font(bold=True, size=13)
    if ctx["force_used"]:
        _set(ws, 2, 1, "--force was used: tests ran although this gate failed.").font = Font(bold=True, color="C00000")
    _header(ws, 4, ["Dataset", "Check", "Status", "Detail", "Value"])
    for r, c in enumerate(g.checks, start=5):
        for i, v in enumerate([c.dataset, c.check, c.status, c.detail, c.value], start=1):
            _set(ws, r, i, v)
        _fill_status(ws.cell(row=r, column=3), c.status)
    ws.freeze_panes = "A5"
    ws.auto_filter.ref = f"A4:E{max(5, len(g.checks) + 4)}"
    _widths(ws, [20, 26, 9, 110, 18])


def _summary(ws, ctx):
    cols = ["Run ID", "Test", "Pack", "Assertion", "Category", "Status", "Reason", "Exceptions",
            "Total amount at risk", "Highest band", "Aggregate band", "Needs client confirmation", "Note"]
    _header(ws, 1, cols)
    for r, run in enumerate(ctx["runs"], start=2):
        a = run["aggregate"]
        vals = [run["run_id"], run["name"], run["pack"], run["assertion"], run["category"], run["status"],
                run["reason"] or None, a["count"], a["total"], a["highest_band"], a["aggregate_band"],
                "yes" if run["needs_confirmation"] else "no", run["systemic"]]
        for i, v in enumerate(vals, start=1):
            _set(ws, r, i, v)
        _fill_status(ws.cell(row=r, column=6), run["status"])
    ws.freeze_panes = "B2"
    _widths(ws, [10, 34, 34, 15, 11, 9, 40, 11, 18, 18, 18, 14, 60])


def _run_sheet(ws, run):
    lead = ["band", "label", "needs_client_confirmation"]
    pack_cols = [c for c in run["columns"] if c not in lead]
    cols = lead + pack_cols + REVIEW_COLUMNS
    _header(ws, 1, cols, {c: PatternFill("solid", fgColor="BF8F00") for c in REVIEW_COLUMNS})
    for r, e in enumerate(run["exceptions"], start=2):
        for i, c in enumerate(lead + pack_cols, start=1):
            _set(ws, r, i, e.get(c))
        for j in range(len(REVIEW_COLUMNS)):
            ws.cell(row=r, column=len(lead) + len(pack_cols) + 1 + j).fill = REVIEW_FILL
    disp_col = get_column_letter(len(lead) + len(pack_cols) + 1)
    last = max(1000, len(run["exceptions"]) + 1)
    dv = DataValidation(type="list", formula1='"' + ",".join(DISPOSITIONS) + '"', allow_blank=True,
                        showErrorMessage=True, errorTitle="Disposition",
                        error="Choose a disposition from the list.")
    dv.add(f"{disp_col}2:{disp_col}{last}")
    ws.add_data_validation(dv)
    if run["status"] != "ok":
        _set(ws, 2, 1, f"Run {run['status']}: {run['reason']}").font = Font(bold=True, color="C00000")
    ws.freeze_panes = "D2"
    if run["exceptions"]:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{len(run['exceptions']) + 1}"
    _widths(ws)
    for j, name in enumerate(REVIEW_COLUMNS):
        ws.column_dimensions[get_column_letter(len(lead) + len(pack_cols) + 1 + j)].width = 24 if j < 2 else 14


def _limitations(ws, ctx):
    _header(ws, 1, ["Run ID", "Test", "Limitations", "Needs client confirmation"])
    for r, run in enumerate(ctx["runs"], start=2):
        for i, v in enumerate([run["run_id"], run["name"], run["limitations"],
                               "yes" if run["needs_confirmation"] else "no"], start=1):
            _set(ws, r, i, v).alignment = Alignment(wrap_text=True, vertical="top")
    _widths(ws, [10, 34, 110, 14])


def _runlog(ws, ctx):
    _header(ws, 1, ["Kind", "Message", "Detail"])
    for r, ev in enumerate(ctx["log"].events, start=2):
        detail = {k: v for k, v in ev.items() if k not in ("kind", "message")}
        for i, v in enumerate([ev["kind"], ev["message"], str(detail) if detail else None], start=1):
            _set(ws, r, i, v)
    _widths(ws, [16, 90, 80])
