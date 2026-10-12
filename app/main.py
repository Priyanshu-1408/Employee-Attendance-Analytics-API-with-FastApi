"""Employee Attendance & Analytics API starter (Phase 1 foundations)."""

import os
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from bson.json_util import dumps as bson_dumps
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

# Existing process variables take precedence over values read from .env.
load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    """Create query and uniqueness indexes; errors intentionally fail startup."""
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employee_emp_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employee_department_joined",
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="ix_attendance_employee_date_status",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date"
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_late_employee",
    )


def truncate_seconds(value: datetime) -> datetime:
    """Normalize an instant to aware UTC and discard fractional seconds."""
    if value.tzinfo is None:
        value = value.replace(
            tzinfo=UTC
        )  # BSON datetimes are UTC; PyMongo may return them naive.
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 100_000_000_000 <= value <= 4_102_444_800_000
    ):
        raise ValueError(
            "timestamp must be an epoch-millisecond integer in the supported range"
        )
    try:
        return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC))
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("timestamp is out of range") from exc


def datetime_to_epoch_ms(value: datetime) -> int:
    value = truncate_seconds(value)
    return int(value.timestamp()) * 1000


def _as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = _as_ist(truncate_seconds(punch_in))
    start = time.fromisoformat(shift_start)
    end = time.fromisoformat(shift_end)
    day = local.date()
    if end <= start and local.time().replace(tzinfo=None) < end:
        day -= timedelta(days=1)
    return day.isoformat()


def _shift_datetime(date_str: str, shift: str) -> datetime:
    day = date.fromisoformat(date_str)
    h, m = map(int, shift.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST)


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    date_str: Optional[str] = None,
    shift_end: Optional[str] = None,
) -> int:
    punch_in = truncate_seconds(punch_in)
    local = punch_in.astimezone(IST)
    if date_str is None:
        date_str = local.date().isoformat()
        if shift_end is not None:
            date_str = attendance_date(punch_in, shift_start, shift_end)
    start = _shift_datetime(date_str, shift_start).astimezone(UTC)
    elapsed_seconds = (punch_in - start).total_seconds()
    return int(elapsed_seconds // 60) if elapsed_seconds > 600 else 0


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds()
    return round_half_up(Decimal(str(seconds)) / Decimal(3600), 2)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: Optional[str] = None,
) -> int:
    end = _shift_datetime(date_str, shift_end)
    if shift_start is not None and time.fromisoformat(shift_end) <= time.fromisoformat(
        shift_start
    ):
        end += timedelta(days=1)
    elapsed = (truncate_seconds(punch_out) - end.astimezone(UTC)).total_seconds()
    minutes = int(elapsed // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    return work_hours is not None and round_half_up(work_hours, 2) < 4.50


def serialize(value):
    """Convert Mongo values to API values and omit internal Mongo identity."""
    if isinstance(value, datetime):
        return datetime_to_epoch_ms(value)
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        result = {
            key: serialize(item)
            for key, item in value.items()
            if key not in {"_id", "id"}
        }
        if "emp_code" in result and ("punch_in" in result or "status" in result):
            result.setdefault("punch_in", None)
            result.setdefault("punch_out", None)
            result.setdefault("work_hours", None)
            result.setdefault("late_minutes", 0)
            result.setdefault("overtime_minutes", 0)
            result.setdefault("history", [])
            result.setdefault("half_day", False)
        return result
    return value


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        if parsed.isoformat() != value:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return value

    @field_validator("shift_end")
    @classmethod
    def shift_must_differ(cls, value: str, info) -> str:
        if info.data.get("shift_start", "09:30") == value:
            raise ValueError("shift_start must differ from shift_end")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def _month_date_bounds(month: str) -> tuple[str, str]:
    """Return inclusive ISO calendar bounds for a validated YYYY-MM month."""
    try:
        first = date.fromisoformat(f"{month}-01")
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, "month must be a valid YYYY-MM month") from exc
    return first.isoformat(), (next_month - timedelta(days=1)).isoformat()


