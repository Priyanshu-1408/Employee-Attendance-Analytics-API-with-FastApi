# Implementation decisions

1. **Indexes.** Unique employee code and attendance `(emp_code, date)` indexes enforce identity and one daily record. Department/code and attendance date/employee indexes support lists; employee department/joined and joined indexes support headcount. Employee/date/status, status/date, and date/late/employee indexes support analytics. I rejected email indexing because no endpoint filters by email.

2. **Punch-in race.** Both requests can attempt insertion. MongoDB's unique attendance key allows one record for that employee/date; the other insert raises a duplicate-key error, mapped to `409`. The guarantee comes from the index, not a pre-insert check.

3. **Ties.** The leaderboard ranks by total late minutes alone, filters on `rank <= limit`, then sorts by employee code. Competition ranking skips the next rank after a tie, and every employee tied at the cutoff is returned.

4. **Headcount.** Department summary starts with employees joined by month end, then looks up logs and groups per employee before department. An employee with no logs still contributes one to headcount.

5. **100x data.** I would measure plans and workload, then consider daily summary collections for repeated analytics and partitioning or sharding if one indexed deployment no longer meets latency needs.
