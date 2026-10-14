# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `create_indexes()` | The employee joining-date filter had no standalone index beginning with `joined_on`. | The `department_summary` explain plan showed `COLLSCAN` on `employees`. | Added `ix_employee_joined_on` on `joined_on`; explain now shows `IXSCAN`. |
| 2 | `department_summary()` | Average work hours were not calculated correctly when averaging values held inside an array in a grouped aggregation. | The department summary returned `avg_work_hours: null`. | Changed the aggregation to sum work hours and count records, then calculate the average safely. |
| 3 | Attendance regularization endpoint | A request that makes no changes should not create a meaningless audit entry. | PATCH with unchanged values returned a validation error. | Reject unchanged regularization requests and retain audit history for successful changes. |
| 4 | Attendance punch-in endpoint and unique index | Duplicate punch-ins for the same employee and date must be prevented, including concurrent requests. | Repeated punch-in requests can otherwise create duplicate attendance records. | Added a unique compound index on `emp_code` and `date`, with duplicate-key handling. |

## Reviewed but not treated as defects

- The late leaderboard uses an index for the monthly date filter; the explain plan showed `IXSCAN` and no collection scan.
- The department trend uses an index for its date range. Its in-memory sort is expected because the selected index does not provide the requested ascending sort order directly.
- A `FETCH` stage after `IXSCAN` is normal because MongoDB may need to retrieve the full matching document.