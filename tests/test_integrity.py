import copy

from conftest import write_csv, write_yaml
from workbench import sandbox
from workbench.integrity import run_gate
from workbench.loader import load_datasets


def gate(path, mapping):
    con = sandbox.connect()
    res = load_datasets(con, mapping, path)
    return run_gate(con, mapping, res)


def statuses(g, check):
    return [c.status for c in g.checks if c.check == check]


def test_clean_dataset_passes_with_placeholder_warning(small_project):
    root, path, mapping = small_project
    g = gate(path, mapping)
    assert not g.failed
    assert g.status == "warn"                     # the one N/A placeholder is shown as warn
    assert statuses(g, "placeholders") == ["warn"]
    assert statuses(g, "file_hash") == ["info"]


def test_missing_column_fails(small_project):
    root, path, mapping = small_project
    m = copy.deepcopy(mapping)
    m["datasets"]["orders"]["columns"]["value"]["from"] = "Amount"
    g = gate(path, m)
    assert g.failed and statuses(g, "dataset_loaded") == ["fail"]


def test_duplicate_key_fails(small_project):
    root, path, mapping = small_project
    p = root / "data" / "po.csv"
    p.write_text(p.read_text() + "P1,X,Y,1.00,2025-01-05\n")
    g = gate(path, mapping)
    c = [c for c in g.checks if c.check == "duplicate_primary_key"][0]
    assert c.status == "fail" and "P1" in c.detail


def test_truncated_csv_fails(small_project):
    root, path, mapping = small_project
    p = root / "data" / "po.csv"
    p.write_text(p.read_text()[:-12])             # cut mid-row
    g = gate(path, mapping)
    assert g.failed and "fail" in statuses(g, "row_count")


def test_expected_rows_mismatch_fails(small_project):
    root, path, mapping = small_project
    m = copy.deepcopy(mapping)
    m["datasets"]["orders"]["expected_rows"] = 4
    g = gate(path, m)
    assert statuses(g, "expected_rows") == ["fail"]


def test_unparseable_fails(small_project):
    root, path, mapping = small_project
    p = root / "data" / "po.csv"
    p.write_text(p.read_text() + "P9,A,B,1;00,2025-01-05\n")
    g = gate(path, mapping)
    assert statuses(g, "unparseable") == ["fail"]


def test_unbalanced_journal_fails(tmp_path):
    write_csv(tmp_path / "gl.csv", ["J", "L", "A"], [["J1", "1", "100.00"], ["J1", "2", "-100.00"],
                                                     ["J2", "1", "50.00"], ["J2", "2", "-49.00"]])
    m = {"mapping_version": 1, "datasets": {"gl": {"source": {"file": "gl.csv"}, "columns": {
        "je_id": {"from": "J", "type": "string"}, "line_no": {"from": "L", "type": "integer"},
        "amount": {"from": "A", "type": "decimal"}},
        "primary_key": ["je_id", "line_no"],
        "balance_check": {"group_by": ["je_id"], "amount": "amount", "tolerance": 0.01}}}}
    g = gate(write_yaml(tmp_path / "m.yaml", m), m)
    c = [c for c in g.checks if c.check == "balance"][0]
    assert c.status == "fail" and "possible incomplete extract" in c.detail and "J2" in c.detail


def test_orphans_warn_not_fail_and_both_directions(tmp_path):
    write_csv(tmp_path / "s.csv", ["Id", "Emp"], [["S1", "E1"], ["S2", "E9"]])
    write_csv(tmp_path / "e.csv", ["Emp"], [["E1"], ["E2"]])
    m = {"mapping_version": 1, "datasets": {
        "sales": {"source": {"file": "s.csv"}, "columns": {"sale_id": {"from": "Id"}, "emp": {"from": "Emp"}},
                  "references": [{"fields": ["emp"], "to": "staff", "to_fields": ["emp"]}]},
        "staff": {"source": {"file": "e.csv"}, "columns": {"emp": {"from": "Emp"}}}}}
    g = gate(write_yaml(tmp_path / "m.yaml", m), m)
    refs = [c for c in g.checks if c.check.startswith("reference")]
    orphan = [c for c in refs if c.check == "reference_orphans"][0]
    childless = [c for c in refs if c.check == "reference_unused_parents"][0]
    assert orphan.status == "warn" and "E9" in orphan.detail and orphan.value == "1"
    assert childless.status == "info" and childless.value == "1"
    assert not g.failed


def test_sequence_gaps_listed(tmp_path):
    write_csv(tmp_path / "s.csv", ["Id"], [["S001"], ["S002"], ["S005"]])
    m = {"mapping_version": 1, "datasets": {"sales": {"source": {"file": "s.csv"},
         "columns": {"sale_id": {"from": "Id"}}, "sequence": {"field": "sale_id", "pattern": r"^S(\d+)$"}}}}
    g = gate(write_yaml(tmp_path / "m.yaml", m), m)
    c = [c for c in g.checks if c.check == "sequence_gaps"][0]
    assert c.status == "warn" and c.value == "2" and "3, 4" in c.detail


def test_control_totals_info(small_project):
    root, path, mapping = small_project
    g = gate(path, mapping)
    totals = {c.detail: c.value for c in g.checks if c.check == "control_total"}
    assert totals["sum(value)"] == "360.50"
    assert totals["min(order_date)"] == "2025-01-02" and totals["max(order_date)"] == "2025-01-04"
    assert totals["distinct(order_id)"] == "3"
