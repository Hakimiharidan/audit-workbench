import openpyxl
import pytest

from conftest import REPO, write_csv
from workbench.pipeline import run_workbench
from workbench.workpaper import DISPOSITIONS, REVIEW_COLUMNS, SIGNOFF_FIELDS

CONFIG = REPO / "config.yaml"


@pytest.fixture
def workbook(small_project, sod_pack_root, tmp_path):
    root, path, _ = small_project
    write_csv(root / "data" / "po.csv", ["PONo", "Raiser", "Approver", "Value", "When"], [
        ["P1", "A", "B", "100.00", "2025-01-02"],
        ["=HYPERLINK(\"http://x\")", "=cmd", "=cmd", "60000.00", "2025-01-03"],
        ["P3", "C", "C", "10.00", "2025-01-04"],
    ])
    out = run_workbench(path, CONFIG, tmp_path / "out", packs_root=sod_pack_root)
    assert out.status == "ok"
    return openpyxl.load_workbook(out.workpaper)


def test_sheets_present(workbook):
    assert workbook.sheetnames == ["Cover", "Integrity", "Summary", "ITGC-02", "Limitations", "Run log"]


def test_dropdown_and_blank_review_fields(workbook):
    ws = workbook["ITGC-02"]
    header = [c.value for c in ws[1]]
    assert header[-4:] == REVIEW_COLUMNS
    disp = header.index("Disposition") + 1
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        assert all(row[header.index(c)].value is None for c in REVIEW_COLUMNS)
    [dv] = ws.data_validations.dataValidation
    assert dv.type == "list" and dv.formula1 == '"' + ",".join(DISPOSITIONS) + '"'
    col = openpyxl.utils.get_column_letter(disp)
    assert str(dv.sqref).startswith(f"{col}2:{col}")


def test_sorted_by_band_then_amount(workbook):
    ws = workbook["ITGC-02"]
    header = [c.value for c in ws[1]]
    bands = [r[header.index("band")].value for r in ws.iter_rows(min_row=2)]
    assert bands == ["above_materiality", "trivial"]


def test_formula_like_text_is_literal(workbook):
    ws = workbook["ITGC-02"]
    cells = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert cells and all(c.data_type == "s" for c in cells)


def test_cover_preparer_reviewer_blank(workbook):
    ws = workbook["Cover"]
    rows = {r[0].value: r[1].value for r in ws.iter_rows(max_col=2) if r[0].value}
    for f in SIGNOFF_FIELDS:
        assert f in rows and rows[f] is None
    assert len(rows["Run ID"]) == 12 and rows["Overall materiality"] == 50000


def test_summary_and_limitations(workbook):
    s = workbook["Summary"]
    header = [c.value for c in s[1]]
    row = {h: c.value for h, c in zip(header, s[2])}
    assert row["Run ID"] == "ITGC-02" and row["Exceptions"] == 2 and row["Status"] == "ok"
    assert row["Highest band"] == "above_materiality"
    assert workbook["Limitations"]["C2"].value
