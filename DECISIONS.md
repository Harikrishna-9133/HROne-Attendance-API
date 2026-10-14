# DECISIONS.md

1. **Indexes:** I created a unique index on `emp_code` to prevent duplicate employees, and a unique compound index on `(emp_code, date)` to guarantee one attendance record per employee per date. Additional indexes support employee department and joining-date filters, attendance date ranges, employee attendance history, and late-attendance queries. I added a standalone `joined_on` index after the department summary explain plan showed a collection scan.

2. **Punch-in race condition:** Two identical punch-in requests may arrive simultaneously. The unique compound index is the final safeguard: MongoDB accepts at most one record for the same employee and date. The duplicate-key exception is handled so the losing request does not create another attendance record.

3. **Leaderboard ties:** Employees are sorted by total late minutes in descending order, with employee code in ascending order as the deterministic tie-breaker. The result therefore remains predictable when employees have equal totals at the cutoff.

4. **Headcount:** The department summary starts from employees who joined before the period ends and looks up their attendance records. This includes eligible employees with zero attendance logs in the month.

5. **Scaling to 100x data:** I would inspect real query plans and workload metrics, then optimize compound indexes for the most frequent filters and sorts. I would also consider pagination, suitable MongoDB deployment capacity, and avoiding unnecessary fields in aggregation pipelines.