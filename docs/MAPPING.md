# Mapping files

A mapping connects your extract to the test packs. It has two layers. The
examples `mappings/example_retail.yaml` and `mappings/example_gl.yaml` are
commented line by line — copy one and edit the right-hand sides.

Check a mapping without loading any data rows:

```bash
workbench validate-mapping my_mapping.yaml
```

Every problem is reported in one pass as
`file:yaml.path: what is wrong — how to fix`, and the exit code is 1.

## Layer 1 — datasets

```yaml
mapping_version: 1
null_placeholders: ["N/A", ""]        # converted to NULL and counted per column

datasets:
  sales:
    source: {file: ../data/sales.xlsx, sheet: Sales}   # paths relative to the mapping file
    columns:                           # field: {from: source header, type: ...}
      sale_id:      {from: Receipt No, type: string}
      sale_date:    {from: Date, type: date, format: "%d/%m/%Y"}   # format only if not ISO
      total_amount: {from: Total, type: decimal}
    primary_key: [sale_id]             # duplicates fail the gate
    references:                        # foreign keys, checked both ways
      - {fields: [employee_id], to: employees, to_fields: [employee_id], require_children: false}
    sequence: {field: sale_id, pattern: '^R(\d+)$'}   # one capture group around the number
    control_totals: [total_amount]     # default: every decimal field; [] for none
    balance_check: {group_by: [je_id], amount: amount, tolerance: 0.01}
    expected_rows: 1482                # record count reported with the extract
    null_placeholders: ["N/A"]         # optional per-dataset override
```

Rules:

- Headers must match exactly. A **missing** mapped column rejects the dataset
  (gate fail). **Extra** columns are never loaded; their names are listed.
- Values are read as text and converted in the database. A value that cannot
  be converted (`1,234.50`, `£12`, `12.345` as money, `05/01/2025` with no
  `format`) is listed by row and fails the gate. Float noise from Excel
  (`12.3400000001`) is rounded and counted.
- Types: `string`, `integer`, `decimal` (2 places), `ratio` (6 places), `date`, `boolean`.

## Layer 2 — runs

```yaml
runs:
  - id: REV-01                         # unique, max 31 characters (sheet name)
    pack: general/duplicates           # folder under packs/
    bind:
      records:                         # every role in the pack's contract
        dataset: sales
        fields: {record_id: sale_id, date: sale_date, amount: total_amount}
    params:
      key_fields: [customer_id, amount, employee_id]
      date_tolerance_days: 0
```

- Every required contract field must be bound to a dataset field of the same type.
- The same pack can run several times with different ids and bindings.
- A run whose dataset could not be loaded is **skipped** and logged, not failed.
- `field_list` parameters may name contract fields or other fields of the bound dataset.

## Personal data

Map only what the tests need. Names, phone numbers, ID numbers and dates of
birth are usually not needed; leave them unmapped and they are never loaded.
Keep mappings for real extracts out of the repository.
