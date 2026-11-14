"""Read source files as text, without any implicit conversion.

CSV is parsed with the csv module; Excel with openpyxl in read-only mode.
Each cell becomes a str (or None for an empty Excel cell). Conversions that
had to happen to obtain text (Excel numbers and dates) are counted per column
so they can be reported.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl


class SourceError(Exception):
    """A source file or sheet cannot be read."""


@dataclass
class RawTable:
    header: list[str]
    rows: list[list[str | None]]          # data rows that have the right shape
    row_numbers: list[int]                # source row number (1 = header) of each row
    source_rows: int                      # non-blank data rows seen in the source
    rejected: list[tuple[int, str]] = field(default_factory=list)
    blank_rows_skipped: int = 0
    coercions: dict[str, dict[str, int]] = field(default_factory=dict)  # column -> {kind: count}


def is_excel(path: Path, sheet: str | None) -> bool:
    return sheet is not None or path.suffix.lower() in {".xlsx", ".xlsm"}


def list_sheets(path: Path) -> list[str]:
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def read_header(path: Path, sheet: str | None = None) -> list[str]:
    path = Path(path)
    if not path.is_file():
        raise SourceError(f"file not found: {path}")
    if is_excel(path, sheet):
        wb = openpyxl.load_workbook(path, read_only=True)
        try:
            if sheet not in wb.sheetnames:
                raise SourceError(f"sheet '{sheet}' not in {path.name} (sheets: {', '.join(wb.sheetnames)})")
            first = next(wb[sheet].iter_rows(values_only=True, max_row=1), ())
            return [("" if v is None else str(v)) for v in first]
        finally:
            wb.close()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return next(csv.reader(fh), [])


def _cell_to_text(value) -> tuple[str | None, str | None]:
    """Return (text, coercion kind or None)."""
    if value is None:
        return None, None
    if isinstance(value, str):
        return value, None
    if isinstance(value, bool):
        return ("true" if value else "false"), "excel boolean -> text"
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return value.date().isoformat(), "excel date -> text"
        return value.isoformat(sep=" "), "excel datetime -> text"
    if isinstance(value, dt.date):
        return value.isoformat(), "excel date -> text"
    if isinstance(value, dt.time):
        return value.isoformat(), "excel time -> text"
    if isinstance(value, int):
        return str(value), "excel number -> text"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value)), "excel number -> text"
        return repr(value), "excel number -> text"
    return str(value), "excel value -> text"


def read_table(path: Path, sheet: str | None = None) -> RawTable:
    path = Path(path)
    if not path.is_file():
        raise SourceError(f"file not found: {path}")
    if is_excel(path, sheet):
        return _read_excel(path, sheet)
    return _read_csv(path)


def _read_csv(path: Path) -> RawTable:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        t = RawTable(header=header, rows=[], row_numbers=[], source_rows=0)
        width = len(header)
        for line_no, row in enumerate(reader, start=2):
            if not row or all(c == "" for c in row) and len(row) <= 1:
                t.blank_rows_skipped += 1
                continue
            t.source_rows += 1
            if len(row) != width:
                t.rejected.append((line_no, f"has {len(row)} fields, header has {width} (truncated or malformed row)"))
                continue
            t.rows.append(list(row))
            t.row_numbers.append(line_no)
    return t


def _read_excel(path: Path, sheet: str | None) -> RawTable:
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        if sheet is None:
            sheet = wb.sheetnames[0]
        if sheet not in wb.sheetnames:
            raise SourceError(f"sheet '{sheet}' not in {path.name}")
        it = wb[sheet].iter_rows(values_only=True)
        first = next(it, ())
        header = [("" if v is None else str(v)) for v in first]
        width = len(header)
        t = RawTable(header=header, rows=[], row_numbers=[], source_rows=0)
        for row_no, values in enumerate(it, start=2):
            values = list(values)
            if all(v is None or (isinstance(v, str) and v == "") for v in values):
                t.blank_rows_skipped += 1
                continue
            t.source_rows += 1
            extra = values[width:]
            if any(v is not None for v in extra):
                t.rejected.append((row_no, f"has values beyond the {width} header columns"))
                continue
            values = (values + [None] * width)[:width]
            out = []
            for col, v in zip(header, values):
                text, kind = _cell_to_text(v)
                if kind:
                    c = t.coercions.setdefault(col, {})
                    c[kind] = c.get(kind, 0) + 1
                out.append(text)
            t.rows.append(out)
            t.row_numbers.append(row_no)
        return t
    finally:
        wb.close()
