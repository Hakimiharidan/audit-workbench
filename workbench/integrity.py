"""Integrity gate (IPE completeness and accuracy checks) on the mapped datasets.

Fail = structural problems that make test results unreliable.
Warn = findings that the tests themselves report (orphans, gaps, placeholders).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from workbench.loader import LoadResult
from workbench.sandbox import qi

LIST_LIMIT = 50


@dataclass
class Check:
    dataset: str
    check: str
    status: str            # info | pass | warn | fail
    detail: str
    value: str | None = None


@dataclass
class GateResult:
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> str:
        s = {c.status for c in self.checks}
        return "fail" if "fail" in s else "warn" if "warn" in s else "pass"

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def _fmt(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return f"{v:.2f}"
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _clip(items, limit=LIST_LIMIT) -> str:
    items = [str(i) for i in items]
    more = f" … (+{len(items) - limit} more)" if len(items) > limit else ""
    return ", ".join(items[:limit]) + more


def _key_expr(fields) -> str:
    if len(fields) == 1:
        return f"CAST({qi(fields[0])} AS VARCHAR)"
    return "concat_ws('|', " + ", ".join(f"CAST({qi(f)} AS VARCHAR)" for f in fields) + ")"


def run_gate(con, mapping: dict, load: LoadResult) -> GateResult:
    g = GateResult()
    add = g.checks.append
    for f in load.files:
        add(Check("(file)", "file_hash", "info", f"{f.path} · {f.size} bytes · modified {f.modified}", f.sha256))

    datasets = mapping.get("datasets") or {}
    for name, ds in datasets.items():
        d = load.datasets[name]
        if d.status != "loaded":
            add(Check(name, "dataset_loaded", "fail", f"dataset rejected: {d.reason}"))
            continue
        add(Check(name, "dataset_loaded", "pass", f"loaded from {d.file}" + (f" [{d.sheet}]" if d.sheet else "")))
        t = qi(d.table)

        # row counts
        if d.rejected_rows or d.source_rows != d.loaded_rows:
            rows = _clip(f"row {n}: {why}" for n, why in d.rejected_rows)
            add(Check(name, "row_count", "fail",
                      f"source rows {d.source_rows}, loaded {d.loaded_rows}; rejected: {rows}", str(len(d.rejected_rows))))
        else:
            add(Check(name, "row_count", "pass", f"source rows {d.source_rows} = loaded rows {d.loaded_rows}", str(d.loaded_rows)))
        if d.blank_rows_skipped:
            add(Check(name, "blank_rows", "info", f"{d.blank_rows_skipped} fully blank row(s) skipped", str(d.blank_rows_skipped)))
        if "expected_rows" in ds:
            exp = ds["expected_rows"]
            status = "pass" if exp == d.source_rows else "fail"
            add(Check(name, "expected_rows", status,
                      f"expected {exp} rows (declared with the extract), source has {d.source_rows}"
                      + ("" if status == "pass" else " — possible incomplete extract"), str(d.source_rows)))
        if d.ignored_columns:
            add(Check(name, "ignored_columns", "info", "not loaded: " + ", ".join(d.ignored_columns), str(len(d.ignored_columns))))
        for c in d.coercions:
            add(Check(name, "coercion", "info", c))

        # unparseable values
        for f, rows in sorted(d.unparseable.items()):
            add(Check(name, "unparseable", "fail",
                      f"{f}: {len(rows)} value(s) could not be read as {d.fields[f]} (source rows {_clip(rows)})", str(len(rows))))

        # primary key
        pk = ds.get("primary_key")
        if pk:
            k = _key_expr(pk)
            dups = con.execute(f"SELECT {k} AS k, count(*) FROM {t} GROUP BY 1 HAVING count(*) > 1 ORDER BY 1").fetchall()
            nullk = con.execute(f"SELECT count(*) FROM {t} WHERE " + " OR ".join(f"{qi(x)} IS NULL" for x in pk)).fetchone()[0]
            if dups:
                add(Check(name, "duplicate_primary_key", "fail",
                          f"{len(dups)} duplicated key(s) on ({', '.join(pk)}): {_clip(r[0] for r in dups)}", str(len(dups))))
            else:
                add(Check(name, "duplicate_primary_key", "pass", f"({', '.join(pk)}) unique", "0"))
            if nullk:
                add(Check(name, "null_primary_key", "fail", f"{nullk} row(s) with an empty key ({', '.join(pk)})", str(nullk)))
            distinct = con.execute(f"SELECT count(DISTINCT {k}) FROM {t}").fetchone()[0]
            add(Check(name, "control_total", "info", f"distinct({', '.join(pk)})", str(distinct)))

        # control totals
        sums = ds.get("control_totals")
        if sums is None:
            sums = [f for f, ty in d.fields.items() if ty == "decimal"]
        for f in sums:
            v = con.execute(f"SELECT sum({qi(f)}) FROM {t}").fetchone()[0]
            add(Check(name, "control_total", "info", f"sum({f})", _fmt(v if v is not None else Decimal(0))))
        for f, ty in d.fields.items():
            if ty == "date":
                lo, hi = con.execute(f"SELECT min({qi(f)}), max({qi(f)}) FROM {t}").fetchone()
                add(Check(name, "control_total", "info", f"min({f})", _fmt(lo)))
                add(Check(name, "control_total", "info", f"max({f})", _fmt(hi)))

        # placeholders and nulls
        n = d.loaded_rows or 1
        for f in d.fields:
            ph = d.placeholders.get(f, 0)
            nl = d.nulls.get(f, 0)
            if ph:
                add(Check(name, "placeholders", "warn",
                          f"{f}: {ph} placeholder value(s) converted to NULL ({ph / n:.1%})", str(ph)))
            if nl:
                add(Check(name, "nulls", "info", f"{f}: {nl} NULL value(s) ({nl / n:.1%}), placeholders included", str(nl)))

        # sequence gaps
        seq = ds.get("sequence")
        if seq:
            pattern = re.compile(seq.get("pattern", r"^\D*(\d+)$"))
            ids = [r[0] for r in con.execute(f"SELECT {qi(seq['field'])} FROM {t} WHERE {qi(seq['field'])} IS NOT NULL").fetchall()]
            nums, unmatched = set(), 0
            for v in ids:
                m = pattern.match(str(v))
                if m:
                    nums.add(int(m.group(1)))
                else:
                    unmatched += 1
            if nums:
                gaps = sorted(set(range(min(nums), max(nums) + 1)) - nums)
                add(Check(name, "sequence_gaps", "warn" if gaps else "pass",
                          (f"{len(gaps)} gap(s) in {seq['field']} between {min(nums)} and {max(nums)}: {_clip(gaps)}"
                           if gaps else f"no gaps in {seq['field']} between {min(nums)} and {max(nums)}"), str(len(gaps))))
            if unmatched:
                add(Check(name, "sequence_pattern", "warn", f"{unmatched} {seq['field']} value(s) do not match {pattern.pattern}", str(unmatched)))

        # balance check
        bc = ds.get("balance_check")
        if bc:
            tol = Decimal(str(bc.get("tolerance", "0.01")))
            k = _key_expr(bc["group_by"])
            rows = con.execute(
                f"SELECT {k}, sum({qi(bc['amount'])}) AS s FROM {t} GROUP BY 1 "
                f"HAVING abs(coalesce(sum({qi(bc['amount'])}), 0)) > {tol} ORDER BY 1").fetchall()
            if rows:
                add(Check(name, "balance", "fail",
                          f"Unbalanced: possible incomplete extract — {len(rows)} group(s) by ({', '.join(bc['group_by'])}) "
                          f"do not sum to zero within {tol}: " + _clip(f"{r[0]} ({_fmt(r[1])})" for r in rows), str(len(rows))))
            else:
                add(Check(name, "balance", "pass", f"every ({', '.join(bc['group_by'])}) group sums to zero within {tol}", "0"))

    # referential integrity, both directions
    for name, ds in datasets.items():
        for ref in ds.get("references") or []:
            child, parent = load.datasets[name], load.datasets.get(ref["to"])
            if child.status != "loaded" or parent is None or parent.status != "loaded":
                continue
            ck, pk = _key_expr(ref["fields"]), _key_expr(ref["to_fields"])
            ct, pt = qi(child.table), qi(parent.table)
            label = f"{name}({', '.join(ref['fields'])}) → {ref['to']}({', '.join(ref['to_fields'])})"
            orphans = [r[0] for r in con.execute(
                f"SELECT DISTINCT {ck} FROM {ct} c WHERE {ck} IS NOT NULL AND {ck} NOT IN "
                f"(SELECT {pk} FROM {pt} WHERE {pk} IS NOT NULL) ORDER BY 1").fetchall()]
            add(Check(name, "reference_orphans", "warn" if orphans else "pass",
                      f"{label}: {len(orphans)} value(s) with no parent" + (f": {_clip(orphans)}" if orphans else ""),
                      str(len(orphans))))
            unused = [r[0] for r in con.execute(
                f"SELECT DISTINCT {pk} FROM {pt} WHERE {pk} IS NOT NULL AND {pk} NOT IN "
                f"(SELECT {ck} FROM {ct} WHERE {ck} IS NOT NULL) ORDER BY 1").fetchall()]
            status = ("warn" if ref.get("require_children") else "info") if unused else "pass"
            add(Check(ref["to"], "reference_unused_parents", status,
                      f"{label}: {len(unused)} parent value(s) with no child" + (f": {_clip(unused)}" if unused else ""),
                      str(len(unused))))
    return g
