"""Acceptance criteria for v0.1 (design §5.11), on the shipped synthetic data."""
import csv
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path

import openpyxl
import pytest
import yaml

from conftest import REPO, write_yaml
from workbench.cli import main
from workbench.pipeline import run_workbench
from workbench.workpaper import DISPOSITIONS, REVIEW_COLUMNS, SIGNOFF_FIELDS

RETAIL = REPO / "mappings" / "example_retail.yaml"
GL = REPO / "mappings" / "example_gl.yaml"
CONFIG = REPO / "config.yaml"
PLANTED = json.loads((REPO / "sample_data" / "planted_issues.json").read_text())["runs"]


def portable_mapping(src: Path, dest_dir: Path, edit=None) -> Path:
    """Copy a mapping into dest_dir with absolute source paths, optionally editing it."""
    m = yaml.safe_load(src.read_text())
    for ds in m["datasets"].values():
        ds["source"]["file"] = str((src.parent / ds["source"]["file"]).resolve())
    for run in m.get("runs") or []:
        if "holiday_file" in (run.get("params") or {}):
            run["params"]["holiday_file"] = str((src.parent / run["params"]["holiday_file"]).resolve())
    if edit:
        edit(m)
    return write_yaml(dest_dir / src.name, m)


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    out = {}
    for name, mp in (("retail", RETAIL), ("gl", GL)):
        t0 = time.perf_counter()
        o = run_workbench(mp, CONFIG, tmp_path_factory.mktemp(name), engagement="Acceptance (synthetic)")
        o.seconds = time.perf_counter() - t0
        out[name] = o
    return out


def found_ids(outcome):
    return {r["run_id"]: {e["exception_id"] for e in r["exceptions"]} for r in outcome.runs}


# 2. recall 100% and extra exceptions reported
@pytest.mark.parametrize("name", ["retail", "gl"])
def test_recall_every_planted_issue(outcomes, name, capsys):
    o = outcomes[name]
    assert o.status == "ok"
    found = found_ids(o)
    report = []
    for run_id, ids in found.items():
        planted = set(PLANTED[run_id])
        missed = planted - ids
        assert not missed, f"{run_id} missed {sorted(missed)[:10]}"
        report.append(f"{run_id}: recall {len(planted)}/{len(planted)} = 100%, extra exceptions {len(ids - planted)}")
    with capsys.disabled():
        print(f"\n[{name}] " + " | ".join(report))


def test_all_planted_runs_covered(outcomes):
    covered = set(found_ids(outcomes["retail"])) | set(found_ids(outcomes["gl"]))
    assert set(PLANTED) <= covered


# 3. byte-identical exceptions list on re-run
@pytest.mark.parametrize("mapping", [RETAIL, GL])
def test_byte_identical_rerun(mapping, tmp_path):
    a = run_workbench(mapping, CONFIG, tmp_path / "a")
    b = run_workbench(mapping, CONFIG, tmp_path / "b")
    assert a.exceptions_file.read_bytes() == b.exceptions_file.read_bytes()
    assert a.run_id == b.run_id
    assert a.exceptions_file.stat().st_size > 0


