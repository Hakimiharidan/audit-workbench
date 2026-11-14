"""Discover test packs (a folder with meta.yaml + test.sql) and check their metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

ASSERTIONS = {"existence", "completeness", "accuracy", "occurrence", "cutoff", "classification"}
CATEGORIES = {"itgc", "financial", "analytical"}
LABELS = {"likely_error", "inquiry", "control_deficiency"}
FIELD_TYPES = {"string", "integer", "decimal", "ratio", "date", "boolean"}
SQL_TYPES = {
    "string": "VARCHAR", "integer": "BIGINT", "decimal": "DECIMAL(18,2)",
    "ratio": "DECIMAL(18,6)", "date": "DATE", "boolean": "BOOLEAN",
}
PARAM_TYPES = {
    "integer", "decimal", "string", "boolean",
    "string_list", "integer_list", "field_list", "date_list_file",
}
REQUIRED_KEYS = [
    "id", "name", "objective", "assertion", "risk", "category", "contract",
    "parameters", "initial_label", "needs_client_confirmation", "limitations",
]

DEFAULT_PACKS_ROOT = Path(__file__).resolve().parent.parent / "packs"


class PackError(Exception):
    """A pack folder is malformed or its SQL is not allowed."""


@dataclass
class Pack:
    key: str
    path: Path
    meta: dict
    sql: str

    @property
    def id(self) -> str:
        return str(self.meta.get("id", self.key))

    def label_for(self, rule: str | None) -> str:
        return _per_rule(self.meta.get("initial_label"), rule)

    def confirmation_for(self, rule: str | None) -> bool:
        return bool(_per_rule(self.meta.get("needs_client_confirmation"), rule))

    def roles(self) -> dict:
        return self.meta.get("contract") or {}

    def optional_roles(self) -> list[str]:
        return list(self.meta.get("optional_roles") or [])


def _per_rule(value, rule):
    if isinstance(value, dict):
        if rule is not None:
            # a combined rule like "weekend;round" uses its first part
            for part in str(rule).split(";"):
                if part in value:
                    return value[part]
        return next(iter(value.values()))
    return value


def discover_packs(root: Path) -> dict[str, Pack]:
    """Return every pack under root, keyed by its posix path relative to root, sorted."""
    root = Path(root)
    packs: dict[str, Pack] = {}
    if not root.is_dir():
        return packs
    for meta_path in sorted(root.rglob("meta.yaml")):
        folder = meta_path.parent
        sql_path = folder / "test.sql"
        if not sql_path.is_file():
            continue
        key = folder.relative_to(root).as_posix()
        try:
            meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise PackError(f"{meta_path}: invalid YAML: {exc}") from exc
        packs[key] = Pack(key=key, path=folder, meta=meta, sql=sql_path.read_text(encoding="utf-8"))
    return packs


def _check_enum_or_map(name, value, allowed, problems, kind=str):
    if isinstance(value, dict):
        if not value:
            problems.append(f"{name}: empty map")
        for rule, v in value.items():
            if allowed is not None and v not in allowed:
                problems.append(f"{name}.{rule}: '{v}' not one of {sorted(allowed)}")
            if allowed is None and not isinstance(v, kind):
                problems.append(f"{name}.{rule}: must be {kind.__name__}")
    elif allowed is not None and value not in allowed:
        problems.append(f"{name}: '{value}' not one of {sorted(allowed)}")
    elif allowed is None and not isinstance(value, kind):
        problems.append(f"{name}: must be {kind.__name__} or a map of rule -> {kind.__name__}")


def validate_meta(meta: dict) -> list[str]:
    """Return a list of human-readable problems with a pack's meta.yaml (empty = valid)."""
    problems: list[str] = []
    if not isinstance(meta, dict):
        return ["meta.yaml must be a mapping"]
    for key in REQUIRED_KEYS:
        if key not in meta:
            problems.append(f"missing required key '{key}'")
    if "assertion" in meta and meta["assertion"] not in ASSERTIONS:
        problems.append(f"assertion: '{meta['assertion']}' not one of {sorted(ASSERTIONS)}")
    if "category" in meta and meta["category"] not in CATEGORIES:
        problems.append(f"category: '{meta['category']}' not one of {sorted(CATEGORIES)}")
    if "initial_label" in meta:
        _check_enum_or_map("initial_label", meta["initial_label"], LABELS, problems)
    if "needs_client_confirmation" in meta:
        _check_enum_or_map("needs_client_confirmation", meta["needs_client_confirmation"], None, problems, bool)

    contract = meta.get("contract")
    if contract is not None:
        if not isinstance(contract, dict) or not contract:
            problems.append("contract: must map role names to {required, optional}")
        else:
            for role, spec in contract.items():
                if not isinstance(spec, dict) or "required" not in spec:
                    problems.append(f"contract.{role}: needs a 'required' map of field -> type")
                    continue
                for section in ("required", "optional"):
                    for field, ftype in (spec.get(section) or {}).items():
                        if ftype not in FIELD_TYPES:
                            problems.append(f"contract.{role}.{section}.{field}: type '{ftype}' not one of {sorted(FIELD_TYPES)}")
    for role in meta.get("optional_roles") or []:
        if not isinstance(contract, dict) or role not in contract:
            problems.append(f"optional_roles: '{role}' is not a contract role")

    params = meta.get("parameters")
    if params is not None:
        if not isinstance(params, dict):
            problems.append("parameters: must be a map")
        else:
            for name, spec in params.items():
                if not isinstance(spec, dict) or spec.get("type") not in PARAM_TYPES:
                    problems.append(f"parameters.{name}: type must be one of {sorted(PARAM_TYPES)}")
                    continue
                if spec["type"] == "field_list":
                    role = spec.get("role")
                    if not role:
                        problems.append(f"parameters.{name}: field_list needs a 'role'")
                    elif isinstance(contract, dict) and role not in contract:
                        problems.append(f"parameters.{name}: role '{role}' is not a contract role")

    note = meta.get("systemic_note")
    if note is not None:
        if not isinstance(note, dict) or not {"rule", "role", "threshold"} <= set(note):
            problems.append("systemic_note: needs rule, role and threshold")
    return problems


def check_select_only(sql: str) -> None:
    """Raise PackError unless sql is exactly one SELECT statement."""
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise PackError(f"SQL does not parse: {exc}") from exc
    if len(statements) != 1:
        raise PackError(f"test.sql must contain exactly one statement, found {len(statements)}")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise PackError(f"test.sql must be a SELECT, found {statements[0].type.name}")
