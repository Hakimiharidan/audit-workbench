import copy

from conftest import write_yaml
from workbench.cli import main
from workbench.mapping import validate_mapping


def issues_for(tmp_path, mapping, packs):
    path = write_yaml(tmp_path / "m.yaml", mapping)
    return validate_mapping(path, packs)


def test_valid_example_has_no_issues(small_project, sod_pack_root):
    root, path, _ = small_project
    assert validate_mapping(path, sod_pack_root) == []


def test_reports_all_issues_in_one_pass(small_project, sod_pack_root):
    root, _, mapping = small_project
    m = copy.deepcopy(mapping)
    m["datasets"]["orders"]["columns"]["value"]["type"] = "money"          # 1 bad type
    m["datasets"]["orders"]["primary_key"] = ["nope"]                       # 2 unknown key field
    m["runs"][0]["pack"] = "itgc/does_not_exist"                            # 3 unknown pack
    issues = issues_for(root, m, sod_pack_root)
    text = "\n".join(map(str, issues))
    assert len(issues) == 3, text
    assert "money" in text and "nope" in text and "does_not_exist" in text
    # each issue says where and how to fix
    for i in issues:
        assert " — " in str(i) and str(i).startswith(str(root / "m.yaml"))


def test_missing_column_suggests_near_match(small_project, sod_pack_root):
    root, _, mapping = small_project
    m = copy.deepcopy(mapping)
    m["datasets"]["orders"]["columns"]["order_date"]["from"] = "when"
    issues = issues_for(root, m, sod_pack_root)
    assert len(issues) == 1
    assert "datasets.orders.columns.order_date.from" in str(issues[0])
    assert "'When'" in str(issues[0])


def test_missing_source_file(small_project, sod_pack_root):
    root, _, mapping = small_project
    m = copy.deepcopy(mapping)
    m["datasets"]["orders"]["source"]["file"] = "data/missing.csv"
    issues = issues_for(root, m, sod_pack_root)
    assert any("not found" in str(i) for i in issues)


def test_bad_run_ids_rejected(small_project, sod_pack_root):
    root, _, mapping = small_project
    m = copy.deepcopy(mapping)
    r = m["runs"][0]
    m["runs"] = [dict(r, id="X" * 32), dict(r, id="bad/id"), dict(r, id="SAME"), dict(r, id="SAME")]
    text = "\n".join(map(str, issues_for(root, m, sod_pack_root)))
    assert "31" in text and "bad/id" in text and "duplicate" in text


def test_contract_field_unbound(small_project, sod_pack_root):
    root, _, mapping = small_project
    m = copy.deepcopy(mapping)
    del m["runs"][0]["bind"]["records"]["fields"]["approver"]
    issues = issues_for(root, m, sod_pack_root)
    assert len(issues) == 1 and "approver" in str(issues[0])


def test_contract_type_mismatch(small_project, sod_pack_root):
    root, _, mapping = small_project
    m = copy.deepcopy(mapping)
    m["runs"][0]["bind"]["records"]["fields"]["amount"] = "order_date"
    issues = issues_for(root, m, sod_pack_root)
    assert len(issues) == 1 and "decimal" in str(issues[0]) and "date" in str(issues[0])


def test_unknown_param(small_project, sod_pack_root):
    root, _, mapping = small_project
    m = copy.deepcopy(mapping)
    m["runs"][0]["params"] = {"tolerance": 3}
    issues = issues_for(root, m, sod_pack_root)
    assert len(issues) == 1 and "tolerance" in str(issues[0])


def test_reference_to_unknown_dataset(small_project, sod_pack_root):
    root, _, mapping = small_project
    m = copy.deepcopy(mapping)
    m["datasets"]["orders"]["references"] = [{"fields": ["raised_by"], "to": "staff", "to_fields": ["id"]}]
    issues = issues_for(root, m, sod_pack_root)
    assert any("staff" in str(i) for i in issues)


def test_yaml_syntax_error_reported(tmp_path, sod_pack_root):
    p = tmp_path / "bad.yaml"
    p.write_text("datasets: [unclosed\n")
    issues = validate_mapping(p, sod_pack_root)
    assert len(issues) == 1 and "YAML" in str(issues[0])


def test_cli_exit_codes(small_project, sod_pack_root, capsys):
    root, path, mapping = small_project
    assert main(["validate-mapping", str(path), "--packs", str(sod_pack_root)]) == 0
    assert "OK" in capsys.readouterr().out
    m = copy.deepcopy(mapping)
    m["runs"][0]["pack"] = "nope/nope"
    bad = write_yaml(root / "bad.yaml", m)
    assert main(["validate-mapping", str(bad), "--packs", str(sod_pack_root)]) == 1
    out = capsys.readouterr().out
    assert "1 problem" in out and "nope/nope" in out
