import copy
from decimal import Decimal

from conftest import write_csv, write_yaml
from workbench import sandbox
from workbench.loader import load_datasets
from workbench.packs import discover_packs
from workbench.runner import run_packs

META = {
    "id": "T-01", "name": "t", "objective": "o", "assertion": "occurrence", "risk": "r",
    "category": "analytical", "initial_label": "inquiry", "needs_client_confirmation": False,
    "limitations": "l", "amount_column": "amount_at_risk",
}


def make_pack(root, key, contract, sql, parameters=None, **extra):
    d = root.joinpath(*key.split("/"))
    d.mkdir(parents=True)
    write_yaml(d / "meta.yaml", dict(META, contract=contract, parameters=parameters or {}, **extra))
    (d / "test.sql").write_text(sql)


def setup(tmp_path, runs, packs_fn, extra_datasets=None):
    write_csv(tmp_path / "s.csv", ["Id", "Cust", "Amt", "D"], [
        ["S1", "C1", "10.00", "2025-01-01"], ["S2", "C1", "10.00", "2025-01-01"], ["S3", "C2", "5.00", "2025-01-02"]])
    mapping = {"mapping_version": 1, "datasets": {"sales": {"source": {"file": "s.csv"}, "columns": {
        "sale_id": {"from": "Id"}, "customer": {"from": "Cust"}, "total": {"from": "Amt", "type": "decimal"},
        "sale_date": {"from": "D", "type": "date"}}}} | (extra_datasets or {}), "runs": runs}
    mpath = write_yaml(tmp_path / "m.yaml", mapping)
    root = tmp_path / "packs"
    packs_fn(root)
    con = sandbox.connect()
    load = load_datasets(con, mapping, mpath)
    sandbox.lock(con)
    return run_packs(con, mapping, discover_packs(root), load, mpath)


BIND = {"records": {"dataset": "sales", "fields": {"record_id": "sale_id", "amount": "total"}}}
CONTRACT = {"records": {"required": {"record_id": "string"}, "optional": {"amount": "decimal", "date": "date"}}}


def test_basic_run_with_optional_unbound_and_value_param(tmp_path):
    def packs(root):
        make_pack(root, "g/big", CONTRACT,
                  "SELECT 'big:' || record_id AS exception_id, record_id, date, amount AS amount_at_risk "
                  "FROM records WHERE amount >= $min_amount",
                  {"min_amount": {"type": "decimal", "default": "1"}})
    [r] = setup(tmp_path, [{"id": "T-01", "pack": "g/big", "bind": BIND, "params": {"min_amount": 6}}], packs)
    assert r.status == "ok", r.reason
    assert [e["exception_id"] for e in r.exceptions] == ["big:S1", "big:S2"]
    assert r.exceptions[0]["amount_at_risk"] == Decimal("10.00") and r.exceptions[0]["date"] is None
    assert r.params == {"min_amount": "6"}


def test_field_list_substitution_and_list_param(tmp_path):
    def packs(root):
        make_pack(root, "g/dup", CONTRACT,
                  "SELECT 'k:' || record_id AS exception_id, NULL::DECIMAL(18,2) AS amount_at_risk, "
                  "count(*) OVER (PARTITION BY row({{keys}})) AS n FROM records "
                  "WHERE COLUMNS(c -> list_contains($keys, c)) IS NOT NULL QUALIFY n > 1",
                  {"keys": {"type": "field_list", "role": "records", "default": []}})
    [r] = setup(tmp_path, [{"id": "T-01", "pack": "g/dup", "bind": BIND,
                            "params": {"keys": ["customer", "amount"]}}], packs)
    assert r.status == "ok", r.reason
    assert [e["exception_id"] for e in r.exceptions] == ["k:S1", "k:S2"]


def test_field_list_injection_rejected(tmp_path):
    def packs(root):
        make_pack(root, "g/dup", CONTRACT, "SELECT 'x' AS exception_id, NULL AS amount_at_risk, {{keys}} FROM records",
                  {"keys": {"type": "field_list", "role": "records", "default": []}})
    [r] = setup(tmp_path, [{"id": "T-01", "pack": "g/dup", "bind": BIND,
                            "params": {"keys": ['customer" FROM records; DROP TABLE ds_sales; --']}}], packs)
    assert r.status == "failed" and "not a field" in r.reason


