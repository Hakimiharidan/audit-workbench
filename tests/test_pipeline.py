import copy
import json

import openpyxl

from conftest import REPO, write_yaml
from workbench.cli import main
from workbench.pipeline import run_workbench

CONFIG = REPO / "config.yaml"


def test_outputs_written(small_project, sod_pack_root, tmp_path):
    root, path, _ = small_project
    out = run_workbench(path, CONFIG, tmp_path / "out", engagement="Test", packs_root=sod_pack_root)
    assert out.status == "ok"
    assert out.workpaper.is_file() and out.exceptions_file.is_file() and out.runlog_file.is_file()
    lines = out.exceptions_file.read_text().splitlines()
    assert [json.loads(l)["exception_id"] for l in lines] == ["sod:P2"]
    log = json.loads(out.runlog_file.read_text())
    kinds = [e["kind"] for e in log["events"]]
    assert "placeholder" in kinds and "run_ok" in kinds and log["run"]["force_used"] is False


def test_gate_fail_stops_without_force(small_project, sod_pack_root, tmp_path):
    root, path, _ = small_project
    p = root / "data" / "po.csv"
    p.write_text(p.read_text() + "P1,X,Y,1.00,2025-01-05\n")              # duplicate key
    out = run_workbench(path, CONFIG, tmp_path / "out", packs_root=sod_pack_root)
    assert out.status == "gate_failed" and out.exceptions_file is None and out.runs == []
    assert any("duplicate_primary_key" in m for m in out.messages)
    wb = openpyxl.load_workbook(out.workpaper)
    assert "ITGC-02" not in wb.sheetnames and wb["Integrity"]["A1"].value.endswith("FAIL")


def test_force_recorded(small_project, sod_pack_root, tmp_path):
    root, path, _ = small_project
    p = root / "data" / "po.csv"
    p.write_text(p.read_text() + "P1,X,Y,1.00,2025-01-05\n")
    out = run_workbench(path, CONFIG, tmp_path / "out", force=True, packs_root=sod_pack_root)
    assert out.status == "ok"
    log = json.loads(out.runlog_file.read_text())
    assert log["run"]["force_used"] is True and any(e["kind"] == "force" for e in log["events"])
    wb = openpyxl.load_workbook(out.workpaper)
    cover = {r[0].value: r[1].value for r in wb["Cover"].iter_rows(max_col=2)}
    assert cover["--force used"].startswith("YES")
    assert "--force" in wb["Integrity"]["A2"].value


def test_missing_column_is_gate_failure_not_mapping_error(small_project, sod_pack_root, tmp_path):
    root, path, mapping = small_project
    m = copy.deepcopy(mapping)
    m["datasets"]["orders"]["columns"]["value"]["from"] = "Amount"
    mp = write_yaml(root / "m2.yaml", m)
    out = run_workbench(mp, CONFIG, tmp_path / "out", packs_root=sod_pack_root)
    assert out.status == "gate_failed"
    assert any("dataset_loaded" in msg for msg in out.messages)


def test_structural_mapping_error_runs_nothing(small_project, sod_pack_root, tmp_path):
    root, path, mapping = small_project
    m = copy.deepcopy(mapping)
    m["runs"][0]["pack"] = "nope/nope"
    out = run_workbench(write_yaml(root / "m3.yaml", m), CONFIG, tmp_path / "out", packs_root=sod_pack_root)
    assert out.status == "invalid_mapping" and not (tmp_path / "out").exists()


def test_cli_run_exit_codes(small_project, sod_pack_root, tmp_path, capsys):
    root, path, _ = small_project
    assert main(["run", str(path), "--config", str(CONFIG), "--out", str(tmp_path / "o"),
                 "--packs", str(sod_pack_root)]) == 0
    assert "Workpaper:" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text("materiality: {overall: 0}\n")
    assert main(["run", str(path), "--config", str(bad), "--out", str(tmp_path / "o2"),
                 "--packs", str(sod_pack_root)]) == 1
