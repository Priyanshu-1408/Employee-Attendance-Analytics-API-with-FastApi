# HROne Employee Attendance & Analytics API

FastAPI and MongoDB service for employee records, attendance punches and corrections, monthly analytics, department trends, and MongoDB explain output. All application code is in `app/main.py`.

## Requirements and setup

Use Python 3.11 or newer and MongoDB 6.0 or newer. From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

The requirements install FastAPI, Uvicorn, PyMongo, Pydantic, and python-dotenv. On macOS/Linux, create the environment with `python3.11 -m venv .venv` and activate it with `source .venv/bin/activate`.

## MongoDB configuration

Set `MONGO_URI` and `MONGO_DB` in the shell, or place them in a local `.env` file in the repository root:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

The application loads `.env` with `override=False`, so explicitly set environment variables take precedence. If unset, the code defaults to `mongodb://localhost:27017` and `attendance_db`. Start a MongoDB 6.0+ instance before starting the app. The app creates its indexes at startup; startup fails if MongoDB/index creation fails.

## Seed the sample data

With MongoDB running and the same `MONGO_URI` / `MONGO_DB` configured:

```powershell
python sample_seed.py
```

**This script deletes the existing documents in both `employees` and `attendance_logs` before inserting the sample fixtures.** Use a disposable development database. The sample set illustrates document shapes and does not cover all analytics edge cases.

## Run the API

```powershell
uvicorn app.main:app --port 8000
```

Readiness is available at `GET /health`. Interactive API docs are at [http://localhost:8000/docs](http://localhost:8000/docs), ReDoc at [http://localhost:8000/redoc](http://localhost:8000/redoc), and the generated OpenAPI document at [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json).

## Checks and remaining verification

Manual Swagger checks covered employee creation and listing; attendance punch-in and punch-out; duplicate punch-in returning HTTP 409; attendance regularization and audit history; employee monthly analytics; department summary; late leaderboard; and department trend.

MongoDB Explain responses were inspected for `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, and `department_trend`. The inspected responses executed successfully and showed the expected index usage for the tested data. These checks cover the tested data only; they do not establish behavior for all data shapes or query plans at scale.

No automated test suite is present in this repository. Exhaustive edge-case testing has not been completed; cases still needing broader coverage include concurrent requests, overnight shifts, employees with no logs, mid-month joiners, weekend records, leaderboard ties, and missing trend dates. The manual checks do not guarantee correctness under every concurrency condition or with large datasets.
