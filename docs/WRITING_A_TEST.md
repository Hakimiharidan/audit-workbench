# Writing a test pack

A test pack is a folder under `packs/` with two files. Nothing else is needed:
the engine discovers it, `validate-mapping` checks it, and the workpaper gets a
sheet for each run of it.

```
packs/<group>/<name>/
  meta.yaml
  test.sql
```

## meta.yaml

```yaml
id: TMP-01                      # shown in the workpaper
name: Large records
objective: List records at or above a threshold.
assertion: existence            # existence | completeness | accuracy | occurrence | cutoff | classification
risk: Large items carry more risk of material misstatement.
category: analytical            # itgc | financial | analytical
contract:                       # the input roles this test reads, and their fields
  records:
    required: {record_id: string, amount: decimal}
    optional: {date: date}      # unbound optional fields arrive as typed NULLs
optional_roles: []              # roles that may be left unbound (they arrive empty)
parameters:
  threshold: {type: decimal, default: "10000"}
amount_column: amount_at_risk
initial_label: inquiry          # likely_error | inquiry | control_deficiency, or a map rule -> label
needs_client_confirmation: false  # or a map rule -> true/false
limitations: >
  What the test cannot tell you. Shown on the Limitations sheet.
# optional: summary note when one rule hits most rows of a role
# systemic_note: {rule: recalc, role: commissions, threshold: 0.5}
```

Field types: `string`, `integer`, `decimal` (money, 2 places), `ratio` (rates,
6 places), `date`, `boolean`.

Parameter types: `integer`, `decimal`, `string`, `boolean`, `string_list`,
`integer_list`, `field_list` (names of fields, with `role:`), `date_list_file`
(a YAML file of dates registered as a table named by `table:`). Add
`required: true` when there is no sensible default.

## test.sql

- Exactly one `SELECT` (CTEs are fine). Anything else is rejected.
- Read only the role views named in the contract (`records`, `people`, …).
  The SQL never sees source column names, and the connection cannot read files.
- Value parameters are bound: `WHERE amount >= $threshold`.
- `field_list` parameters are inserted as quoted identifiers with
  `{{name}}` (for example `row({{key_fields}})`) and are also bound as a list
  of names, so `COLUMNS(c -> list_contains($key_fields, c)) IS NOT NULL` works.
- Return one row per exception with:
  - `exception_id` — unique and **stable**: build it from keys, never row order
    (for example `'large:' || record_id`);
  - `amount_at_risk` — DECIMAL or NULL (control exceptions);
  - optionally `rule` (used for per-rule labels; `a;b` for several) and any
    key fields a reviewer needs.

## Try it

Add a run to a mapping, then:

```bash
workbench validate-mapping my_mapping.yaml
workbench run my_mapping.yaml --config config.yaml --out output/try
```

`tests/test_acceptance.py::test_temp_pack_folder_runs` shows a complete example.
