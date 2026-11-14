import copy
import datetime as dt
from decimal import Decimal

import duckdb
import pytest

from conftest import write_csv, write_xlsx, write_yaml
from workbench import sandbox
from workbench.loader import load_datasets


def load(mapping_path, mapping):
    con = sandbox.connect()
    return con, load_datasets(con, mapping, mapping_path)


def test_placeholders_counted_not_silent(small_project):
    root, path, mapping = small_project
    con, res = load(path, mapping)
    d = res.datasets["orders"]
    assert d.status == "loaded"
    assert d.placeholders["raised_by"] == 1          # the "N/A"
    assert con.execute("SELECT count(*) FROM ds_orders WHERE raised_by IS NULL").fetchone()[0] == 1
    assert d.source_rows == d.loaded_rows == 3


def test_extra_columns_ignored_and_logged(tmp_path):
    write_csv(tmp_path / "p.csv", ["Id", "Secret"], [["1", "x"]])
    m = {"mapping_version": 1, "datasets": {"people": {
        "source": {"file": "p.csv"}, "columns": {"pid": {"from": "Id", "type": "string"}}}}}
    con, res = load(write_yaml(tmp_path / "m.yaml", m), m)
    assert res.datasets["people"].ignored_columns == ["Secret"]
    cols = [r[0] for r in con.execute("DESCRIBE ds_people").fetchall()]
    assert "Secret" not in cols and "secret" not in cols


def test_missing_column_rejects_dataset(small_project):
    root, path, mapping = small_project
    m = copy.deepcopy(mapping)
    m["datasets"]["orders"]["columns"]["value"]["from"] = "Amount"
    con, res = load(path, m)
    d = res.datasets["orders"]
    assert d.status == "rejected" and "Amount" in d.reason
    with pytest.raises(duckdb.CatalogException):
        con.execute("SELECT * FROM ds_orders")


def test_csv_short_row_rejected(tmp_path):
    p = write_csv(tmp_path / "x.csv", ["A", "B"], [["1", "2"], ["3", "4"]])
    p.write_text(p.read_text() + "5\n")                 # truncated last row
    m = {"mapping_version": 1, "datasets": {"x": {"source": {"file": "x.csv"}, "columns": {
        "a": {"from": "A", "type": "integer"}, "b": {"from": "B", "type": "integer"}}}}}
    con, res = load(write_yaml(tmp_path / "m.yaml", m), m)
    d = res.datasets["x"]
    assert d.source_rows == 3 and d.loaded_rows == 2
    assert d.rejected_rows[0][0] == 4 and "truncated" in d.rejected_rows[0][1]


def test_thousands_separator_is_unparseable(tmp_path):
    write_csv(tmp_path / "x.csv", ["Amt"], [["1,234.50"], ["£12.00"], ["12.5"]])
    m = {"mapping_version": 1, "datasets": {"x": {"source": {"file": "x.csv"}, "columns": {
        "amt": {"from": "Amt", "type": "decimal"}}}}}
    con, res = load(write_yaml(tmp_path / "m.yaml", m), m)
    assert res.datasets["x"].unparseable["amt"] == [2, 3]
    assert con.execute("SELECT amt FROM ds_x WHERE _row = 4").fetchone()[0] == Decimal("12.50")


def test_date_format_declared_and_undeclared(tmp_path):
    write_csv(tmp_path / "x.csv", ["D"], [["05/01/2025"]])
    m = {"mapping_version": 1, "datasets": {"x": {"source": {"file": "x.csv"}, "columns": {
        "d": {"from": "D", "type": "date"}}}}}
    mp = write_yaml(tmp_path / "m.yaml", m)
    con, res = load(mp, m)
    assert res.datasets["x"].unparseable["d"] == [2]
    m["datasets"]["x"]["columns"]["d"]["format"] = "%d/%m/%Y"
    con, res = load(mp, m)
    assert res.datasets["x"].unparseable == {}
    assert con.execute("SELECT d FROM ds_x").fetchone()[0] == dt.date(2025, 1, 5)


def test_decimal_type_is_exact(small_project):
    root, path, mapping = small_project
    con, res = load(path, mapping)
    assert con.execute("SELECT any_value(typeof(value)), sum(value) FROM ds_orders").fetchone() == ("DECIMAL(18,2)", Decimal("360.50"))


def test_float_noise_rounded_and_logged(tmp_path):
    write_csv(tmp_path / "x.csv", ["Amt"], [["12.3400000001"], ["2.00"], ["0.30000000000000004"]])
    m = {"mapping_version": 1, "datasets": {"x": {"source": {"file": "x.csv"}, "columns": {
        "amt": {"from": "Amt", "type": "decimal"}}}}}
    con, res = load(write_yaml(tmp_path / "m.yaml", m), m)
    d = res.datasets["x"]
    assert d.unparseable == {}
    assert any("amt: 2 value(s) rounded" in c and "float noise" in c for c in d.coercions)
    assert [r[0] for r in con.execute("SELECT amt FROM ds_x ORDER BY _row").fetchall()] == [
        Decimal("12.34"), Decimal("2.00"), Decimal("0.30")]


def test_genuine_third_decimal_is_unparseable(tmp_path):
    write_csv(tmp_path / "x.csv", ["Amt"], [["12.345"], ["2.00"], ["1.0001"]])
    m = {"mapping_version": 1, "datasets": {"x": {"source": {"file": "x.csv"}, "columns": {
        "amt": {"from": "Amt", "type": "decimal"}}}}}
    con, res = load(write_yaml(tmp_path / "m.yaml", m), m)
    d = res.datasets["x"]
    assert d.unparseable == {"amt": [2, 4]}
    assert not any("rounded" in c for c in d.coercions)
    assert con.execute("SELECT amt FROM ds_x WHERE _row = 2").fetchone()[0] is None


def test_excel_values_converted_and_counted(tmp_path):
    write_xlsx(tmp_path / "w.xlsx", {"S": (["Id", "When", "Amt", "Note"], [
        ["A1", dt.datetime(2025, 3, 1), 10.5, "N/A"],
        ["A2", "N/A", 7, None],
        [None, None, None, None],
    ])})
    m = {"mapping_version": 1, "datasets": {"s": {"source": {"file": "w.xlsx", "sheet": "S"}, "columns": {
        "id": {"from": "Id", "type": "string"}, "when": {"from": "When", "type": "date"},
        "amt": {"from": "Amt", "type": "decimal"}, "note": {"from": "Note", "type": "string"}}}}}
    con, res = load(write_yaml(tmp_path / "m.yaml", m), m)
    d = res.datasets["s"]
    assert d.blank_rows_skipped == 1 and d.loaded_rows == 2
    assert d.placeholders == {"when": 1, "note": 1}
    assert d.nulls["note"] == 2                      # one placeholder + one empty cell
    assert any("excel number -> text" in c for c in d.coercions)
    assert con.execute("SELECT \"when\" FROM ds_s WHERE id='A1'").fetchone()[0] == dt.date(2025, 3, 1)


def test_file_info_hashes(small_project):
    root, path, mapping = small_project
    con, res = load(path, mapping)
    assert len(res.files) == 1 and len(res.files[0].sha256) == 64


def test_sandbox_blocks_file_read(small_project):
    root, path, mapping = small_project
    con, res = load(path, mapping)
    sandbox.lock(con)
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"SELECT * FROM read_csv('{root / 'data' / 'po.csv'}')")
    with pytest.raises(duckdb.Error):
        con.execute("SET enable_external_access = true")
