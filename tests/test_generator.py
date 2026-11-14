import csv
import json
from decimal import Decimal

import openpyxl

from conftest import REPO
from tools.generate_synthetic import generate

# The synthetic headers are fixed on purpose: they are invented for this project
# and must not drift towards the names used in any real extract.
EXPECTED_HEADERS = {
    "Clients": ["ClientCode", "ClientName", "Mobile", "BirthDate"],
    "Staff": ["StaffCode", "StaffName", "Role", "Sex", "BirthDate", "Mobile", "Joined", "Left",
              "EmploymentStatus", "CommRate", "NationalIdFake"],
    "Items": ["ItemCode", "ItemName", "UnitPrice", "Spec"],
    "Receipts": ["ReceiptNo", "ReceiptDate", "ClientCode", "Tender", "Units", "GrossAmount", "CashierCode"],
    "ReceiptItems": ["ReceiptNo", "ItemCode"],
    "Payouts": ["ReceiptNo", "StaffCode", "PayoutAmount"],
    "POs": ["PONumber", "PODate", "TotalUnits", "RaisedBy", "SignedOffBy"],
    "POItems": ["PONumber", "ItemCode", "Units"],
    "gl.csv": ["JrnlRef", "Ln", "PostDate", "Acct", "Amt", "Narrative", "Poster", "Src"],
}


def test_deterministic(tmp_path):
    a = generate(42, tmp_path / "a")
    b = generate(42, tmp_path / "b")
    assert a == b
    assert (tmp_path / "a" / "gl.csv").read_bytes() == (tmp_path / "b" / "gl.csv").read_bytes()
    assert json.loads((tmp_path / "a" / "planted_issues.json").read_text()) == a


def test_other_seed_differs(tmp_path):
    assert generate(42, tmp_path / "a") != generate(7, tmp_path / "b")


def test_headers_are_the_fixed_synthetic_set(tmp_path):
    generate(42, tmp_path)
    wb = openpyxl.load_workbook(tmp_path / "retail.xlsx", read_only=True)
    found = {ws.title: list(next(ws.iter_rows(values_only=True))) for ws in wb.worksheets}
    with open(tmp_path / "gl.csv") as fh:
        found["gl.csv"] = next(csv.reader(fh))
    assert found == EXPECTED_HEADERS


def test_placeholder_share_about_80pct(tmp_path):
    generate(42, tmp_path)
    ws = openpyxl.load_workbook(tmp_path / "retail.xlsx", read_only=True)["Receipts"]
    rows = list(ws.iter_rows(values_only=True))
    col = rows[0].index("ClientCode")
    share = sum(1 for r in rows[1:] if r[col] == "N/A") / (len(rows) - 1)
    assert 0.75 <= share <= 0.85


def test_gl_balanced(tmp_path):
    generate(42, tmp_path)
    totals = {}
    with open(tmp_path / "gl.csv") as fh:
        for r in csv.DictReader(fh):
            totals[r["JrnlRef"]] = totals.get(r["JrnlRef"], Decimal(0)) + Decimal(r["Amt"])
    assert all(v == 0 for v in totals.values())


def test_planted_minimums(tmp_path):
    p = generate(42, tmp_path)["runs"]
    assert len([x for x in p["REV-02"] if x.startswith("orphan:")]) == 6
    assert len([x for x in p["REV-02"] if x.startswith("gap:")]) == 6
    assert len([x for x in p["ITGC-01"] if x.startswith("after_leave:")]) == 4
    assert len([x for x in p["ITGC-01"] if x.startswith("leaver_still_active:")]) == 1
    assert len(p["ITGC-02"]) == 1
    assert len(p["REV-01"]) == 8
    assert len([x for x in p["PAY-01"] if x.startswith("attribution:")]) >= 70
    assert len([x for x in p["PAY-01"] if x.startswith("recalc:")]) > 700
    assert len(p["JE-01"]) > 0


def test_shipped_sample_matches_seed_42(tmp_path):
    shipped = json.loads((REPO / "sample_data" / "planted_issues.json").read_text())
    assert shipped == generate(42, tmp_path)
