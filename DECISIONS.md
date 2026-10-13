# DECISIONS.md

### 1. Indexes

Six targeted indexes were created:

- `employees(emp_code)` (unique): guarantees business key uniqueness and powers O(1) employee lookups.
- `employees(department, emp_code)`: supports department-filtered listings and analytics with index-covered sorting.
- `employees(joined_on)`: optimizes headcount eligibility queries in the department summary and trend.
- `attendance_logs(emp_code, date)` (unique): enforces one record per employee per day and eliminates punch-in race conditions.
- `attendance_logs(date desc, emp_code asc)`: satisfies default attendance list queries and date-range scans without in-memory sorting.
- `attendance_logs(emp_code, punch_in asc)`: accelerates punch-out lookups that find the most recent open punch-in.

`(status, date, emp_code)` was considered and rejected — status-filtered listings are infrequent, and leading with `status` prevents the index from serving general date-range scans.

### 2. Punch-in Race

When two identical punch-in requests arrive at the same instant, neither uses a check-then-insert pattern. Both compute the attendance date and call `insert_one` directly. MongoDB's unique index on `(emp_code, date)` is the concurrency barrier: exactly one insertion succeeds (201) and the other triggers a `DuplicateKeyError`, which is caught and returned as 409. The database enforces the constraint atomically — no application-level lock is needed.

### 3. Ties

The leaderboard uses `$setWindowFields` with `$rank`, which assigns equal rank to ties and skips the next rank (1, 2, 2, 4). The subsequent `$match` filters `rank <= limit`, so if multiple employees tie at the cutoff rank, all of them pass the filter — the response correctly contains more than `limit` rows. Sorting by array index after a `$limit` was explicitly avoided because it produces wrong ranks when ties exist.

### 4. Headcount

Employees with zero logs must still count toward headcount. Starting the pipeline from `attendance_logs` would silently drop them — employees who never punched in that month simply wouldn't appear. Instead, the pipeline starts from `employees`, filters `joined_on <= month_end` (R9), and brings in logs via `$lookup` with `preserveNullAndEmptyArrays: true`, which keeps every eligible employee in the pipeline even when the lookup returns nothing. After `$unwind`, `$addToSet` on `emp_code` deduplicates across multiple log entries, giving an accurate headcount regardless of how many days an employee worked.

### 5. 100x Scale

At 100x the data (~10M logs), the main bottlenecks would be the analytics aggregations and the cross-collection `$lookup` in the department summary. The fix is pre-aggregated monthly rollup documents — one per `(emp_code, month)` — updated atomically on each punch-out and regularization. Analytics then become point lookups instead of full collection scans, and the department summary joins against rollups rather than raw logs. On the write side, sharding `attendance_logs` on `emp_code` distributes write load evenly, and routing analytics reads to a secondary replica eliminates contention with the live punch traffic.