# 4. a full run opens no network connections
def test_no_network(monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError(f"network access attempted: {args!r}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket.socket, "sendto", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    for mapping in (RETAIL, GL):
        assert run_workbench(mapping, CONFIG, tmp_path / mapping.stem).status == "ok"


# 5. the integrity gate catches deliberately corrupted files
def _corrupt_gl(tmp_path, how):
    rows = list(csv.reader(open(REPO / "sample_data" / "gl.csv", newline="")))
    header, body = rows[0], rows[1:]
    if how == "missing_column":
        i = header.index("Amt")
        rows = [r[:i] + r[i + 1:] for r in rows]
    elif how == "duplicate_key":
        rows = rows + [body[0]]
    elif how == "unbalanced_journal":
        body[0][header.index("Amt")] = str(float(body[0][header.index("Amt")]) + 5)
        rows = [header] + body
    path = tmp_path / "gl.csv"
    with open(path, "w", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerows(rows)
    if how == "truncated_rows":
        text = path.read_text()
        path.write_text(text[: len(text) * 2 // 3].rsplit(",", 1)[0] + "\n")   # cut mid-row
    return path


@pytest.mark.parametrize("how,check", [
    ("missing_column", "dataset_loaded"),
    ("duplicate_key", "duplicate_primary_key"),
    ("truncated_rows", "row_count"),
    ("unbalanced_journal", "balance"),
])
def test_gate_catches_corruption(tmp_path, how, check):
    data = _corrupt_gl(tmp_path, how)

    def edit(m):
        m["datasets"]["journal_lines"]["source"]["file"] = str(data)
    mp = portable_mapping(GL, tmp_path, edit)
    o = run_workbench(mp, CONFIG, tmp_path / "out")
    assert o.status == "gate_failed"
    fails = [c for c in o.gate.checks if c.status == "fail"]
    assert any(c.check == check for c in fails), [(c.check, c.detail[:80]) for c in fails]
    if how == "unbalanced_journal":
        assert any("possible incomplete extract" in c.detail for c in fails)
    if how == "truncated_rows":
        assert any(c.check == "expected_rows" for c in fails)


def test_corrupted_retail_sheet_missing_column(tmp_path):
    wb = openpyxl.load_workbook(REPO / "sample_data" / "retail.xlsx")
    wb["Receipts"].delete_cols(6)                      # GrossAmount
    bad = tmp_path / "retail.xlsx"
    wb.save(bad)

    def edit(m):
        for ds in m["datasets"].values():
            ds["source"]["file"] = str(bad)
    o = run_workbench(portable_mapping(RETAIL, tmp_path, edit), CONFIG, tmp_path / "out")
    assert o.status == "gate_failed"
    assert any(c.dataset == "sales" and c.check == "dataset_loaded" and "GrossAmount" in c.detail
               for c in o.gate.checks if c.status == "fail")


# 6. the workpaper opens, with working dropdowns and blank conclusion fields
def test_workpaper_review_fields_blank_and_dropdown(outcomes):
    for o in outcomes.values():
        wb = openpyxl.load_workbook(o.workpaper)
        for r in o.runs:
            ws = wb[r["run_id"]]
            header = [c.value for c in ws[1]]
            assert header[-4:] == REVIEW_COLUMNS
            idx = [header.index(c) for c in REVIEW_COLUMNS]
            assert all(row[i].value is None for row in ws.iter_rows(min_row=2) for i in idx)
            [dv] = ws.data_validations.dataValidation
            assert dv.type == "list" and dv.formula1 == '"' + ",".join(DISPOSITIONS) + '"'
            assert ws.max_row - 1 == len(r["exceptions"])
        cover = {row[0].value: row[1].value for row in wb["Cover"].iter_rows(max_col=2) if row[0].value}
        assert all(cover[f] is None for f in SIGNOFF_FIELDS)


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_workpaper_opens_in_spreadsheet_app(outcomes, tmp_path):
    """Open the workpaper in a real spreadsheet application (LibreOffice, headless) and convert it."""
    wp = outcomes["retail"].workpaper
    res = subprocess.run(["soffice", "--headless", "--norestore", f"-env:UserInstallation=file://{tmp_path}/lo",
                          "--convert-to", "pdf", "--outdir", str(tmp_path), str(wp)],
                         capture_output=True, text=True, timeout=180)
    assert (tmp_path / "workpaper.pdf").is_file(), res.stderr


# 7. a new test is just a folder
def test_temp_pack_folder_runs(tmp_path):
    packs = tmp_path / "packs"
    shutil.copytree(REPO / "packs", packs)
    new = packs / "general" / "large_records"
    new.mkdir()
    write_yaml(new / "meta.yaml", {
        "id": "TMP-01", "name": "Large records", "objective": "List records at or above a threshold.",
        "assertion": "existence", "risk": "Large items carry more risk.", "category": "analytical",
        "contract": {"records": {"required": {"record_id": "string", "amount": "decimal"}}},
        "parameters": {"threshold": {"type": "decimal", "default": "7000"}},
        "amount_column": "amount_at_risk", "initial_label": "inquiry", "needs_client_confirmation": False,
        "limitations": "Threshold chosen by the user.",
    })
    (new / "test.sql").write_text(
        "SELECT 'large:' || record_id AS exception_id, record_id, amount AS amount_at_risk "
        "FROM records WHERE amount >= $threshold")

    def edit(m):
        m["runs"] = [{"id": "TMP-01", "pack": "general/large_records",
                      "bind": {"records": {"dataset": "sales", "fields": {"record_id": "sale_id", "amount": "total_amount"}}},
                      "params": {"threshold": "9000"}}]
    mp = portable_mapping(RETAIL, tmp_path, edit)
    assert main(["validate-mapping", str(mp), "--packs", str(packs)]) == 0
    o = run_workbench(mp, CONFIG, tmp_path / "out", packs_root=packs)
    [r] = o.runs
    assert r["status"] == "ok" and r["exceptions"]
    assert all(e["amount_at_risk"] >= 9000 for e in r["exceptions"])
    assert "TMP-01" in openpyxl.load_workbook(o.workpaper).sheetnames


# 8. packs cannot read files outside the mapped sources
def test_sandbox_blocks_pack_file_read(tmp_path):
    packs = tmp_path / "packs"
    shutil.copytree(REPO / "packs", packs)
    evil = packs / "general" / "evil"
    shutil.copytree(packs / "itgc" / "segregation_of_duties", evil)
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n")
    (evil / "test.sql").write_text(f"SELECT a AS exception_id, NULL AS amount_at_risk FROM read_csv('{secret}')")

    def edit(m):
        run = next(r for r in m["runs"] if r["id"] == "ITGC-02")
        m["runs"] = [dict(run, id="EVIL", pack="general/evil")]
    o = run_workbench(portable_mapping(RETAIL, tmp_path, edit), CONFIG, tmp_path / "out", packs_root=packs)
    [r] = o.runs
    assert r["status"] == "failed" and "Permission" in r["reason"]


# 9. validate-mapping gives clear errors
def test_validate_mapping_errors_clear(tmp_path, capsys):
    def edit(m):
        m["datasets"]["sales"]["columns"]["total_amount"]["from"] = "Gross Amount"      # header typo
        m["datasets"]["sales"]["columns"]["quantity"]["type"] = "number"               # bad type
        m["runs"][2]["params"]["key_fields"] = ["customer_idd"]                         # unknown field
        del m["runs"][0]["bind"]["people"]                                              # unbound role
    mp = portable_mapping(RETAIL, tmp_path, edit)
    assert main(["validate-mapping", str(mp)]) == 1
    out = capsys.readouterr().out
    assert "4 problem(s)" in out
    assert "datasets.sales.columns.total_amount.from: column 'Gross Amount' not in the source header — did you mean 'GrossAmount'?" in out
    assert "datasets.sales.columns.quantity.type: type 'number' is not supported" in out
    assert "runs[2].params.key_fields: 'customer_idd' is not a field available to role 'records'" in out
    assert "runs[0].bind.people: role 'people' is not bound" in out


# 10. end to end in under a minute
def test_runtime_under_60s(outcomes, capsys):
    with capsys.disabled():
        print("\n" + " | ".join(f"{k}: {o.seconds:.2f}s" for k, o in outcomes.items()))
    assert all(o.seconds < 60 for o in outcomes.values())
