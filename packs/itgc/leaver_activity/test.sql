-- ITGC-01 Leaver activity
-- Rule after_leave: event dated after the actor's leave date.
-- Rule leaver_still_active: leave date recorded but status still in $active_values.
SELECT
    'after_leave:' || e.event_id              AS exception_id,
    'after_leave'                             AS rule,
    e.event_id                                AS event_id,
    e.actor                                   AS person_id,
    e.event_date                              AS event_date,
    p.leave_date                              AS leave_date,
    p.status                                  AS status,
    date_diff('day', p.leave_date, e.event_date) AS days_after_leave,
    e.amount                                  AS amount_at_risk
FROM events e
JOIN people p ON p.person_id = e.actor
WHERE p.leave_date IS NOT NULL
  AND e.event_date > p.leave_date

UNION ALL

SELECT
    'leaver_still_active:' || p.person_id,
    'leaver_still_active',
    NULL,
    p.person_id,
    NULL,
    p.leave_date,
    p.status,
    NULL,
    NULL
FROM people p
WHERE p.leave_date IS NOT NULL
  AND list_contains($active_values, p.status)
