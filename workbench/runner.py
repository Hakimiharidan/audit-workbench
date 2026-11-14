"""Run each mapped pack instance against role views in the locked DuckDB connection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from workbench.loader import LoadResult
from workbench.mapping import IDENT, resolve_source
from workbench.packs import SQL_TYPES, Pack, PackError, check_select_only
from workbench.sandbox import qi

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
PARAM_REF = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


class RunError(Exception):
    pass


@dataclass
class RunResult:
    run_id: str
    pack: Pack
    status: str                    # ok | skipped | failed
    reason: str = ""
    exceptions: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    systemic: str | None = None
    role_rows: dict[str, int] = field(default_factory=dict)


def _json_param(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, list):
        return [_json_param(x) for x in v]
    return v


def _contract_fields(spec: dict) -> dict[str, str]:
    return dict(spec.get("required") or {}) | dict(spec.get("optional") or {})


def run_packs(con, mapping: dict, packs: dict[str, Pack], load: LoadResult, mapping_path: Path) -> list[RunResult]:
    results = []
    for run in mapping.get("runs") or []:
        pack = packs.get(run.get("pack"))
        if pack is None:
            results.append(RunResult(run.get("id", "?"), Pack(run.get("pack", "?"), Path("."), {}, ""), "failed",
                                     f"pack '{run.get('pack')}' not found"))
            continue
        results.append(_run_one(con, mapping, pack, run, load, mapping_path))
    return results


def _run_one(con, mapping, pack: Pack, run: dict, load: LoadResult, mapping_path: Path) -> RunResult:
    res = RunResult(run_id=run["id"], pack=pack, status="failed")
    bind = run.get("bind") or {}
    created_views: list[str] = []
    created_tables: list[str] = []
    try:
        # skip when a bound dataset was not loaded
        for role, b in bind.items():
            ds = load.datasets.get(b.get("dataset"))
            if ds is None or ds.status != "loaded":
                res.status = "skipped"
                why = ds.reason if ds is not None else "not defined in the mapping"
                res.reason = f"dataset '{b.get('dataset')}' for role '{role}' is not available: {why}"
                return res

        params, values, field_lists = _resolve_params(pack, run, mapping_path)
        res.params = {k: _json_param(v) for k, v in params.items()}

        # role views
        datasets_fields = {n: d.fields for n, d in load.datasets.items()}
        for role, spec in pack.roles().items():
            cfields = _contract_fields(spec)
            b = bind.get(role)
            passthrough = [f for (r, names) in field_lists.values() if r == role for f in names if f not in cfields]
            if b is None:
                if role not in pack.optional_roles():
                    raise RunError(f"role '{role}' is not bound")
                cols = [f"CAST(NULL AS {SQL_TYPES[t]}) AS {qi(f)}" for f, t in cfields.items()]
                con.execute(f"CREATE OR REPLACE TEMP VIEW {qi(role)} AS SELECT {', '.join(cols)} WHERE false")
                created_views.append(role)
                res.role_rows[role] = 0
                continue
            dsname = b["dataset"]
            fmap = b.get("fields") or {}
            dsf = datasets_fields[dsname]
            cols = []
            for f, t in cfields.items():
                src = fmap.get(f)
                if src is None:
                    if f in (spec.get("required") or {}):
                        raise RunError(f"required field '{f}' of role '{role}' is not bound")
                    cols.append(f"CAST(NULL AS {SQL_TYPES[t]}) AS {qi(f)}")
                else:
                    if src not in dsf:
                        raise RunError(f"'{src}' is not a field of dataset '{dsname}'")
                    cols.append(f"{qi(src)} AS {qi(f)}")
            for f in passthrough:
                if f not in dsf:
                    raise RunError(f"'{f}' is not a field of dataset '{dsname}' (role '{role}')")
                cols.append(qi(f))
            table = load.datasets[dsname].table
            con.execute(f"CREATE OR REPLACE TEMP VIEW {qi(role)} AS SELECT {', '.join(cols)} FROM {qi(table)}")
            created_views.append(role)
            res.role_rows[role] = con.execute(f"SELECT count(*) FROM {qi(role)}").fetchone()[0]

        # date list files -> tables
        for name, spec in (pack.meta.get("parameters") or {}).items():
            if spec.get("type") == "date_list_file" and params.get(name) is not None:
                table = spec.get("table", name)
                rows = _read_date_list(resolve_source(mapping_path, params[name]))
                con.execute(f"CREATE OR REPLACE TEMP TABLE {qi(table)} (holiday_date DATE, holiday_name VARCHAR)")
                if rows:
                    con.executemany(f"INSERT INTO {qi(table)} VALUES (?, ?)", rows)
                created_tables.append(table)

        sql = _substitute(pack.sql, field_lists)
        check_select_only(sql)
        refs = set(PARAM_REF.findall(sql))
        bound = {k: v for k, v in values.items() if k in refs}
        missing = refs - set(bound)
        if missing:
            raise RunError(f"SQL uses parameter(s) with no value: {', '.join(sorted(missing))}")
        cur = con.execute(sql, bound) if bound else con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        for needed in ("exception_id", "amount_at_risk"):
            if needed not in cols:
                raise RunError(f"test.sql must return a column named '{needed}'")
        excs = [dict(zip(cols, r)) for r in rows]
        for e in excs:
            e["exception_id"] = str(e["exception_id"])
        ids = [e["exception_id"] for e in excs]
        if len(ids) != len(set(ids)):
            dups = sorted({i for i in ids if ids.count(i) > 1})[:5]
            raise RunError(f"exception_id values must be unique; repeated: {', '.join(dups)}")
        excs.sort(key=lambda e: e["exception_id"])
        res.exceptions, res.columns, res.status = excs, cols, "ok"
        res.systemic = _systemic_note(pack, res)
        return res
    except (RunError, PackError) as exc:
        res.status, res.reason = "failed", str(exc)
        return res
    except Exception as exc:  # duckdb errors, bad files: the run fails, others continue
        res.status, res.reason = "failed", f"{type(exc).__name__}: {exc}"
        return res
    finally:
        for v in created_views:
            con.execute(f"DROP VIEW IF EXISTS {qi(v)}")
        for t in created_tables:
            con.execute(f"DROP TABLE IF EXISTS {qi(t)}")


def _resolve_params(pack: Pack, run: dict, mapping_path: Path):
    declared = pack.meta.get("parameters") or {}
    given = run.get("params") or {}
    unknown = set(given) - set(declared)
    if unknown:
        raise RunError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    params, values, field_lists = {}, {}, {}
    for name, spec in declared.items():
        v = given.get(name, spec.get("default"))
        if v is None:
            if spec.get("required"):
                raise RunError(f"parameter '{name}' is required")
            continue
        t = spec["type"]
        if t == "decimal":
            v = Decimal(str(v))
        elif t == "field_list":
            role = spec["role"]
            if not v and spec.get("min_items", 1) > 0:
                raise RunError(f"parameter '{name}' needs at least one field")
            for f in v:
                if not isinstance(f, str) or not IDENT.match(f):
                    raise RunError(f"parameter '{name}': '{f}' is not a field name")
            field_lists[name] = (role, list(v))
        elif t == "date_list_file":
            params[name] = v
            continue
        params[name] = v
        values[name] = v
    return params, values, field_lists


def _substitute(sql: str, field_lists: dict) -> str:
    def repl(m):
        name = m.group(1)
        if name not in field_lists:
            raise RunError(f"'{{{{{name}}}}}' in test.sql is not a field_list parameter")
        return ", ".join(qi(f) for f in field_lists[name][1])
    return PLACEHOLDER.sub(repl, sql)


def _read_date_list(path: Path) -> list[tuple]:
    if not path.is_file():
        raise RunError(f"date list file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = data.get("dates", []) if isinstance(data, dict) else data
    rows = []
    for it in items or []:
        if isinstance(it, dict):
            rows.append((it["date"], str(it.get("name", ""))))
        else:
            rows.append((it, ""))
    return rows


def _systemic_note(pack: Pack, res: RunResult) -> str | None:
    note = pack.meta.get("systemic_note")
    if not note:
        return None
    total = res.role_rows.get(note["role"], 0)
    if not total:
        return None
    hits = sum(1 for e in res.exceptions if note["rule"] in str(e.get("rule", "")).split(";"))
    share = hits / total
    if share > float(note["threshold"]):
        return (f"systemic: {share:.0%} of {note['role']} rows hit rule '{note['rule']}' "
                f"({hits} of {total}) — likely one rule question for the client, not {hits} separate errors")
    return None
