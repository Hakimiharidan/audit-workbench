-- PAY-01 (a) attribution, (b) recalculation
SELECT
    'attribution:' || c.sale_id                 AS exception_id,
    'attribution'                               AS rule,
    c.sale_id,
    s.employee_id                               AS sale_employee,
    c.employee_id                               AS commission_employee,
    s.total_amount,
    p.commission_rate,
    c.commission_amount,
    CAST(NULL AS DECIMAL(18,2))                 AS expected_commission,
    c.commission_amount                         AS amount_at_risk
FROM commissions c
JOIN sales s ON s.sale_id = c.sale_id
LEFT JOIN people p ON p.person_id = s.employee_id
WHERE c.employee_id IS DISTINCT FROM s.employee_id

UNION ALL

SELECT
    'recalc:' || c.sale_id,
    'recalc',
    c.sale_id,
    s.employee_id,
    c.employee_id,
    s.total_amount,
    p.commission_rate,
    c.commission_amount,
    CAST(round(s.total_amount * p.commission_rate, 2) AS DECIMAL(18,2)),
    CAST(round(abs(c.commission_amount - s.total_amount * p.commission_rate), 2) AS DECIMAL(18,2))
FROM commissions c
JOIN sales s ON s.sale_id = c.sale_id
JOIN people p ON p.person_id = s.employee_id
WHERE p.commission_rate IS NOT NULL
  AND abs(c.commission_amount - s.total_amount * p.commission_rate) > $tolerance
