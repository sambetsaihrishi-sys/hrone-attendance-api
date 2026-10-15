# DECISIONS.md

### 1. Indexes
I created a unique index on employee codes to prevent duplicate employees and a unique compound index on employee code and attendance date to prevent duplicate daily records. Additional indexes support date-based attendance queries and employee punch-in lookups. I considered adding more indexes for every analytics filter but rejected that because extra indexes increase storage and write costs.

### 2. Punch-in race
If two identical punch-in requests arrive simultaneously, both may initially find no existing attendance record. Both then attempt insertion. MongoDB's unique compound index allows only one insert. The other receives a duplicate-key error, which the application handles as a conflict. This prevents duplicate attendance records.

### 3. Ties
The leaderboard sorts employees by late minutes and applies the requested limit. Employees tied at the cutoff are not automatically included beyond the limit. The database returns only the requested number of results.

### 4. Headcount
The department summary starts with the employees collection and joins attendance records. This includes employees who have no attendance logs, so they still contribute to department headcount.

### 5. Scaling
For 100 times more data, I would optimize aggregation pipelines using execution statistics, add indexes based on actual query patterns, and consider precomputed monthly summaries for frequently requested analytics.