"""Review core: read a workpaper and record a person's dispositions into a reviewed copy.

The tool itself never decides a disposition. This module only writes what a reviewer
chose, into the four review columns of ``workpaper_reviewed.xlsx``, and keeps a change
history in that copy's Run log sheet. The original ``workpaper.xlsx`` is never modified.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from workbench.workpaper import DISPOSITIONS, REVIEW_COLUMNS

ORIGINAL = "workpaper.xlsx"
REVIEWED = "workpaper_reviewed.xlsx"
ALLOWED = tuple(DISPOSITIONS)


class ReviewError(ValueError):
    """A reviewer input or workpaper problem, with a message meant for the reviewer."""


@dataclass
class Workpaper:
    path: Path
    cover: dict
    summary: pd.DataFrame
    integrity: pd.DataFrame
    runs: dict[str, pd.DataFrame]


def current_path(out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    reviewed, original = out_dir / REVIEWED, out_dir / ORIGINAL
    if reviewed.exists():
        return reviewed
    if original.exists():
        return original
    raise ReviewError(f"No {ORIGINAL} in {out_dir}. Run `workbench run` first, then point review at its --out folder.")


def _table(ws) -> pd.DataFrame:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return pd.DataFrame()
    header = [str(h) if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
    return pd.DataFrame([list(r) for r in rows[1:]], columns=header, dtype=object)


def _is_run_sheet(ws) -> bool:
    header = [c.value for c in next(ws.iter_rows(max_row=1))] if ws.max_row else []
    return "exception_id" in header and all(c in header for c in REVIEW_COLUMNS)


def load_workpaper(out_dir: Path) -> Workpaper:
    path = current_path(out_dir)
    wb = load_workbook(path, read_only=True)
    cover = {r[0]: r[1] for r in wb["Cover"].iter_rows(values_only=True) if r and r[0]} if "Cover" in wb else {}
    runs = {}
    for ws in wb.worksheets:
        if ws.title in ("Cover", "Integrity", "Summary", "Limitations", "Run log") or not _is_run_sheet(ws):
            continue
        df = _table(ws)
        df = df[df["exception_id"].notna()].reset_index(drop=True)  # drops a "Run skipped/failed" note row
        df["exception_id"] = df["exception_id"].astype(str)
        runs[ws.title] = df
    summary = _table(wb["Summary"]) if "Summary" in wb else pd.DataFrame()
    integrity = _table(wb["Integrity"]) if "Integrity" in wb else pd.DataFrame()
    wb.close()
    return Workpaper(path=path, cover=cover, summary=summary, integrity=integrity, runs=runs)


def _parse_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ReviewError(f"Date must be YYYY-MM-DD, got {value!r}.") from exc


def record_dispositions(out_dir: Path, run_id: str, exception_ids: list[str], *, disposition: str,
                        note: str, reviewer: str, date) -> Path:
    """Write one reviewer decision to the selected exceptions. Returns the reviewed copy's path."""
    out_dir = Path(out_dir)
    if disposition not in ALLOWED:
        raise ReviewError(f"Disposition must be one of: {', '.join(ALLOWED)}.")
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise ReviewError("Reviewer name is required.")
    ids = [str(i) for i in exception_ids]
    if not ids:
        raise ReviewError("Select at least one exception.")
    review_date = _parse_date(date)

    src = current_path(out_dir)
    wb = load_workbook(src)
    if run_id not in wb.sheetnames or not _is_run_sheet(wb[run_id]):
        raise ReviewError(f"{run_id!r} is not a test sheet in {src.name}.")
    ws = wb[run_id]
    header = [c.value for c in ws[1]]
    col = {name: header.index(name) + 1 for name in ["exception_id", *REVIEW_COLUMNS]}
    row_of = {str(ws.cell(row=r, column=col["exception_id"]).value): r
              for r in range(2, ws.max_row + 1)
              if ws.cell(row=r, column=col["exception_id"]).value is not None}
    missing = [i for i in ids if i not in row_of]
    if missing:
        raise ReviewError(f"Exception(s) not found in {run_id}: {', '.join(missing[:5])}.")

    log = wb["Run log"]
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    for eid in ids:
        r = row_of[eid]
        previous = {name: ws.cell(row=r, column=col[name]).value for name in REVIEW_COLUMNS}
        ws.cell(row=r, column=col["Disposition"]).value = disposition
        ws.cell(row=r, column=col["Reviewer note"]).value = note or None
        ws.cell(row=r, column=col["Reviewed by"]).value = reviewer
        date_cell = ws.cell(row=r, column=col["Date"])
        date_cell.value, date_cell.number_format = review_date, "yyyy-mm-dd"
        prev_text = {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in previous.items()}
        log.append(["review",
                    f"{run_id} {eid}: disposition set to {disposition} by {reviewer}",
                    str({"previous": prev_text, "review_date": review_date.isoformat(), "recorded_at": stamp})])

    dst = out_dir / REVIEWED
    fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".xlsx.tmp")
    os.close(fd)
    try:
        wb.save(tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dst