def _half_up_expression(expression, places: int):
    """Mongo expression for non-negative values using decimal half-up rounding."""
    scale = 10**places
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}},
            scale,
        ]
    }


def _working_day_expression(date_expression):
    weekday = {"$dayOfWeek": {"date": date_expression, "timezone": "+05:30"}}
    return {"$and": [{"$gte": [weekday, 2]}, {"$lte": [weekday, 6]}]}


PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    start_day, end_day = _month_date_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {
            "$set": {
                "_joined_date": {
                    "$dateFromString": {
                        "dateString": "$joined_on",
                        "timezone": "+05:30",
                    }
                },
                "_month_start": {
                    "$dateFromString": {"dateString": start_day, "timezone": "+05:30"}
                },
                "_month_next": {
                    "$dateAdd": {
                        "startDate": {
                            "$dateFromString": {
                                "dateString": start_day,
                                "timezone": "+05:30",
                            }
                        },
                        "unit": "month",
                        "amount": 1,
                        "timezone": "+05:30",
                    }
                },
            }
        },
        {
            "$set": {
                "_calendar_dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$dateDiff": {
                                        "startDate": "$_month_start",
                                        "endDate": "$_month_next",
                                        "unit": "day",
                                        "timezone": "+05:30",
                                    }
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$_month_start",
                                "unit": "day",
                                "amount": "$$offset",
                                "timezone": "+05:30",
                            }
                        },
                    }
                }
            }
        },
        {
            "$set": {
                "_working_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_calendar_dates",
                            "as": "calendar_day",
                            "cond": {
                                "$and": [
                                    {"$gte": ["$$calendar_day", "$_joined_date"]},
                                    _working_day_expression("$$calendar_day"),
                                ]
                            },
                        }
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start_day]},
                                    {"$lte": ["$date", end_day]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_logs",
            }
        },
        {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"_has_log": {"$eq": [{"$type": "$_logs.emp_code"}, "string"]}}},
        {
            "$set": {
                "_log_date": {
                    "$cond": [
                        "$_has_log",
                        {
                            "$dateFromString": {
                                "dateString": "$_logs.date",
                                "timezone": "+05:30",
                            },
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "_working_days": {"$first": "$_working_days"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {"$in": ["$_logs.status", PRESENCE_STATUSES]},
                                    _working_day_expression("$_log_date"),
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$_logs.half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {"$eq": ["$_logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$_logs.late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            "$_has_log",
                            {"$ifNull": ["$_logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$cond": [
                            "$_has_log",
                            {"$ifNull": ["$_logs.overtime_minutes", 0]},
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "month": {"$literal": month},
                "working_days": "$_working_days",
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$_working_days", 0]},
                        _half_up_expression(
                            {
                                "$multiply": [
                                    {"$divide": ["$present_days", "$_working_days"]},
                                    100,
                                ]
                            },
                            4,
                        ),
                        None,
                    ]
                },
            }
        },
    ]
    result = list(db.employees.aggregate(pipeline))
    if not result:
        raise HTTPException(404, "unknown employee")
    return serialize(result[0])


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    start_day, end_day = _month_date_bounds(month)
    employee_match = {"joined_on": {"$lte": end_day}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start_day]},
                                    {"$lte": ["$date", end_day]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_logs",
            }
        },
        {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
        {
            "$set": {
                "_has_log": {"$eq": [{"$type": "$_logs.emp_code"}, "string"]},
                "_log_date": {
                    "$cond": [
                        {"$eq": [{"$type": "$_logs.emp_code"}, "string"]},
                        {
                            "$dateFromString": {
                                "dateString": "$_logs.date",
                                "timezone": "+05:30",
                            }
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "department": {"$first": "$department"},
                "headcount": {"$first": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {"$in": ["$_logs.status", PRESENCE_STATUSES]},
                                    _working_day_expression("$_log_date"),
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$_logs.half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {"$in": ["$_logs.status", PRESENCE_STATUSES]},
                                    {
                                        "$ne": [
                                            {"$ifNull": ["$_logs.work_hours", None]},
                                            None,
                                        ]
                                    },
                                ]
                            },
                            "$_logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {"$in": ["$_logs.status", PRESENCE_STATUSES]},
                                    {
                                        "$ne": [
                                            {"$ifNull": ["$_logs.work_hours", None]},
                                            None,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$_logs.late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            "$_has_log",
                            {"$ifNull": ["$_logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {"$eq": ["$_logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$_has_log",
                                    {"$eq": ["$_logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        _half_up_expression(
                            {"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2
                        ),
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]
    return {"month": month, "items": serialize(list(db.employees.aggregate(pipeline)))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start_day, end_day = _month_date_bounds(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_day, "$lte": end_day},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$_employee.name",
                    "department": "$_employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return {
        "month": month,
        "items": serialize(list(db.attendance_logs.aggregate(pipeline))),
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(422, "to must be on or after from")
    day_count = (to_date - from_date).days + 1
    if day_count > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    start_day = from_date.isoformat()
    pipeline = [
        {"$match": {"department": department}},
        {
            "$set": {
                "_trend_start": {
                    "$dateFromString": {"dateString": start_day, "timezone": "+05:30"}
                },
                "_joined_date": {
                    "$dateFromString": {
                        "dateString": "$joined_on",
                        "timezone": "+05:30",
                    }
                },
            }
        },
        {
            "$set": {
                "_trend_dates": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$_trend_start",
                                "unit": "day",
                                "amount": "$$offset",
                                "timezone": "+05:30",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$_trend_dates"},
        {
            "$set": {
                "_trend_day_key": {
                    "$dateToString": {
                        "date": "$_trend_dates",
                        "format": "%Y-%m-%d",
                        "timezone": "+05:30",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code", "day_key": "$_trend_day_key"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$eq": ["$date", "$$day_key"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_daily_log",
            }
        },
        {"$unwind": {"path": "$_daily_log", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$_trend_dates",
                "headcount": {
                    "$sum": {
                        "$cond": [{"$lte": ["$_joined_date", "$_trend_dates"]}, 1, 0]
                    }
                },
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$_daily_log.status", PRESENCE_STATUSES]},
                            {
                                "$cond": [
                                    {"$ifNull": ["$_daily_log.half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$_daily_log.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$set": {
                "is_working_day": _working_day_expression("$_id"),
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        _half_up_expression(
                            {"$divide": ["$present_count", "$headcount"]}, 4
                        ),
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": ASCENDING},
                "output": {
                    "_moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    },
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "date": "$_id",
                        "format": "%Y-%m-%d",
                        "timezone": "+05:30",
                    }
                },
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_moving_avg_raw", None]},
                        None,
                        _half_up_expression("$_moving_avg_raw", 4),
                    ]
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]
    items = list(db.employees.aggregate(pipeline))
    if not items:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": serialize(items)}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Return MongoDB executionStats for the named endpoint's operation."""
    supported = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in supported:
        raise HTTPException(422, "unsupported endpoint")

    if endpoint == "attendance_list":
        date_from_text = date_from.isoformat() if date_from is not None else None
        date_to_text = date_to.isoformat() if date_to is not None else None
        if date_from_text and date_to_text and date_from_text > date_to_text:
            raise HTTPException(422, "date_from must be on or before date_to")
        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from_text or date_to_text:
            query["date"] = {}
            if date_from_text:
                query["date"]["$gte"] = date_from_text
            if date_to_text:
                query["date"]["$lte"] = date_to_text
        if status is not None:
            query["status"] = status
        projection = {
            "_id": 0,
            "emp_code": 1,
            "date": 1,
            "status": 1,
            "punch_in": 1,
            "punch_out": 1,
            "work_hours": 1,
            "late_minutes": 1,
            "overtime_minutes": 1,
            "half_day": 1,
            "history": 1,
        }
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "projection": projection,
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
        explain = db.command("explain", command, verbosity="executionStats")
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        start_day, end_day = _month_date_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code}},
            {
                "$set": {
                    "_joined_date": {
                        "$dateFromString": {
                            "dateString": "$joined_on",
                            "timezone": "+05:30",
                        }
                    },
                    "_month_start": {
                        "$dateFromString": {
                            "dateString": start_day,
                            "timezone": "+05:30",
                        }
                    },
                    "_month_next": {
                        "$dateAdd": {
                            "startDate": {
                                "$dateFromString": {
                                    "dateString": start_day,
                                    "timezone": "+05:30",
                                }
                            },
                            "unit": "month",
                            "amount": 1,
                            "timezone": "+05:30",
                        }
                    },
                }
            },
            {
                "$set": {
                    "_calendar_dates": {
                        "$map": {
                            "input": {
                                "$range": [
                                    0,
                                    {
                                        "$dateDiff": {
                                            "startDate": "$_month_start",
                                            "endDate": "$_month_next",
                                            "unit": "day",
                                            "timezone": "+05:30",
                                        }
                                    },
                                ]
                            },
                            "as": "offset",
                            "in": {
                                "$dateAdd": {
                                    "startDate": "$_month_start",
                                    "unit": "day",
                                    "amount": "$$offset",
                                    "timezone": "+05:30",
                                }
                            },
                        }
                    }
                }
            },
            {
                "$set": {
                    "_working_days": {
                        "$size": {
                            "$filter": {
                                "input": "$_calendar_dates",
                                "as": "calendar_day",
                                "cond": {
                                    "$and": [
                                        {"$gte": ["$$calendar_day", "$_joined_date"]},
                                        _working_day_expression("$$calendar_day"),
                                    ]
                                },
                            }
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"employee_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$employee_code"]},
                                        {"$gte": ["$date", start_day]},
                                        {"$lte": ["$date", end_day]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "_logs",
                }
            },
            {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
            {"$set": {"_has_log": {"$eq": [{"$type": "$_logs.emp_code"}, "string"]}}},
            {
                "$set": {
                    "_log_date": {
                        "$cond": [
                            "$_has_log",
                            {
                                "$dateFromString": {
                                    "dateString": "$_logs.date",
                                    "timezone": "+05:30",
                                }
                            },
                            None,
                        ]
                    }
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "_working_days": {"$first": "$_working_days"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$_has_log",
                                        {"$in": ["$_logs.status", PRESENCE_STATUSES]},
                                        _working_day_expression("$_log_date"),
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$ifNull": ["$_logs.half_day", False]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$_has_log",
                                        {"$eq": ["$_logs.status", "LEAVE"]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$_has_log",
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$_logs.late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$cond": [
                                "$_has_log",
                                {"$ifNull": ["$_logs.late_minutes", 0]},
                                0,
                            ]
                        }
                    },
                    "total_overtime_minutes": {
                        "$sum": {
                            "$cond": [
                                "$_has_log",
                                {"$ifNull": ["$_logs.overtime_minutes", 0]},
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "month": {"$literal": month},
                    "working_days": "$_working_days",
                    "present_days": 1,
                    "leave_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "total_overtime_minutes": 1,
                    "attendance_pct": {
                        "$cond": [
                            {"$gt": ["$_working_days", 0]},
                            _half_up_expression(
                                {
                                    "$multiply": [
                                        {
                                            "$divide": [
                                                "$present_days",
                                                "$_working_days",
                                            ]
                                        },
                                        100,
                                    ]
                                },
                                4,
                            ),
                            None,
                        ]
                    },
                }
            },
        ]
        collection = "employees"
        explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        start_day, end_day = _month_date_bounds(month)
        employee_match = {"joined_on": {"$lte": end_day}}
        if department is not None:
            employee_match["department"] = department
        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"employee_code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$employee_code"]},
                                        {"$gte": ["$date", start_day]},
                                        {"$lte": ["$date", end_day]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "_logs",
                }
            },
            {"$unwind": {"path": "$_logs", "preserveNullAndEmptyArrays": True}},
            {
                "$set": {
                    "_has_log": {"$eq": [{"$type": "$_logs.emp_code"}, "string"]},
                    "_log_date": {
                        "$cond": [
                            {"$eq": [{"$type": "$_logs.emp_code"}, "string"]},
                            {
                                "$dateFromString": {
                                    "dateString": "$_logs.date",
                                    "timezone": "+05:30",
                                }
                            },
                            None,
                        ]
                    },
                }
            },
            {
                "$group": {
                    "_id": {"department": "$department", "emp_code": "$emp_code"},
                    "department": {"$first": "$department"},
                    "headcount": {"$first": 1},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$_has_log",
                                        {"$in": ["$_logs.status", PRESENCE_STATUSES]},
                                        _working_day_expression("$_log_date"),
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$ifNull": ["$_logs.half_day", False]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "work_hours_sum": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$_has_log",
                                        {"$in": ["$_logs.status", PRESENCE_STATUSES]},
                                        {
                                            "$ne": [
                                                {
                                                    "$ifNull": [
                                                        "$_logs.work_hours",
                                                        None,
                                                    ]
                                                },
                                                None,
                                            ]
                                        },
                                    ]
                                },
                                "$_logs.work_hours",
                                0,
                            ]
                        }
                    },
                    "work_hours_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$_has_log",
                                        {"$in": ["$_logs.status", PRESENCE_STATUSES]},
                                        {
                                            "$ne": [
                                                {
                                                    "$ifNull": [
                                                        "$_logs.work_hours",
                                                        None,
                                                    ]
                                                },
                                                None,
                                            ]
                                        },
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$_has_log",
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$_logs.late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$cond": [
                                "$_has_log",
                                {"$ifNull": ["$_logs.late_minutes", 0]},
                                0,
                            ]
                        }
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$_has_log",
                                        {"$eq": ["$_logs.status", "LEAVE"]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$_has_log",
                                        {"$eq": ["$_logs.status", "ON_DUTY"]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$group": {
                    "_id": "$_id.department",
                    "headcount": {"$sum": "$headcount"},
                    "present_days": {"$sum": "$present_days"},
                    "work_hours_sum": {"$sum": "$work_hours_sum"},
                    "work_hours_count": {"$sum": "$work_hours_count"},
                    "late_count": {"$sum": "$late_count"},
                    "total_late_minutes": {"$sum": "$total_late_minutes"},
                    "leave_count": {"$sum": "$leave_count"},
                    "on_duty_count": {"$sum": "$on_duty_count"},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$work_hours_count", 0]},
                            _half_up_expression(
                                {"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2
                            ),
                            None,
                        ]
                    },
                }
            },
            {"$sort": {"department": ASCENDING}},
        ]
        collection = "employees"
        explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        start_day, end_day = _month_date_bounds(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_day, "$lte": end_day},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "_employee",
                }
            },
            {"$unwind": "$_employee"},
        ]
        if department is not None:
            pipeline.append({"$match": {"_employee.department": department}})
        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": DESCENDING},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
                {
                    "$project": {
                        "_id": 0,
                        "rank": 1,
                        "emp_code": "$_id",
                        "name": "$_employee.name",
                        "department": "$_employee.department",
                        "total_late_minutes": 1,
                        "late_count": 1,
                    }
                },
            ]
        )
        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
    else:  # department_trend
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from, and to are required")
        if to_date < from_date:
            raise HTTPException(422, "to must be on or after from")
        day_count = (to_date - from_date).days + 1
        if day_count > 92:
            raise HTTPException(422, "date range cannot exceed 92 days")
        start_day = from_date.isoformat()
        pipeline = [
            {"$match": {"department": department}},
            {
                "$set": {
                    "_trend_start": {
                        "$dateFromString": {
                            "dateString": start_day,
                            "timezone": "+05:30",
                        }
                    },
                    "_joined_date": {
                        "$dateFromString": {
                            "dateString": "$joined_on",
                            "timezone": "+05:30",
                        }
                    },
                }
            },
            {
                "$set": {
                    "_trend_dates": {
                        "$map": {
                            "input": {"$range": [0, day_count]},
                            "as": "offset",
                            "in": {
                                "$dateAdd": {
                                    "startDate": "$_trend_start",
                                    "unit": "day",
                                    "amount": "$$offset",
                                    "timezone": "+05:30",
                                }
                            },
                        }
                    }
                }
            },
            {"$unwind": "$_trend_dates"},
            {
                "$set": {
                    "_trend_day_key": {
                        "$dateToString": {
                            "date": "$_trend_dates",
                            "format": "%Y-%m-%d",
                            "timezone": "+05:30",
                        }
                    }
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"employee_code": "$emp_code", "day_key": "$_trend_day_key"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$employee_code"]},
                                        {"$eq": ["$date", "$$day_key"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "_daily_log",
                }
            },
            {"$unwind": {"path": "$_daily_log", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": "$_trend_dates",
                    "headcount": {
                        "$sum": {
                            "$cond": [
                                {"$lte": ["$_joined_date", "$_trend_dates"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$_daily_log.status", PRESENCE_STATUSES]},
                                {
                                    "$cond": [
                                        {"$ifNull": ["$_daily_log.half_day", False]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gt": [
                                        {"$ifNull": ["$_daily_log.late_minutes", 0]},
                                        0,
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$set": {"is_working_day": _working_day_expression("$_id")}},
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                            _half_up_expression(
                                {"$divide": ["$present_count", "$headcount"]}, 4
                            ),
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"_id": ASCENDING},
                    "output": {
                        "_moving_avg_raw": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": {
                        "$dateToString": {
                            "date": "$_id",
                            "format": "%Y-%m-%d",
                            "timezone": "+05:30",
                        }
                    },
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": {
                        "$cond": [
                            {"$eq": ["$_moving_avg_raw", None]},
                            None,
                            _half_up_expression("$_moving_avg_raw", 4),
                        ]
                    },
                }
            },
            {"$sort": {"date": ASCENDING}},
        ]
        collection = "employees"
        explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    # Explain output may contain BSON numeric/date values. Relaxed Extended JSON
    # preserves the server document while making it a JSON response body.
    raw_explain = json.loads(bson_dumps(explain))
    return {"endpoint": endpoint, "collection": collection, "explain": raw_explain}


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(503, "MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_seconds(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)
    projection = {
        "_id": 0,
        "emp_code": 1,
        "name": 1,
        "email": 1,
        "department": 1,
        "shift_start": 1,
        "shift_end": 1,
        "joined_on": 1,
        "created_at": 1,
    }
    items = list(
        db.employees.find(query, projection)
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": serialize(items),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(422, "invalid punch-in status")
    try:
        ts = (
            epoch_ms_to_datetime(body.punched_at)
            if body.punched_at is not None
            else truncate_seconds(datetime.now(UTC))
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, emp["shift_start"], day, emp["shift_end"]
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise HTTPException(422, "punched_at must be an epoch-millisecond integer")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    try:
        ts = (
            epoch_ms_to_datetime(body.punched_at)
            if body.punched_at is not None
            else truncate_seconds(datetime.now(UTC))
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no eligible punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    punch_in_time = truncate_seconds(record["punch_in"])
    elapsed = ts - punch_in_time
    if elapsed <= timedelta(0) or elapsed > timedelta(hours=24):
        raise HTTPException(
            422, "punched_at must be after punch_in and within 24 hours"
        )

    work_hours = compute_work_hours(punch_in_time, ts)
    values = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(
            ts, emp["shift_end"], record["date"], emp["shift_start"]
        ),
        "half_day": compute_half_day(work_hours),
    }
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_in": record["punch_in"], "punch_out": None},
        {"$set": values},
    )
    if result.matched_count != 1:
        raise HTTPException(409, "attendance record was updated concurrently")
    record.update(values)
    return serialize(record)


@app.patch("/attendance/{emp_code}/{record_date}")
def regularize_attendance(emp_code: str, record_date: date, body: RegularizeIn):
    day = record_date.isoformat()
    # Explicit nulls are invalid because these OpenAPI fields are optional, not nullable.
    if any(
        name in body.model_fields_set and getattr(body, name) is None
        for name in ("status", "punch_in", "punch_out")
    ):
        raise HTTPException(422, "correction fields cannot be null")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    try:
        new_status = body.status if "status" in supplied else record["status"]
        new_in = (
            epoch_ms_to_datetime(body.punch_in)
            if "punch_in" in supplied
            else record.get("punch_in")
        )
        new_out = (
            epoch_ms_to_datetime(body.punch_out)
            if "punch_out" in supplied
            else record.get("punch_out")
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    new_in = truncate_seconds(new_in) if new_in is not None else None
    new_out = truncate_seconds(new_out) if new_out is not None else None

    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                422, "ABSENT and LEAVE cannot be supplied with punch times"
            )
        new_in = new_out = None
    elif new_in is None:
        raise HTTPException(422, "presence status requires punch_in")

    if (
        new_in is not None
        and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day
    ):
        raise HTTPException(422, "punch_in must remain on the record attendance date")
    if new_out is not None:
        if new_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        duration = new_out - new_in
        if duration <= timedelta(0) or duration > timedelta(hours=24):
            raise HTTPException(
                422, "punch_out must be after punch_in and within 24 hours"
            )

    if new_status in {"ABSENT", "LEAVE"}:
        derived = {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        work_hours = (
            compute_work_hours(new_in, new_out) if new_out is not None else None
        )
        derived = {
            "work_hours": work_hours,
            "late_minutes": compute_late_minutes(
                new_in, emp["shift_start"], day, emp["shift_end"]
            ),
            "overtime_minutes": (
                compute_overtime(new_out, emp["shift_end"], day, emp["shift_start"])
                if new_out
                else 0
            ),
            "half_day": compute_half_day(work_hours),
        }
    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived,
    }
    defaults = {
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    old_values = {key: record.get(key, defaults.get(key)) for key in new_values}
    changes = {
        key: {"from": old_values[key], "to": value}
        for key, value in new_values.items()
        if old_values[key] != value
    }
    if not changes:
        raise HTTPException(422, "correction does not change the record")

    entry = {
        "at": truncate_seconds(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Compare the complete editable/derived snapshot and history. A concurrent
    # update changes at least one predicate, preventing a lost correction/audit entry.
    snapshot_fields = {**old_values}
    snapshot_fields["history"] = record.get("history", [])
    conditions = [{"_id": record["_id"]}]
    for key, value in snapshot_fields.items():
        if key in record:
            conditions.append({key: record[key]})
        else:
            conditions.append({key: {"$exists": False}})
    result = db.attendance_logs.update_one(
        {"$and": conditions},
        {"$set": new_values, "$push": {"history": entry}},
    )
    if result.matched_count != 1:
        raise HTTPException(409, "attendance record was updated concurrently")
    record.update(new_values)
    record.setdefault("history", []).append(entry)
    return serialize(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    date_from = date_from.isoformat() if date_from is not None else None
    date_to = date_to.isoformat() if date_to is not None else None
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    projection = {
        "_id": 0,
        "emp_code": 1,
        "date": 1,
        "status": 1,
        "punch_in": 1,
        "punch_out": 1,
        "work_hours": 1,
        "late_minutes": 1,
        "overtime_minutes": 1,
        "half_day": 1,
        "history": 1,
    }
    docs = list(
        db.attendance_logs.find(query, projection)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": serialize(docs),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# Phase 1 intentionally leaves punch-out, regularization, analytics, and explain
# routes for the subsequent implementation phase.
