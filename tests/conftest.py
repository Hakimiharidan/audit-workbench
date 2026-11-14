import csv
from pathlib import Path

import openpyxl
import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent


def write_csv(path: Path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def write_xlsx(path: Path, sheets: dict):
    """sheets: {name: (header, rows)}"""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, (header, rows) in sheets.items():
        ws = wb.create_sheet(name)
        ws.append(list(header))
        for r in rows:
            ws.append(list(r))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def write_yaml(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def small_project(tmp_path):
    """A tiny CSV source + mapping with one dataset and one SoD run."""
    write_csv(tmp_path / "data" / "po.csv", ["PONo", "Raiser", "Approver", "Value", "When"], [
        ["P1", "A", "B", "100.00", "2025-01-02"],
        ["P2", "C", "C", "250.50", "2025-01-03"],
        ["P3", "N/A", "B", "10.00", "2025-01-04"],
    ])
    mapping = {
        "mapping_version": 1,
        "null_placeholders": ["N/A", ""],
        "datasets": {
            "orders": {
                "source": {"file": "data/po.csv"},
                "columns": {
                    "order_id": {"from": "PONo", "type": "string"},
                    "raised_by": {"from": "Raiser", "type": "string"},
                    "approved_by": {"from": "Approver", "type": "string"},
                    "value": {"from": "Value", "type": "decimal"},
                    "order_date": {"from": "When", "type": "date"},
                },
                "primary_key": ["order_id"],
            }
        },
        "runs": [
            {
                "id": "ITGC-02",
                "pack": "itgc/segregation_of_duties",
                "bind": {"records": {"dataset": "orders", "fields": {
                    "record_id": "order_id", "initiator": "raised_by", "approver": "approved_by",
                    "amount": "value"}}},
                "params": {},
            }
        ],
    }
    path = write_yaml(tmp_path / "mapping.yaml", mapping)
    return tmp_path, path, mapping


@pytest.fixture
def sod_pack_root(tmp_path_factory):
    """A packs root containing only a minimal SoD-shaped pack (independent of shipped packs)."""
    root = tmp_path_factory.mktemp("packs")
    d = root / "itgc" / "segregation_of_duties"
    d.mkdir(parents=True)
    write_yaml(d / "meta.yaml", {
        "id": "ITGC-02", "name": "SoD", "objective": "o", "assertion": "classification",
        "risk": "r", "category": "itgc",
        "contract": {"records": {
            "required": {"record_id": "string", "initiator": "string", "approver": "string"},
            "optional": {"amount": "decimal", "date": "date"}}},
        "parameters": {}, "amount_column": "amount_at_risk",
        "initial_label": "control_deficiency", "needs_client_confirmation": False,
        "limitations": "l",
    })
    (d / "test.sql").write_text(
        "SELECT 'sod:' || record_id AS exception_id, record_id, initiator, approver, "
        "amount AS amount_at_risk FROM records WHERE initiator = approver")
    return root
