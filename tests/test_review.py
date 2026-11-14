"""Review screen core: reads a workpaper, records human dispositions into a reviewed copy."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from openpyxl import load_workbook

from workbench import review
from workbench.pipeline import run_workbench
from workbench.workpaper import DISPOSITIONS, REVIEW_COLUMNS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def run_out(tmp_path_factory):
    out = tmp_path_factory.mktemp("retail")
    run_workbench(ROOT / "mappings/example_retail.yaml", ROOT / "config.yaml", out, engagement="Review test")
    return out


@pytest.fixture
def out(run_out, tmp_path):
    d = tmp_path / "out"
    shutil.copytree(run_out, d)
    return d


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _cells(path: Path) -> dict:
    wb = load_workbook(path)
    return {(ws.title, c.coordinate): c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row}


def test_load_finds_run_sheets_and_exceptions(out):
    wp = review.load_workpaper(out)
    assert wp.path.name == "workpaper.xlsx"
    assert set(wp.runs) == {"ITGC-01", "ITGC-02", "REV-01", "REV-02", "PAY-01"}
    itgc = wp.runs["ITGC-01"]
    assert len(itgc) == 5
    assert {"exception_id", "band", "label", *REVIEW_COLUMNS} <= set(itgc.columns)
    assert itgc["Disposition"].isna().all()
    assert not wp.summary.empty and not wp.integrity.empty


def test_record_writes_only_review_columns_into_a_copy(out):
    original = out / "workpaper.xlsx"
    before_sha, before = _sha(original), _cells(original)
    eid = review.load_workpaper(out).runs["ITGC-02"]["exception_id"].iloc[0]

    dst = review.record_dispositions(out, "ITGC-02", [eid], disposition="False positive",
                                     note="Approver delegated in writing", reviewer="A. Reviewer",
                                     date="2026-11-14")

    assert dst == out / "workpaper_reviewed.xlsx"
    assert _sha(original) == before_sha, "original workpaper must never change"
    after = _cells(dst)
    ws = load_workbook(dst)["ITGC-02"]
    header = [c.value for c in ws[1]]
    review_cols = {header.index(c) + 1 for c in REVIEW_COLUMNS}
    from openpyxl.utils import get_column_letter
    review_letters = {get_column_letter(i) for i in review_cols}
    for (sheet, coord), value in before.items():
        if sheet == "Run log":
            continue
        letter = "".join(ch for ch in coord if ch.isalpha())
        if sheet == "ITGC-02" and letter in review_letters:
            continue
        assert after.get((sheet, coord)) == value, f"{sheet}!{coord} changed"
    row = [c.value for c in ws[2]]
    assert row[header.index("Disposition")] == "False positive"
    assert row[header.index("Reviewer note")] == "Approver delegated in writing"
    assert row[header.index("Reviewed by")] == "A. Reviewer"
    assert str(row[header.index("Date")]).startswith("2026-11-14")


def test_reviewed_copy_keeps_dropdown_and_logs_change(out):
    eid = review.load_workpaper(out).runs["ITGC-01"]["exception_id"].iloc[0]
    review.record_dispositions(out, "ITGC-01", [eid], disposition="Error", note="", reviewer="A. Reviewer",
                               date="2026-11-14")
    review.record_dispositions(out, "ITGC-01", [eid], disposition="Needs client query", note="",
                               reviewer="B. Senior", date="2026-11-15")
    wb = load_workbook(out / "workpaper_reviewed.xlsx")
    formulas = [d.formula1 for d in wb["ITGC-01"].data_validations.dataValidation]
    assert any("False positive" in f for f in formulas)
    log = [r for r in wb["Run log"].iter_rows(min_row=2, values_only=True) if r[0] == "review"]
    assert len(log) == 2
    assert eid in log[1][1] and "Needs client query" in log[1][1] and "B. Senior" in log[1][1]
    assert "Error" in str(log[1][2])  # previous value kept in the history


def test_second_load_reads_the_reviewed_copy(out):
    eid = review.load_workpaper(out).runs["REV-01"]["exception_id"].iloc[0]
    review.record_dispositions(out, "REV-01", [eid], disposition="Accepted", note="", reviewer="R",
                               date="2026-11-14")
    wp = review.load_workpaper(out)
    assert wp.path.name == "workpaper_reviewed.xlsx"
    df = wp.runs["REV-01"]
    assert df.loc[df["exception_id"] == eid, "Disposition"].item() == "Accepted"
    assert df["Disposition"].isna().sum() == len(df) - 1


def test_bulk_selection_only_changes_selected_rows(out):
    df = review.load_workpaper(out).runs["PAY-01"]
    chosen = list(df["exception_id"].iloc[:3])
    review.record_dispositions(out, "PAY-01", chosen, disposition="Needs client query", note="rule unknown",
                               reviewer="R", date="2026-11-14")
    after = review.load_workpaper(out).runs["PAY-01"]
    assert after["Disposition"].notna().sum() == 3
    assert set(after.loc[after["Disposition"].notna(), "exception_id"]) == set(chosen)


@pytest.mark.parametrize("kwargs, message", [
    ({"disposition": "Probably fine"}, "Disposition must be one of"),
    ({"disposition": ""}, "Disposition must be one of"),
    ({"reviewer": "  "}, "Reviewer name is required"),
    ({"ids": []}, "Select at least one exception"),
    ({"ids": ["no-such-id"]}, "not found"),
    ({"run_id": "Summary"}, "not a test sheet"),
])
def test_invalid_input_is_rejected_and_nothing_is_written(out, kwargs, message):
    eid = review.load_workpaper(out).runs["ITGC-01"]["exception_id"].iloc[0]
    args = {"run_id": "ITGC-01", "ids": [eid], "disposition": "Error", "note": "", "reviewer": "R",
            "date": "2026-11-14"} | kwargs
    with pytest.raises(review.ReviewError, match=message):
        review.record_dispositions(out, args.pop("run_id"), args.pop("ids"), **args)
    assert not (out / "workpaper_reviewed.xlsx").exists()


def test_allowed_dispositions_are_the_workpaper_list():
    assert review.ALLOWED == tuple(DISPOSITIONS)


def test_missing_workpaper_has_clear_error(tmp_path):
    with pytest.raises(review.ReviewError, match="No workpaper.xlsx"):
        review.load_workpaper(tmp_path)
