import pytest
import yaml

from workbench.packs import PackError, check_select_only, discover_packs, validate_meta

GOOD_META = {
    "id": "TMP-01",
    "name": "Temp",
    "objective": "o",
    "assertion": "occurrence",
    "risk": "r",
    "category": "analytical",
    "contract": {"records": {"required": {"record_id": "string"}, "optional": {"amount": "decimal"}}},
    "parameters": {"key_fields": {"type": "field_list", "role": "records", "default": []}},
    "amount_column": "amount_at_risk",
    "initial_label": "inquiry",
    "needs_client_confirmation": False,
    "limitations": "l",
}


def write_pack(root, key, meta, sql="SELECT 1 AS exception_id, NULL AS amount_at_risk"):
    d = root.joinpath(*key.split("/"))
    d.mkdir(parents=True)
    (d / "meta.yaml").write_text(yaml.safe_dump(meta))
    (d / "test.sql").write_text(sql)
    return d


def test_discovery_finds_folder_pack(tmp_path):
    write_pack(tmp_path, "grp/tmp_pack", GOOD_META)
    (tmp_path / "grp" / "not_a_pack").mkdir()
    packs = discover_packs(tmp_path)
    assert list(packs) == ["grp/tmp_pack"]
    assert packs["grp/tmp_pack"].meta["id"] == "TMP-01"


def test_good_meta_has_no_problems():
    assert validate_meta(GOOD_META) == []


def test_bad_enums_reported():
    meta = dict(GOOD_META, assertion="vibes", category="x", initial_label="maybe")
    problems = validate_meta(meta)
    assert any("assertion" in p for p in problems)
    assert any("category" in p for p in problems)
    assert any("initial_label" in p for p in problems)


def test_missing_keys_and_bad_types_reported():
    meta = {k: v for k, v in GOOD_META.items() if k != "limitations"}
    meta["contract"] = {"records": {"required": {"record_id": "money"}}}
    meta["parameters"] = {"k": {"type": "field_list"}}
    problems = validate_meta(meta)
    assert any("limitations" in p for p in problems)
    assert any("money" in p for p in problems)
    assert any("role" in p for p in problems)


def test_label_map_allowed():
    meta = dict(GOOD_META, initial_label={"a": "likely_error", "b": "inquiry"},
                needs_client_confirmation={"a": False, "b": True})
    assert validate_meta(meta) == []


@pytest.mark.parametrize("sql", [
    "CREATE TABLE x AS SELECT 1",
    "SELECT 1; SELECT 2",
    "COPY (SELECT 1) TO 'x.csv'",
    "",
])
def test_select_only_rejects(sql):
    with pytest.raises(PackError):
        check_select_only(sql)


def test_select_only_accepts_with_cte():
    check_select_only("WITH a AS (SELECT 1 AS x) SELECT x FROM a")
