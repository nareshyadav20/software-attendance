# DECISIONS.md

## Architectural & Technical Decisions

### 1. Indexes
I created:
* `employees`: `{emp_code: 1}` (unique) to guarantee employee code uniqueness; `{department: 1, joined_on: 1}` and `{joined_on: 1}` for filtered listings and headcount evaluations.
* `attendance_logs`: `{emp_code: 1, date: 1}` (unique) to enforce at most one record per employee per attendance day; `{date: -1, emp_code: 1}` to support fast index-backed pagination and sorting on `/attendance`; `{emp_code: 1, punch_in: -1}` for instant punch-out lookups; `{date: 1, late_minutes: 1}` and `{date: 1, status: 1}` for analytics.
* **Considered and rejected**: A bloated multi-field compound index covering all log fields was rejected to avoid heavy B-tree index write amplification on high-frequency punch workloads.

### 2. Punch-in Race
When two identical punch-in requests arrive simultaneously:
Both compute the same attendance date and attempt `db.attendance_logs.insert_one(doc)` concurrently without check-then-insert. The unique compound index on `(emp_code, date)` guarantees atomic serialization at the WiredTiger storage engine level. Exactly one insert succeeds and returns `201 Created`; the other fails with MongoDB's `DuplicateKeyError` (E11000), which is caught and translated into `409 Conflict`.

### 3. Ties
Leaderboard rankings use MongoDB's `$rank` window operator in `$setWindowFields` to implement standard competition ranking (1, 2, 2, 4). When employees tie on `total_late_minutes`, they share the same rank. Per the OpenAPI contract, the query filter `rank <= limit` is evaluated after ranking, with rows sorted by `total_late_minutes` descending then `emp_code` ascending. Thus, if a tie occurs at the cutoff, all tied employees are returned, allowing the response to exceed `limit`.

### 4. Headcount
The department summary pipeline initiates from the `employees` collection instead of `attendance_logs`. It filters by `joined_on <= end_of_month` (R9) and uses `$lookup` to join each employee's monthly attendance logs. Employees without any attendance logs yield an empty `logs: []` array but remain in the grouping, ensuring full headcount inclusion.

### 5. One Thing to Change for 100x Data
At 100x scale (~10 million records), on-the-fly cross-collection aggregations will strain I/O and RAM. I would implement an asynchronous daily rollup collection (`department_daily_rollups`) maintained via MongoDB Change Streams or worker jobs, transforming O(N) log aggregations into fast O(1) point lookups.
