# Starter code review

The findings below compare the original starter `app/main.py` with the assignment contract and describe the corresponding implementation currently present in `app/main.py`. This is a code review, not a claim that MongoDB integration tests have passed.

| # | Where in starter | Defect and how to identify it | Current implementation |
|---|---|---|---|
| 1 | `health()` | Always returned `200`, including when MongoDB was unavailable. Stop MongoDB and call `/health`. | Sends a MongoDB `ping`; maps a failed ping to `503`. |
| 2 | `compute_late_minutes()` | Used naive/local datetime replacement and the punch-in calendar date for shift start; overnight shifts and non-local timezone hosts can produce wrong results. Try IST punch times around midnight. | Normalizes instants to UTC, uses IST shift/date helpers, and applies the strict 10-minute grace rule and floored minutes. |
| 3 | `compute_work_hours()` | Python `round()` uses ties-to-even for halfway values, contrary to the required half-up rule. Exercise a duration whose hour value is exactly halfway at two decimals. | Uses `Decimal` with `ROUND_HALF_UP` after whole-second truncation. |
| 4 | `compute_overtime()` | Did not enforce the 30-minute minimum and always placed shift end on the attendance date, which is wrong for overnight shifts. Try under-30-minute overtime and an overnight punch-out. | Applies the 30-minute threshold and advances the end for overnight shifts. |
| 5 | Request models | `EmployeeIn` and `PunchInIn` did not enforce the OpenAPI constraints, including code/email/date/shift formats, lengths, status enum, and strict epoch-millisecond input. Submit malformed values. | Pydantic fields and validators enforce employee constraints; punch-in uses a strict integer timestamp and allowed status values. |
| 6 | `create_employee()` | The pre-insert duplicate lookup was a check-then-insert race; `created_at` was naive and the response returned a datetime rather than epoch milliseconds. Send concurrent creates or inspect the response. | Relies on the unique employee index and maps duplicate-key errors to `409`; stores UTC `created_at` and serializes it to epoch milliseconds. |
| 7 | `list_employees()` | Used `page * page_size` as the offset, counted the unfiltered collection, and did not sort. Check page 1, a department filter, and multiple pages. | Uses `(page - 1) * page_size`, filtered MongoDB count, MongoDB sort by `emp_code`, and bounded pagination. |
| 8 | `punch_in()` | Unknown employees caused a dereference error; timestamp conversion used host-local time and treated a falsey timestamp as omitted; arbitrary statuses were accepted. A check-then-insert also allowed simultaneous duplicate records. | Validates employee/status/timestamp, applies UTC/IST and whole-second handling, and relies on the unique (`emp_code`, `date`) index to resolve races as `409`. |
| 9 | `punch_in()` and response construction | Returned an `id` derived from Mongo `_id`, which the contract forbids. Inspect any successful response. | Shared serialization removes `_id` and `id`; the endpoint returns the attendance schema fields. |
| 10 | `list_attendance()` | Fetched every matching record into Python before sorting and paging; ties on date lacked the required employee-code order. Large collections also made total calculation memory-heavy. | Counts in MongoDB and applies projection, sort, skip, and limit in MongoDB. |
| 11 | `list_attendance()` | Did not validate date ordering or pagination bounds and returned Mongo `_id` as `id`. Try reversed dates, page zero, or oversized page size. | Validates dates and page bounds; emits only response-schema fields and excludes Mongo identity. |
| 12 | Missing starter routes | Punch-out, regularization, all four analytics routes, and explain were TODOs despite being required by the assignment. Compare starter route decorators with `openapi.yaml`. | These routes are now implemented in `app/main.py`; their MongoDB behavior and query plans have not been integration-tested here. |
| 13 | Startup | The starter did not create the required indexes. Inspect indexes in a fresh database after startup. | Startup creates named, idempotent unique and query indexes. Actual plans have not been checked against MongoDB. |

## Examined and not defects

- The two collection names (`employees`, `attendance_logs`) and the natural attendance key (`emp_code`, `date`) match `DATA_MODEL.md`.
- The sample seed documents intentionally omit `history` and `half_day` on one legacy record; the model explicitly says missing values mean `[]` and `false`. The current serializer supplies those defaults.
- The starter's default shift values, `09:30` and `18:30`, match the OpenAPI defaults.
- `sample_seed.py` is a fixture loader, not application startup. It deletes and reloads the two sample collections when run; it is not a safe command for a database whose data should be preserved.
