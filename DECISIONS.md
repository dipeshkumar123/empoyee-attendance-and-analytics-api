# DECISIONS.md

## 1. Indexes

**Created indexes:**
- `employees.emp_code` (unique) — enforces business-key uniqueness for `emp_code`; makes `create_employee` race-safe (DuplicateKeyError → 409) and speeds up all lookups by `emp_code`.
- `employees(department, joined_on)` — supports `department_summary` pipeline's initial `$match` on `joined_on <= month_end` with optional `department` filter, and `department_trend` headcount filter.
- `employees.joined_on` — standalone index for headcount queries that only filter by join date.
- `attendance_logs(emp_code, date)` (unique) — natural key for attendance records; guarantees at most one record per employee per date; makes `punch_in` race-safe (DuplicateKeyError → 409); used by `punch_out` to find the open record, `regularize_attendance` point lookup, `employee_monthly` `$match`, and `late_leaderboard` initial `$match`.
- `attendance_logs(date DESC, emp_code ASC)` — covers `list_attendance` default sort and pagination; avoids in-memory sort on ~100k records.
- `attendance_logs(emp_code ASC, punch_in DESC)` — supports `punch_out` query for "most recent record whose punch_in ≤ punched_at" with a bounded index scan.

**Considered and rejected:**
- A compound index `attendance_logs(emp_code, status, date)` for analytics filters. The existing `(emp_code, date)` unique index already serves the high-cardinality prefix; adding `status` would increase write overhead without measurable read benefit because analytics pipelines use `$match` on `date` range first (handled by the unique index's `emp_code` prefix + range scan on `date`) or start from `employees` collection (`department_summary`). The `$densify` in `department_trend` requires a `$sort` on `day` anyway, so an index on `date` alone isn't used there. The current set keeps write amplification low while covering all query patterns.

## 2. Punch-in Race

Two identical `POST /attendance/punch-in` requests for the same `emp_code` arriving at the same instant:

1. Both requests pass validation, fetch the employee, compute the same `day` and `late_minutes`.
2. Both attempt `db.attendance_logs.insert_one(doc)` with the same `{emp_code, date}`.
3. MongoDB's unique index on `(emp_code, date)` allows exactly one insert to succeed.
4. The winner returns 201 with the created record.
5. The loser catches `DuplicateKeyError` and raises `HTTPException(409, "already punched in for this date")`.

No check-then-insert; the database is the single source of truth for uniqueness. The same pattern applies to concurrent `punch_out` (conditional update on `punch_out: None`) and `regularize_attendance` (snapshot match on base state).

## 3. Ties

Two employees tied on `total_late_minutes` at the cutoff of `limit` (e.g., limit=10, ranks 9 and 10 both have 120 minutes):

- The pipeline sorts by `total_late_minutes DESC, emp_code ASC`, then applies `$rank` (standard competition: 1, 2, 2, 4).
- The `$match: {rank: {$lte: limit}}` runs **after** ranking, so both tied employees get the same rank (e.g., 9) and both are returned.
- Response contains 11 rows (more than `limit`) because the tie at rank 9 includes two people.
- Row order: `total_late_minutes DESC, emp_code ASC` (secondary sort by `emp_code` breaks ties deterministically for output stability).

This matches the spec: "limit applies after ranking: return every employee whose rank <= limit. The response can therefore contain more than limit rows when employees tie at a rank <= limit."

## 4. Headcount

`department_summary` ensures employees with zero logs are counted (R9) by:

1. Starting the pipeline on `employees` collection with `$match: {joined_on: {$lte: month_end}}` — this is the headcount base (all employees who joined on or before month-end).
2. `$lookup` to `attendance_logs` with a sub-pipeline filtering `date` in the month. `preserveNullAndEmptyArrays: True` on the subsequent `$unwind` keeps employees who have no matching logs.
3. `$replaceRoot` merges the unwound log (or empty object) into the root so downstream expressions read `status`, `work_hours`, etc. from the log or get `null`/missing.
4. `$group` by `department` with `$addToSet: "$emp_code"` — the set includes every employee from step 1, regardless of whether they had logs, because the unwound stream has one document per employee (logs array empty → one document with empty log).
5. `headcount: {$size: "$emp_codes"}` counts unique employees in the department who joined in time.

Employees who join after `month_end` are excluded by the initial `$match`. Employees with zero logs contribute to `headcount` but not to `present_days`, `avg_work_hours`, `late_count`, etc. (their log fields are missing/null, and the aggregation expressions guard with `$ifNull`).

## 5. One Thing You Would Change for 100× Data

**Move analytics to pre-aggregated materialized views (or a separate OLAP store).**

At 100× (~10M attendance records), the current on-demand aggregations (`department_summary`, `late_leaderboard`, `department_trend`) would scan large portions of `attendance_logs` even with indexes. The `$densify` + `$lookup` in `department_trend` and the `$lookup` + `$unwind` in `department_summary` would become latency bottlenecks.

Instead, I would:
- Maintain daily rollups per department (headcount, present_count, late_count, work_hours_sum, work_hours_count) via a change stream or scheduled job that increments counters on every `punch_in`, `punch_out`, and `regularize` write.
- Serve `department_summary` and `department_trend` from the pre-computed daily rollup collection (tiny, one row per department per day).
- Keep `employee_monthly` and `late_leaderboard` on the raw collection but add a composite index `(date, emp_code)` and consider a late-minutes materialized view per employee per month for the leaderboard.
- This shifts read latency from O(scanned records) to O(departments × days) and makes the API horizontally scalable.