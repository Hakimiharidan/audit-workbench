"""Load mapped datasets into DuckDB as typed tables, counting every change made to the data.

Rules (design §5.3): read as text; placeholders converted explicitly and counted;
types converted in DuckDB from text with TRY_CAST; anything that fails to convert
is listed by source row; missing mapped columns reject the dataset; unmapped
columns are never loaded.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from workbench.mapping import placeholders_for, resolve_source
from workbench.sandbox import qi
from workbench.sources import SourceError, read_table


@dataclass
class FileInfo:
    path: str
    sha256: str
    size: int
    modified: str


@dataclass
class DatasetLoad:
    name: str
    status: str                      # loaded | rejected
    reason: str = ""
    file: str = ""
    sheet: str | None = None
    source_rows: int = 0
    loaded_rows: int = 0
    rejected_rows: list[tuple[int, str]] = field(default_factory=list)
    blank_rows_skipped: int = 0
    ignored_columns: list[str] = field(default_factory=list)
    placeholders: dict[str, int] = field(default_factory=dict)
    nulls: dict[str, int] = field(default_factory=dict)
    unparseable: dict[str, list[int]] = field(default_factory=dict)
    coercions: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def table(self) -> str:
        return f"ds_{self.name}"


@dataclass
class LoadResult:
    files: list[FileInfo]
    datasets: dict[str, DatasetLoad]


def file_info(path: Path, label: str) -> FileInfo:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    st = path.stat()
    modified = dt.datetime.fromtimestamp(st.st_mtime, tz=dt.timezone.utc).replace(microsecond=0).isoformat()
    return FileInfo(path=label, sha256=h.hexdigest(), size=st.st_size, modified=modified)


def _cast_expr(col: str, ftype: str, fmt: str | None) -> str:
    c = f"CAST({qi(col)} AS VARCHAR)"
    if ftype == "string":
        return c
    if ftype == "integer":
        return f"TRY_CAST({c} AS BIGINT)"
    if ftype == "decimal":
        return f"TRY_CAST({c} AS DECIMAL(18,2))"
    if ftype == "ratio":
        return f"TRY_CAST({c} AS DECIMAL(18,6))"
    if ftype == "date":
        if fmt:
            lit = "'" + fmt.replace("'", "''") + "'"
            return f"CAST(TRY_STRPTIME({c}, {lit}) AS DATE)"
        return f"CAST(TRY_CAST({c} AS TIMESTAMP) AS DATE)"
    if ftype == "boolean":
        return (f"CASE WHEN lower({c}) IN ('true','yes','y','1') THEN TRUE "
                f"WHEN lower({c}) IN ('false','no','n','0') THEN FALSE ELSE NULL END")
    raise ValueError(f"unsupported type {ftype}")


PLACES = {"decimal": 2, "ratio": 6}
NOISE = {2: Decimal("0.000001"), 6: Decimal("0.000000001")}
# Marker text for a value that must count as unparseable; no cast accepts it.
_REJECT = "\x00excess-precision"


def _check_precision(values: list, places: int = 2) -> tuple[list, int]:
    """Decimals must have at most `places` places (2 for decimal, 6 for ratio). Float noise
    (|v - round(v)| < 1e-6, or 1e-9 for ratio) is rounded and counted; a genuine extra digit
    (12.345 as a decimal) is made unparseable so the gate fails on it."""
    quantum = Decimal(1).scaleb(-places)
    out, noise = [], 0
    for v in values:
        if v is None:
            out.append(v)
            continue
        try:
            dec = Decimal(v)
        except (InvalidOperation, ValueError):
            out.append(v)                      # left for TRY_CAST to reject
            continue
        if not dec.is_finite():
            out.append(_REJECT)
            continue
        rounded = dec.quantize(quantum, rounding=ROUND_HALF_UP)
        if rounded == dec:
            out.append(v)
        elif abs(dec - rounded) < NOISE[places]:
            out.append(str(rounded))
            noise += 1
        else:
            out.append(_REJECT)
    return out, noise


def load_datasets(con, mapping: dict, mapping_path: Path) -> LoadResult:
    files: dict[str, FileInfo] = {}
    out: dict[str, DatasetLoad] = {}
    for name, ds in (mapping.get("datasets") or {}).items():
        src = ds["source"]
        fpath = resolve_source(mapping_path, src["file"])
        sheet = src.get("sheet")
        d = DatasetLoad(name=name, status="rejected", file=str(src["file"]), sheet=sheet,
                        fields={f: s.get("type", "string") for f, s in ds["columns"].items()})
        out[name] = d
        if not fpath.is_file():
            d.reason = f"source file not found: {src['file']}"
            continue
        if src["file"] not in files:
            files[src["file"]] = file_info(fpath, str(src["file"]))
        try:
            raw = read_table(fpath, sheet)
        except SourceError as exc:
            d.reason = str(exc)
            continue
        d.source_rows = raw.source_rows
        d.rejected_rows = list(raw.rejected)
        d.blank_rows_skipped = raw.blank_rows_skipped
        mapped_from = {f: str(s["from"]) for f, s in ds["columns"].items()}
        missing = [m for m in mapped_from.values() if m not in raw.header]
        if missing:
            d.reason = f"mapped column(s) missing from source: {', '.join(missing)}"
            continue
        d.ignored_columns = [h for h in raw.header if h not in set(mapped_from.values())]
        for h in raw.header:
            if raw.header.count(h) > 1 and h in mapped_from.values():
                d.reason = f"source header '{h}' appears more than once"
        if d.reason:
            continue

        placeholders = set(placeholders_for(mapping, ds))
        idx = {h: i for i, h in enumerate(raw.header)}
        data: dict[str, list] = {"_row": raw.row_numbers}
        for f, col in mapped_from.items():
            values = [r[idx[col]] for r in raw.rows]
            n_ph = sum(1 for v in values if v is not None and v in placeholders)
            if n_ph:
                d.placeholders[f] = n_ph
            values = [None if (v is None or v in placeholders) else v for v in values]
            n_null = sum(1 for v in values if v is None)
            if n_null:
                d.nulls[f] = n_null
            if d.fields[f] in PLACES:
                places = PLACES[d.fields[f]]
                values, noise = _check_precision(values, places)
                if noise:
                    d.coercions.append(
                        f"{f}: {noise} value(s) rounded to {places} decimal places (float noise below {NOISE[places]})")
            data[f] = values
            for kind, n in sorted(raw.coercions.get(col, {}).items()):
                d.coercions.append(f"{f}: {n} cell(s) {kind}")
        df = pd.DataFrame(data, dtype=object)
        df["_row"] = df["_row"].astype("int64")
        raw_name = f"raw_{name}"
        con.register(raw_name, df)
        selects = [f"{qi('_row')}"]
        for f, spec in ds["columns"].items():
            selects.append(f"{_cast_expr(f, spec.get('type', 'string'), spec.get('format'))} AS {qi(f)}")
        con.execute(f"CREATE OR REPLACE TABLE {qi(d.table)} AS SELECT {', '.join(selects)} FROM {qi(raw_name)} ORDER BY _row")
        for f, spec in ds["columns"].items():
            ftype = spec.get("type", "string")
            if ftype == "string":
                continue
            expr = _cast_expr(f, ftype, spec.get("format"))
            bad = con.execute(
                f"SELECT _row FROM {qi(raw_name)} WHERE {qi(f)} IS NOT NULL AND ({expr}) IS NULL ORDER BY _row"
            ).fetchall()
            if bad:
                d.unparseable[f] = [r[0] for r in bad]
        con.unregister(raw_name)
        d.loaded_rows = con.execute(f"SELECT count(*) FROM {qi(d.table)}").fetchone()[0]
        d.status = "loaded"
    return LoadResult(files=sorted(files.values(), key=lambda f: f.path), datasets=out)
