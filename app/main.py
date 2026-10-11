"""Employee Attendance & Analytics API starter (Phase 1 foundations)."""
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
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
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date"
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status"
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
        value = value.replace(tzinfo=UTC)  # BSON datetimes are UTC; PyMongo may return them naive.
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise ValueError("timestamp must be an epoch-millisecond integer in the supported range")
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


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str] = None,
                         shift_end: Optional[str] = None) -> int:
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


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str,
                     shift_start: Optional[str] = None) -> int:
    end = _shift_datetime(date_str, shift_end)
    if shift_start is not None and time.fromisoformat(shift_end) <= time.fromisoformat(shift_start):
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
        result = {key: serialize(item) for key, item in value.items() if key not in {"_id", "id"}}
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
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)
    projection = {"_id": 0, "emp_code": 1, "name": 1, "email": 1, "department": 1,
                  "shift_start": 1, "shift_end": 1, "joined_on": 1, "created_at": 1}
    items = list(db.employees.find(query, projection).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {"items": serialize(items), "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(422, "invalid punch-in status")
    try:
        ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else truncate_seconds(datetime.now(UTC))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day, emp["shift_end"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None,
                    date_to: Optional[date] = None, status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
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
    projection = {"_id": 0, "emp_code": 1, "date": 1, "status": 1, "punch_in": 1, "punch_out": 1,
                  "work_hours": 1, "late_minutes": 1, "overtime_minutes": 1, "half_day": 1, "history": 1}
    docs = list(db.attendance_logs.find(query, projection).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": serialize(docs), "total": total, "page": page, "page_size": page_size}


# Phase 1 intentionally leaves punch-out, regularization, analytics, and explain
# routes for the subsequent implementation phase.
