"""Mapping files: parse and validate (layer 1 `datasets`, layer 2 `runs`).

`validate_mapping` never loads data rows; it reads only headers. It collects
every problem in one pass so the user can fix them all at once.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from workbench.packs import FIELD_TYPES, PackError, discover_packs, validate_meta
from workbench.sources import SourceError, list_sheets, read_header, is_excel

DEFAULT_PLACEHOLDERS = ["N/A", ""]
DATASET_KEYS = {
    "source", "columns", "primary_key", "references", "sequence", "control_totals",
    "balance_check", "expected_rows", "null_placeholders", "description",
}
RUN_KEYS = {"id", "pack", "bind", "params", "description"}
SHEET_BAD_CHARS = set('[]:*?/\\')
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MappingError(Exception):
    pass


@dataclass
class MappingIssue:
    file: str
    path: str
    message: str
    fix: str

    def __str__(self) -> str:
        return f"{self.file}:{self.path}: {self.message} — {self.fix}"


def load_mapping(path: Path) -> dict:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MappingError(f"mapping file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise MappingError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise MappingError("the mapping must be a YAML mapping with 'datasets' and 'runs'")
    return data


def resolve_source(mapping_path: Path, file: str) -> Path:
    p = Path(file)
    return p if p.is_absolute() else (Path(mapping_path).resolve().parent / p)


def placeholders_for(mapping: dict, ds: dict) -> list[str]:
    if "null_placeholders" in ds:
        return list(ds["null_placeholders"] or [])
    return list(mapping.get("null_placeholders", DEFAULT_PLACEHOLDERS) or [])


class _Collector:
    def __init__(self, file: Path):
        self.file = str(file)
        self.issues: list[MappingIssue] = []

    def add(self, path: str, message: str, fix: str) -> None:
        self.issues.append(MappingIssue(self.file, path, message, fix))


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate_mapping(path: Path, packs_root: Path, check_sources: bool = True) -> list[MappingIssue]:
    path = Path(path)
    c = _Collector(path)
    try:
        mapping = load_mapping(path)
    except MappingError as exc:
        c.add("(file)", str(exc).replace("invalid YAML", "invalid YAML syntax"),
              "fix the YAML syntax at the line shown (indentation, quotes, brackets)")
        return c.issues

    if mapping.get("mapping_version") != 1:
        c.add("mapping_version", f"expected 1, found {mapping.get('mapping_version')!r}",
              "add 'mapping_version: 1' at the top")
    ph = mapping.get("null_placeholders", DEFAULT_PLACEHOLDERS)
    if not _is_str_list(ph):
        c.add("null_placeholders", "must be a list of strings", 'e.g. null_placeholders: ["N/A", ""]')

    datasets = mapping.get("datasets")
    if not isinstance(datasets, dict) or not datasets:
        c.add("datasets", "missing or empty", "add at least one dataset under 'datasets:'")
        datasets = {}
    fields_by_ds: dict[str, dict[str, str]] = {}
    for name, ds in datasets.items():
        fields_by_ds[name] = _validate_dataset(c, path, name, ds, check_sources)
    for name, ds in datasets.items():
        if isinstance(ds, dict):
            _validate_dataset_links(c, name, ds, fields_by_ds)

    try:
        packs = discover_packs(packs_root)
    except PackError as exc:
        c.add("(packs)", str(exc), "fix the pack's meta.yaml")
        packs = {}
    runs = mapping.get("runs")
    if runs is None:
        runs = []
    if not isinstance(runs, list):
        c.add("runs", "must be a list", "write each run as '- id: ...'")
        runs = []
    seen: set[str] = set()
    for i, run in enumerate(runs):
        _validate_run(c, i, run, packs, fields_by_ds, seen)
    return c.issues


def _validate_dataset(c: _Collector, mpath: Path, name: str, ds, check_sources: bool) -> dict[str, str]:
    base = f"datasets.{name}"
    if not IDENT.match(str(name)):
        c.add(base, f"dataset name '{name}' is not a simple identifier", "use letters, digits and _ only")
    if not isinstance(ds, dict):
        c.add(base, "must be a mapping", "give it 'source' and 'columns'")
        return {}
    for key in ds:
        if key not in DATASET_KEYS:
            c.add(f"{base}.{key}", f"unknown key '{key}'", f"allowed keys: {', '.join(sorted(DATASET_KEYS))}")
    fields: dict[str, str] = {}
    cols = ds.get("columns")
    if not isinstance(cols, dict) or not cols:
        c.add(f"{base}.columns", "missing or empty", "map each field: {from: <source header>, type: <type>}")
        cols = {}
    for field, spec in cols.items():
        fpath = f"{base}.columns.{field}"
        if not IDENT.match(str(field)):
            c.add(fpath, f"field name '{field}' is not a simple identifier", "use letters, digits and _ only")
        if not isinstance(spec, dict) or "from" not in spec:
            c.add(fpath, "needs 'from' (source column) and 'type'", "e.g. {from: SaleDate, type: date}")
            continue
        ftype = spec.get("type", "string")
        if ftype not in FIELD_TYPES:
            c.add(f"{fpath}.type", f"type '{ftype}' is not supported",
                  f"use one of: {', '.join(sorted(FIELD_TYPES))}")
        if "format" in spec and ftype != "date":
            c.add(f"{fpath}.format", "'format' only applies to date fields", "remove it or set type: date")
        fields[field] = ftype

    src = ds.get("source")
    if not isinstance(src, dict) or not src.get("file"):
        c.add(f"{base}.source", "needs 'file' (and 'sheet' for Excel)", "e.g. source: {file: ../data/x.csv}")
    elif check_sources:
        fpath = resolve_source(mpath, src["file"])
        sheet = src.get("sheet")
        if not fpath.is_file():
            c.add(f"{base}.source.file", f"file not found: {fpath}",
                  "paths are relative to the mapping file's folder")
        elif is_excel(fpath, sheet) and sheet is None:
            c.add(f"{base}.source.sheet", "Excel source needs a sheet name",
                  f"available sheets: {', '.join(list_sheets(fpath))}")
        else:
            try:
                header = read_header(fpath, sheet)
            except SourceError as exc:
                c.add(f"{base}.source.sheet", str(exc), "use one of the sheet names shown")
                header = None
            if header is not None:
                for field, spec in cols.items():
                    if isinstance(spec, dict) and "from" in spec and str(spec["from"]) not in header:
                        near = difflib.get_close_matches(str(spec["from"]), header, n=1, cutoff=0.6)
                        if not near:
                            near = [h for h in header if h.strip().lower() == str(spec["from"]).strip().lower()]
                        hint = f"did you mean '{near[0]}'?" if near else f"source columns: {', '.join(header)}"
                        c.add(f"{base}.columns.{field}.from",
                              f"column '{spec['from']}' not in the source header", hint)
    if "expected_rows" in ds and (not isinstance(ds["expected_rows"], int) or ds["expected_rows"] < 0):
        c.add(f"{base}.expected_rows", "must be a whole number", "the record count reported with the extract")
    if "null_placeholders" in ds and not _is_str_list(ds["null_placeholders"]):
        c.add(f"{base}.null_placeholders", "must be a list of strings", 'e.g. ["N/A", ""]')
    return fields


def _need_fields(c, path, value, fields, what="field"):
    if not _is_str_list(value) or not value:
        c.add(path, f"must be a non-empty list of {what} names", "e.g. [sale_id]")
        return
    for f in value:
        if f not in fields:
            c.add(path, f"'{f}' is not a field of this dataset", f"fields: {', '.join(fields) or '(none)'}")


def _validate_dataset_links(c: _Collector, name: str, ds: dict, fields_by_ds: dict) -> None:
    base = f"datasets.{name}"
    fields = fields_by_ds.get(name, {})
    if "primary_key" in ds:
        _need_fields(c, f"{base}.primary_key", ds["primary_key"], fields)
    for i, ref in enumerate(ds.get("references") or []):
        rpath = f"{base}.references[{i}]"
        if not isinstance(ref, dict):
            c.add(rpath, "must be {fields, to, to_fields}", "e.g. {fields: [employee_id], to: employees, to_fields: [employee_id]}")
            continue
        _need_fields(c, f"{rpath}.fields", ref.get("fields"), fields)
        target = ref.get("to")
        if target not in fields_by_ds:
            c.add(f"{rpath}.to", f"dataset '{target}' is not defined", f"datasets: {', '.join(fields_by_ds)}")
        else:
            _need_fields(c, f"{rpath}.to_fields", ref.get("to_fields"), fields_by_ds[target])
            if _is_str_list(ref.get("fields")) and _is_str_list(ref.get("to_fields")) and len(ref["fields"]) != len(ref["to_fields"]):
                c.add(rpath, "fields and to_fields have different lengths", "list the same number of fields on both sides")
    seq = ds.get("sequence")
    if seq is not None:
        if not isinstance(seq, dict) or seq.get("field") not in fields:
            c.add(f"{base}.sequence.field", "must name a field of this dataset", "e.g. sequence: {field: sale_id, pattern: '^S(\\d+)$'}")
        pat = seq.get("pattern") if isinstance(seq, dict) else None
        if pat is not None:
            try:
                if re.compile(pat).groups != 1:
                    c.add(f"{base}.sequence.pattern", "needs exactly one capture group around the number", "e.g. '^S(\\d+)$'")
            except re.error as exc:
                c.add(f"{base}.sequence.pattern", f"invalid regex: {exc}", "e.g. '^S(\\d+)$'")
    if "control_totals" in ds:
        _need_fields(c, f"{base}.control_totals", ds["control_totals"], fields)
        for f in ds["control_totals"] or []:
            if f in fields and fields[f] not in ("decimal", "integer", "ratio"):
                c.add(f"{base}.control_totals", f"'{f}' is {fields[f]}, not a number", "list decimal or integer fields")
    bc = ds.get("balance_check")
    if bc is not None:
        if not isinstance(bc, dict):
            c.add(f"{base}.balance_check", "must be {group_by, amount, tolerance}", "e.g. {group_by: [je_id], amount: amount, tolerance: 0.01}")
        else:
            _need_fields(c, f"{base}.balance_check.group_by", bc.get("group_by"), fields)
            if fields.get(bc.get("amount")) != "decimal":
                c.add(f"{base}.balance_check.amount", "must name a decimal field", "e.g. amount: amount")
            tol = bc.get("tolerance", 0.01)
            if not isinstance(tol, (int, float, str)):
                c.add(f"{base}.balance_check.tolerance", "must be a number", "e.g. 0.01")


def _validate_run(c: _Collector, i: int, run, packs: dict, fields_by_ds: dict, seen: set) -> None:
    base = f"runs[{i}]"
    if not isinstance(run, dict):
        c.add(base, "must be a mapping", "give it id, pack, bind and params")
        return
    for key in run:
        if key not in RUN_KEYS:
            c.add(f"{base}.{key}", f"unknown key '{key}'", f"allowed keys: {', '.join(sorted(RUN_KEYS))}")
    rid = run.get("id")
    if not isinstance(rid, str) or not rid:
        c.add(f"{base}.id", "missing", "give each run a short id such as REV-01")
    else:
        if len(rid) > 31:
            c.add(f"{base}.id", f"'{rid}' is longer than 31 characters (Excel sheet name limit)", "shorten the id")
        bad = SHEET_BAD_CHARS & set(rid)
        if bad:
            c.add(f"{base}.id", f"'{rid}' contains {''.join(sorted(bad))}, not allowed in sheet names", "use letters, digits, - and _")
        if rid.lower() in {s.lower() for s in seen}:
            c.add(f"{base}.id", f"duplicate run id '{rid}'", "each run id must be unique")
        if rid.lower() in {"cover", "integrity", "summary", "limitations", "run log"}:
            c.add(f"{base}.id", f"'{rid}' is a reserved sheet name", "choose another id")
        seen.add(rid)
    key = run.get("pack")
    pack = packs.get(key)
    if pack is None:
        c.add(f"{base}.pack", f"pack '{key}' not found", f"available packs: {', '.join(packs) or '(none)'}")
        return
    for p in validate_meta(pack.meta):
        c.add(f"{base}.pack", f"pack '{key}' meta.yaml: {p}", "fix the pack's meta.yaml")
    contract = pack.roles()
    bind = run.get("bind") or {}
    if not isinstance(bind, dict):
        c.add(f"{base}.bind", "must map each contract role to {dataset, fields}", "see the pack's meta.yaml contract")
        bind = {}
    for role in bind:
        if role not in contract:
            c.add(f"{base}.bind.{role}", f"'{role}' is not a role of pack '{key}'", f"roles: {', '.join(contract)}")
    bound_ds_fields: dict[str, dict[str, str]] = {}
    for role, spec in contract.items():
        rpath = f"{base}.bind.{role}"
        b = bind.get(role)
        if b is None:
            if role not in pack.optional_roles():
                c.add(rpath, f"role '{role}' is not bound", f"bind it to a dataset: {{dataset: ..., fields: {{...}}}}")
            continue
        if not isinstance(b, dict) or "dataset" not in b:
            c.add(rpath, "needs 'dataset' and 'fields'", "e.g. {dataset: sales, fields: {record_id: sale_id}}")
            continue
        dsname = b["dataset"]
        if dsname not in fields_by_ds:
            c.add(f"{rpath}.dataset", f"dataset '{dsname}' is not defined", f"datasets: {', '.join(fields_by_ds)}")
            continue
        dsf = fields_by_ds[dsname]
        bound_ds_fields[role] = dsf
        fmap = b.get("fields") or {}
        required = spec.get("required") or {}
        optional = spec.get("optional") or {}
        for cf in fmap:
            if cf not in required and cf not in optional:
                c.add(f"{rpath}.fields.{cf}", f"'{cf}' is not a field of role '{role}'",
                      f"contract fields: {', '.join(list(required) + list(optional))}")
        for cf, ctype in list(required.items()) + list(optional.items()):
            src = fmap.get(cf)
            if src is None:
                if cf in required:
                    c.add(f"{rpath}.fields", f"required contract field '{cf}' ({ctype}) is not bound",
                          f"add '{cf}: <field of {dsname}>'")
                continue
            if src not in dsf:
                c.add(f"{rpath}.fields.{cf}", f"'{src}' is not a field of dataset '{dsname}'",
                      f"fields: {', '.join(dsf)}")
            elif dsf[src] != ctype:
                c.add(f"{rpath}.fields.{cf}", f"contract needs {ctype} but '{src}' is {dsf[src]}",
                      f"change the dataset field type or bind a {ctype} field")
    params = run.get("params") or {}
    if not isinstance(params, dict):
        c.add(f"{base}.params", "must be a mapping", "e.g. params: {tolerance: 1.00}")
        return
    declared = pack.meta.get("parameters") or {}
    for pname, value in params.items():
        ppath = f"{base}.params.{pname}"
        if pname not in declared:
            c.add(ppath, f"pack '{key}' has no parameter '{pname}'", f"parameters: {', '.join(declared) or '(none)'}")
            continue
        _validate_param(c, ppath, declared[pname], value, bind, bound_ds_fields, contract)


def _validate_param(c, ppath, spec, value, bind, bound_ds_fields, contract) -> None:
    ptype = spec.get("type")
    ok = True
    if ptype == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif ptype == "decimal":
        ok = isinstance(value, (int, float, str)) and not isinstance(value, bool)
        if ok and isinstance(value, str):
            ok = re.fullmatch(r"-?\d+(\.\d+)?", value) is not None
    elif ptype == "string":
        ok = isinstance(value, str)
    elif ptype == "boolean":
        ok = isinstance(value, bool)
    elif ptype in ("string_list", "field_list", "date_list_file"):
        ok = _is_str_list(value) if ptype != "date_list_file" else isinstance(value, str)
    elif ptype == "integer_list":
        ok = isinstance(value, list) and all(isinstance(x, int) and not isinstance(x, bool) for x in value)
    if not ok:
        c.add(ppath, f"value {value!r} is not a valid {ptype}", "see the pack's meta.yaml parameters")
        return
    if ptype == "field_list":
        role = spec.get("role")
        allowed = set((contract.get(role) or {}).get("required") or {}) | set((contract.get(role) or {}).get("optional") or {})
        allowed |= set(bound_ds_fields.get(role, {}))
        for f in value:
            if not IDENT.match(f) or f not in allowed:
                c.add(ppath, f"'{f}' is not a field available to role '{role}'",
                      f"use contract fields or fields of the bound dataset: {', '.join(sorted(allowed))}")
