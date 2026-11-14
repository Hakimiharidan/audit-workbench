-- ITGC-02 Segregation of duties: initiator = approver
SELECT
    'sod:' || record_id   AS exception_id,
    'same_person'         AS rule,
    record_id,
    date,
    initiator,
    approver,
    amount                AS amount_at_risk
FROM records
WHERE initiator IS NOT NULL
  AND initiator = approver
