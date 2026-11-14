"""End-to-end run: validate → load → integrity gate → packs → materiality → outputs."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from workbench import __version__, sandbox
from workbench.integrity import run_gate
from workbench.loader import load_datasets
from workbench.mapping import load_mapping, validate_mapping
from workbench.materiality import Materiality, aggregate, band, sort_key
from workbench.packs import DEFAULT_PACKS_ROOT, discover_packs
from workbench.runlog import RunLog, json_safe
from workbench.runner import run_packs
from workbench.workpaper import write_workpaper


@dataclass
class Outcome:
    status: str                      # ok | gate_failed | invalid_mapping | invalid_config
    run_id: str | None = None
    workpaper: Path | None = None
    exceptions_file: Path | None = None
    runlog_file: Path | None = None
    messages: list[str] = field(default_factory=list)
    runs: list = field(default_factory=list)
    gate: object = None


def _json_value(v):
    if isinstance(v, Decimal):
        return f"{v:.2f}" if v == v.quantize(Decimal("0.01")) else format(v, "f")
    return json_safe(v)


def exceptions_jsonl(runs: list[dict]) -> str:
    """Deterministic: sorted by run id then exception id, sorted keys, no timestamps."""
    lines = []
    for run in sorted(runs, key=lambda r: r["run_id"]):
        for e in sorted(run["exceptions"], key=lambda e: e["exception_id"]):
            obj = {k: _json_value(v) for k, v in e.items()}
            obj["run_id"] = run["run_id"]
            lines.append(json.dumps(obj, sort_keys=True, ensure_ascii=False))
    return "".join(line + "\n" for line in lines)


def _run_id(mapping_path: Path, config_path: Path, files, packs) -> str:
    h = hashlib.sha256()
    h.update(__version__.encode())
    h.update(Path(mapping_path).read_bytes())
    h.update(Path(config_path).read_bytes())
    for f in files:
        h.update(f.sha256.encode())
    for key in sorted(packs):
        h.update(key.encode())
        h.update(packs[key].sql.encode())
        h.update(json.dumps(packs[key].meta, sort_keys=True, default=str).encode())
    return h.hexdigest()[:12]


def run_workbench(mapping_path, config_path, out_dir, engagement: str = "", force: bool = False,
                  packs_root=None) -> Outcome:
    mapping_path, config_path, out_dir = Path(mapping_path), Path(config_path), Path(out_dir)
    packs_root = Path(packs_root) if packs_root else DEFAULT_PACKS_ROOT
    out = Outcome(status="invalid_mapping")
    log = RunLog()

    issues = validate_mapping(mapping_path, packs_root)
    if issues:
        out.messages = [str(i) for i in issues] + [f"{len(issues)} problem(s) in the mapping; nothing was run."]
        # A missing column is a gate failure (the dataset is rejected), not a reason to stop here.
        structural = [i for i in issues if not _is_header_issue(i)]
        if structural:
            return out
        out.messages = []
        for i in issues:
            log.event("mapping_warning", str(i))

    try:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        mat = Materiality.from_config(cfg)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        out.status = "invalid_config"
        out.messages = [f"{config_path}: {exc}"]
        return out

    mapping = load_mapping(mapping_path)
    packs = discover_packs(packs_root)
    con = sandbox.connect()
    load = load_datasets(con, mapping, mapping_path)
    for name, d in load.datasets.items():
        if d.status != "loaded":
            log.event("dataset_rejected", f"{name}: {d.reason}")
        for c in d.coercions:
            log.event("coercion", f"{name}.{c}")
        for f, n in d.placeholders.items():
            log.event("placeholder", f"{name}.{f}: {n} placeholder value(s) converted to NULL")
        for row, why in d.rejected_rows:
            log.event("row_rejected", f"{name}: source row {row} {why}")
        if d.ignored_columns:
            log.event("columns_ignored", f"{name}: not loaded: {', '.join(d.ignored_columns)}")
    gate = run_gate(con, mapping, load)
    sandbox.lock(con)
    out.gate = gate
    for c in gate.checks:
        if c.status in ("warn", "fail"):
            log.event(f"gate_{c.status}", f"{c.dataset}/{c.check}: {c.detail}")

    run_id = _run_id(mapping_path, config_path, load.files, packs)
    out.run_id = run_id
    timestamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    log.event("run_start", f"run {run_id}", mapping=str(mapping_path), config=str(config_path),
              materiality={"overall": mat.overall, "performance": mat.performance, "trivial": mat.trivial})

    force_used = False
    results = []
    if gate.failed and not force:
        log.event("stopped", "integrity gate failed; tests not run (use --force to run anyway; the force is recorded)")
        out.status = "gate_failed"
    else:
        if gate.failed:
            force_used = True
            log.event("force", "--force used: tests run although the integrity gate failed")
        results = run_packs(con, mapping, packs, load, mapping_path)
        out.status = "ok"

    runs_ctx = []
    for r in results:
        excs = []
        for e in r.exceptions:
            e = dict(e)
            rule = e.get("rule")
            e["band"] = band(e.get("amount_at_risk"), mat)
            e["label"] = r.pack.label_for(rule)
            e["needs_client_confirmation"] = r.pack.confirmation_for(rule)
            excs.append(e)
        excs.sort(key=sort_key)
        agg = aggregate(excs, mat)
        meta = r.pack.meta
        conf = meta.get("needs_client_confirmation")
        needs = any(conf.values()) if isinstance(conf, dict) else bool(conf)
        runs_ctx.append({
            "run_id": r.run_id, "name": meta.get("name", r.pack.key), "pack": r.pack.key,
            "assertion": meta.get("assertion"), "category": meta.get("category"),
            "status": r.status, "reason": r.reason, "columns": r.columns, "exceptions": excs,
            "aggregate": agg, "systemic": r.systemic, "limitations": meta.get("limitations"),
            "needs_confirmation": needs, "params": r.params,
        })
        log.event(f"run_{r.status}", f"{r.run_id} ({r.pack.key}): {r.status}"
                  + (f" — {r.reason}" if r.reason else f", {agg['count']} exception(s)"), params=r.params)

    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = {
        "engagement": engagement, "run_id": run_id, "version": __version__, "timestamp": timestamp,
        "mapping_path": str(mapping_path), "config_path": str(config_path), "materiality": mat,
        "gate": gate, "force_used": force_used, "files": load.files, "runs": runs_ctx, "log": log,
    }
    out.workpaper = out_dir / "workpaper.xlsx"
    write_workpaper(out.workpaper, ctx)
    if out.status == "ok":
        out.exceptions_file = out_dir / "exceptions.jsonl"
        out.exceptions_file.write_text(exceptions_jsonl(runs_ctx), encoding="utf-8")
    out.runlog_file = out_dir / "runlog.json"
    out.runlog_file.write_text(log.to_json({
        "run_id": run_id, "version": __version__, "timestamp": timestamp, "engagement": engagement,
        "force_used": force_used, "gate_status": gate.status, "inputs": [f.__dict__ for f in load.files],
    }), encoding="utf-8")
    out.runs = runs_ctx

    out.messages.append(f"Integrity gate: {gate.status}" + (" (forced)" if force_used else ""))
    if out.status == "gate_failed":
        for c in gate.checks:
            if c.status == "fail":
                out.messages.append(f"  FAIL {c.dataset}/{c.check}: {c.detail}")
        out.messages.append("Tests were not run. Fix the extract, or re-run with --force (recorded in the workpaper).")
    for rc in runs_ctx:
        a = rc["aggregate"]
        extra = f" — {rc['reason']}" if rc["reason"] else f": {a['count']} exception(s), at risk {a['total']:,.2f}, highest band {a['highest_band']}"
        out.messages.append(f"  {rc['run_id']:<10} {rc['status']}{extra}")
    out.messages.append(f"Workpaper: {out.workpaper}")
    if out.exceptions_file:
        out.messages.append(f"Exceptions list: {out.exceptions_file}")
    return out


def _is_header_issue(issue) -> bool:
    return ".columns." in issue.path and issue.path.endswith(".from") and "not in the source header" in issue.message
