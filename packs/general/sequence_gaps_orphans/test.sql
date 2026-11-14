-- REV-02 Sequence gaps and orphans
WITH nums AS (
    SELECT id, CAST(regexp_extract(id, $id_pattern, 1) AS BIGINT) AS n
    FROM sequence
    WHERE id IS NOT NULL AND regexp_matches(id, $id_pattern)
),
missing AS (
    SELECT unnest(range(min(n), max(n) + 1)) AS n FROM nums
    EXCEPT
    SELECT n FROM nums
),
orphans AS (
    SELECT parent_id, sum(amount) AS amount
    FROM children
    WHERE parent_id IS NOT NULL
      AND parent_id NOT IN (SELECT id FROM sequence WHERE id IS NOT NULL)
    GROUP BY parent_id
),
orphan_nums AS (
    SELECT parent_id, amount,
           CASE WHEN regexp_matches(parent_id, $id_pattern)
                THEN CAST(regexp_extract(parent_id, $id_pattern, 1) AS BIGINT) END AS n
    FROM orphans
)
SELECT
    'gap:' || m.n                                       AS exception_id,
    'gap'                                               AS rule,
    m.n                                                 AS missing_number,
    CAST(NULL AS VARCHAR)                               AS parent_id,
    m.n IN (SELECT n FROM orphan_nums WHERE n IS NOT NULL) AS gap_matches_orphan,
    CAST(NULL AS DECIMAL(18,2))                         AS amount_at_risk
FROM missing m

UNION ALL

SELECT
    'orphan:' || o.parent_id,
    'orphan',
    o.n,
    o.parent_id,
    coalesce(o.n IN (SELECT n FROM missing), false),
    o.amount
FROM orphan_nums o
