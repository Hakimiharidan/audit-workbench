"""Shipped packs: metadata valid, and behaviour on the sample data and small fixtures."""
import copy
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import REPO, write_csv, write_yaml
from workbench import sandbox
from workbench.integrity import run_gate
from workbench.loader import load_datasets
from workbench.mapping import load_mapping
from workbench.packs import DEFAULT_PACKS_ROOT, check_pack_sql, discover_packs, validate_meta
from workbench.runner import run_packs

PACKS = discover_packs(DEFAULT_PACKS_ROOT)


def run_mapping(path: Path, mapping=None):
    mapping = mapping or load_mapping(path)
    con = sandbox.connect()
    load = load_datasets(con, mapping, path)
    run_gate(con, mapping, load)
    sandbox.lock(con)
    return {r.run_id: r for r in run_packs(con, mapping, PACKS, load, path)}


@pytest.fixture(scope="module")
def retail():
    return run_mapping(REPO / "mappings" / "example_retail.yaml")


@pytest.fixture(scope="module")
def gl():
    return run_mapping(REPO / "mappings" / "example_gl.yaml")


def ids(run):
    return {e["exception_id"] for e in run.exceptions}


def test_six_packs_shipped_and_valid():
    assert sorted(p.id for p in PACKS.values()) == ["ITGC-01", "ITGC-02", "JE-01", "PAY-01", "REV-01", "REV-02"]
    for p in PACKS.values():
        assert validate_meta(p.meta) == [], p.key
        check_pack_sql(p)


def test_all_runs_ok(retail, gl):
    assert {k: r.status for k, r in (retail | gl).items()} == {k: "ok" for k in retail | gl}


def test_leaver_activity(retail):
    r = retail["ITGC-01"]
    assert "leaver_still_active:ST007" in ids(r)
    assert not any("ST009" in i for i in ids(r))          # correct leaver, no later activity
    after = [e for e in r.exceptions if e["rule"] == "after_leave"]
    assert len(after) == 4 and all(e["days_after_leave"] > 0 and e["amount_at_risk"] > 0 for e in after)
    assert r.pack.label_for("after_leave") == "control_deficiency"


def test_duplicates_amount_only_on_copies(retail):
    r = retail["REV-01"]
    groups = {}
    for e in r.exceptions:
        groups.setdefault(e["group_id"], []).append(e)
    assert len(groups) == 4 and all(len(g) == 2 for g in groups.values())
    for g in groups.values():
        first, second = sorted(g, key=lambda e: e["position"])
        assert first["amount_at_risk"] is None and second["amount_at_risk"] == second["amount"]


def test_gaps_coincide_with_orphans(retail):
    r = retail["REV-02"]
    assert all(e["gap_matches_orphan"] for e in r.exceptions)
    assert all(e["amount_at_risk"] is not None for e in r.exceptions if e["rule"] == "orphan")


def test_commission_labels_and_systemic_note(retail):
    r = retail["PAY-01"]
    assert r.pack.label_for("attribution") == "likely_error" and r.pack.label_for("recalc") == "inquiry"
    assert r.pack.confirmation_for("recalc") is True and r.pack.confirmation_for("attribution") is False
    assert r.systemic and r.systemic.startswith("systemic: 94%")
    assert r.params["tolerance"] == "1.00"


def test_je_rules_combined_and_holiday_named(gl):
    r = gl["JE-01"]
    rules = {e["rule"] for e in r.exceptions}
    assert {"weekend", "holiday", "round", "weekend;round"} <= rules
    hol = [e for e in r.exceptions if e["rule"] == "holiday"]
    assert {e["holiday_name"] for e in hol} == {"Labour Day", "Malaysia Day", "Christmas Day"}
    rnd = [e for e in r.exceptions if "round" in e["rule"]]
    assert all(e["amount_at_risk"] % 1000 == 0 and e["amount_at_risk"] >= 10000 for e in rnd)
    assert all(isinstance(e["amount_at_risk"], Decimal) for e in r.exceptions)


# ---- small fixtures for parameters the sample data does not exercise

def dup_project(tmp_path, tolerance):
    write_csv(tmp_path / "s.csv", ["Id", "D", "Cust", "Amt"], [
        ["S1", "2025-01-01", "C1", "10.00"], ["S2", "2025-01-02", "C1", "10.00"],
        ["S3", "2025-01-09", "C1", "10.00"], ["S4", "2025-01-01", "N/A", "10.00"], ["S5", "2025-01-01", "N/A", "10.00"]])
    m = {"mapping_version": 1, "datasets": {"sales": {"source": {"file": "s.csv"}, "columns": {
        "sale_id": {"from": "Id"}, "sale_date": {"from": "D", "type": "date"},
        "customer_id": {"from": "Cust"}, "total": {"from": "Amt", "type": "decimal"}}}},
        "runs": [{"id": "REV-01", "pack": "general/duplicates",
                  "bind": {"records": {"dataset": "sales", "fields": {"record_id": "sale_id", "date": "sale_date", "amount": "total"}}},
                  "params": {"key_fields": ["customer_id", "amount"], "date_tolerance_days": tolerance}}]}
    return write_yaml(tmp_path / "m.yaml", m), m


@pytest.mark.parametrize("tolerance,expected", [(0, set()), (1, {"dup:S1", "dup:S2"}), (7, {"dup:S1", "dup:S2", "dup:S3"})])
def test_duplicate_tolerance_and_null_keys_excluded(tmp_path, tolerance, expected):
    path, m = dup_project(tmp_path, tolerance)
    r = run_mapping(path, m)["REV-01"]
    assert r.status == "ok", r.reason
    assert ids(r) == expected                              # S4/S5 have a placeholder customer: never flagged


def test_sod_ignores_empty_initiator(tmp_path):
    write_csv(tmp_path / "p.csv", ["Id", "R", "A"], [["P1", "N/A", "N/A"], ["P2", "X", "X"]])
    m = {"mapping_version": 1, "datasets": {"po": {"source": {"file": "p.csv"}, "columns": {
        "id": {"from": "Id"}, "r": {"from": "R"}, "a": {"from": "A"}}}},
        "runs": [{"id": "ITGC-02", "pack": "itgc/segregation_of_duties",
                  "bind": {"records": {"dataset": "po", "fields": {"record_id": "id", "initiator": "r", "approver": "a"}}}}]}
    r = run_mapping(write_yaml(tmp_path / "m.yaml", m), m)["ITGC-02"]
    assert ids(r) == {"sod:P2"}
