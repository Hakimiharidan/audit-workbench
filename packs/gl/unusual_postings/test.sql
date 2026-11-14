-- JE-01 weekend / holiday / round-amount postings (all arithmetic in DECIMAL)
WITH flagged AS (
    SELECT l.*,
        list_contains($weekend_days, isodow(l.posting_date))             AS is_weekend,
        h.holiday_name                                                    AS holiday_name,
        h.holiday_date IS NOT NULL                                        AS is_holiday,
        abs(l.amount) >= $min_amount AND abs(l.amount) % $round_to = 0    AS is_round
    FROM lines l
    LEFT JOIN (SELECT holiday_date, string_agg(holiday_name, ' / ') AS holiday_name
               FROM holidays GROUP BY holiday_date) h
           ON h.holiday_date = l.posting_date
)
SELECT
    je_id || ':' || line_no                      AS exception_id,
    concat_ws(';',
        CASE WHEN is_weekend THEN 'weekend' END,
        CASE WHEN is_holiday THEN 'holiday' END,
        CASE WHEN is_round THEN 'round' END)     AS rule,
    je_id,
    line_no,
    posting_date,
    dayname(posting_date)                        AS weekday,
    holiday_name,
    account,
    description,
    posted_by,
    source,
    amount,
    abs(amount)                                  AS amount_at_risk
FROM flagged
WHERE is_weekend OR is_holiday OR is_round
