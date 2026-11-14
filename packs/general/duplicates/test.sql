-- REV-01 Duplicates on {{key_fields}} with a date tolerance
WITH base AS (
    SELECT record_id, date, amount, row({{key_fields}}) AS dup_key
    FROM records
    WHERE COLUMNS(c -> list_contains($key_fields, c)) IS NOT NULL
      AND date IS NOT NULL
),
chained AS (
    SELECT *,
        CASE WHEN lag(date) OVER w IS NULL
               OR date_diff('day', lag(date) OVER w, date) > $date_tolerance_days
             THEN 1 ELSE 0 END AS starts_group
    FROM base
    WINDOW w AS (PARTITION BY dup_key ORDER BY date, record_id)
),
grouped AS (
    SELECT *, sum(starts_group) OVER (PARTITION BY dup_key ORDER BY date, record_id
                                      ROWS UNBOUNDED PRECEDING) AS group_no
    FROM chained
),
sized AS (
    SELECT *,
        count(*) OVER g AS group_size,
        first_value(record_id) OVER (PARTITION BY dup_key, group_no ORDER BY date, record_id) AS group_id,
        row_number() OVER (PARTITION BY dup_key, group_no ORDER BY date, record_id) AS position
    FROM grouped
    WINDOW g AS (PARTITION BY dup_key, group_no)
)
SELECT
    'dup:' || record_id                            AS exception_id,
    'duplicate'                                    AS rule,
    group_id,
    record_id,
    date,
    CAST(dup_key AS VARCHAR)                       AS key_values,
    group_size,
    position,
    amount,
    CASE WHEN position > 1 THEN amount END         AS amount_at_risk
FROM sized
WHERE group_size > 1