def test_missing_dataset_skips(tmp_path):
    def packs(root):
        make_pack(root, "g/two", {"records": CONTRACT["records"], "people": {"required": {"person_id": "string"}}},
                  "SELECT 'x' AS exception_id, NULL AS amount_at_risk")
    runs = [{"id": "T-01", "pack": "g/two", "bind": BIND | {"people": {"dataset": "staff", "fields": {"person_id": "pid"}}}}]
    extra = {"staff": {"source": {"file": "missing.csv"}, "columns": {"pid": {"from": "P"}}}}
    [r] = setup(tmp_path, runs, packs, extra)
    assert r.status == "skipped" and "staff" in r.reason


def test_optional_role_unbound_is_empty(tmp_path):
    def packs(root):
        make_pack(root, "g/opt", {"records": CONTRACT["records"], "children": {"required": {"parent_id": "string"}}},
                  "SELECT 'n:' || count(*) AS exception_id, NULL AS amount_at_risk FROM children",
                  optional_roles=["children"])
    [r] = setup(tmp_path, [{"id": "T-01", "pack": "g/opt", "bind": BIND}], packs)
    assert r.status == "ok" and r.exceptions[0]["exception_id"] == "n:0"


def test_pack_cannot_read_files(tmp_path):
    def packs(root):
        make_pack(root, "g/evil", CONTRACT,
                  f"SELECT column0 AS exception_id, NULL AS amount_at_risk FROM read_csv('{tmp_path / 's.csv'}')")
    [r] = setup(tmp_path, [{"id": "T-01", "pack": "g/evil", "bind": BIND}], packs)
    assert r.status == "failed" and "Permission" in r.reason


def test_non_select_pack_fails(tmp_path):
    def packs(root):
        make_pack(root, "g/ddl", CONTRACT, "DROP TABLE ds_sales")
    [r] = setup(tmp_path, [{"id": "T-01", "pack": "g/ddl", "bind": BIND}], packs)
    assert r.status == "failed" and "SELECT" in r.reason


def test_duplicate_exception_id_fails_run(tmp_path):
    def packs(root):
        make_pack(root, "g/dupid", CONTRACT, "SELECT 'same' AS exception_id, NULL AS amount_at_risk FROM records")
    [r] = setup(tmp_path, [{"id": "T-01", "pack": "g/dupid", "bind": BIND}], packs)
    assert r.status == "failed" and "unique" in r.reason


def test_missing_required_columns_fails(tmp_path):
    def packs(root):
        make_pack(root, "g/nocol", CONTRACT, "SELECT record_id FROM records")
    [r] = setup(tmp_path, [{"id": "T-01", "pack": "g/nocol", "bind": BIND}], packs)
    assert r.status == "failed" and "exception_id" in r.reason


def test_date_list_file_registered_as_table(tmp_path):
    (tmp_path / "hol.yaml").write_text("label: x\ndates:\n  - {date: 2025-01-01, name: New Year}\n")
    def packs(root):
        make_pack(root, "g/hol", {"records": {"required": {"record_id": "string", "date": "date"}}},
                  "SELECT 'h:' || record_id AS exception_id, NULL AS amount_at_risk, h.holiday_name FROM records r "
                  "JOIN holidays h ON h.holiday_date = r.date",
                  {"holiday_file": {"type": "date_list_file", "table": "holidays", "required": True}})
    bind = {"records": {"dataset": "sales", "fields": {"record_id": "sale_id", "date": "sale_date"}}}
    [r] = setup(tmp_path, [{"id": "T-01", "pack": "g/hol", "bind": bind, "params": {"holiday_file": "hol.yaml"}}], packs)
    assert r.status == "ok", r.reason
    assert [(e["exception_id"], e["holiday_name"]) for e in r.exceptions] == [("h:S1", "New Year"), ("h:S2", "New Year")]
