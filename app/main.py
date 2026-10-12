"""
Employee Attendance & Analytics API.

Run:   uvicorn app.main:app --port 8000
Env:   MONGO_URI, MONGO_DB  (a local .env is loaded if present; real environment
       variables always win because load_dotenv does not override them).

All application code lives in this one file, per the assignment contract
(openapi.yaml defines the HTTP surface and the business rules R1-R10;
DATA_MODEL.md defines what is stored in MongoDB).

Conventions enforced here:
  * Instants cross the API boundary as integer epoch milliseconds and are stored
    as BSON datetimes (UTC). Values that look like seconds, floats or strings
    are rejected with 422 (strict int fields with min/max bounds).
  * Calendar dates are "YYYY-MM-DD" strings (IST), times of day are "HH:MM".
  * work_hours / late_minutes / overtime_minutes / half_day are computed only
    here (R2-R5) and analytics read the stored values (never recompute).
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ABSENCE_STATUSES = ("ABSENT", "LEAVE")

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
EMP_CODE_PATTERN = r"^EMP\d{4,6}$"
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

StatusLiteral = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceLiteral = Literal["PRESENT", "WFH", "ON_DUTY"]
ExplainTarget = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


# --------------------------------------------------------------------------- #
# Startup: idempotent index creation
# --------------------------------------------------------------------------- #
INDEX_SPECS = (
    ("employees", [("emp_code", ASCENDING)], True),
    ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], False),
    ("employees", [("joined_on", ASCENDING)], False),
    ("attendance_logs", [("emp_code", ASCENDING), ("date", ASCENDING)], True),
    ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], False),
    ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], False),
)


def ensure_indexes() -> None:
    for collection, keys, unique in INDEX_SPECS:
        try:
            db[collection].create_index(keys, unique=unique)
        except PyMongoError as exc:  # degrade loudly but stay up
            print(f"index creation failed on {collection} {keys}: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Time helpers - instants are epoch ms at the boundary, UTC datetimes inside
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # PyMongo returns naive datetimes in UTC by default
        dt = dt.replace(tzinfo=UTC)
    return int(round(dt.timestamp() * 1000))


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_hhmm(hhmm: str) -> tuple[int, int]:
    hour, minute = hhmm.split(":")
    return int(hour), int(minute)


def ist_instant(day: date, hhmm: str) -> datetime:
    hour, minute = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    # zero-padded HH:MM compares correctly as text
    return shift_end <= shift_start


# --------------------------------------------------------------------------- #
# Business rules R1-R5
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of punch-in; overnight shifts belong to the
    previous day when the IST time is earlier than shift_end."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < time(*parse_hhmm(shift_end)):
        day -= timedelta(days=1)
    return day


def shift_end_instant(day: date, shift_start: str, shift_end: str) -> datetime:
    """The instant the shift ends; next calendar day for overnight shifts."""
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    return ist_instant(end_day, shift_end)


def compute_late_minutes(punch_in: datetime, shift_start: str, day: date) -> int:
    """R2: strictly more than 10:00 after shift_start -> floor minutes since
    shift_start (not since the grace period)."""
    elapsed = int((punch_in - ist_instant(day, shift_start)).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_overtime_minutes(punch_out: datetime, day: date, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes from shift_end to punch_out, only when >= 30."""
    elapsed = int((punch_out - shift_end_instant(day, shift_start, shift_end)).total_seconds())
    minutes = elapsed // 60 if elapsed > 0 else 0
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up (never banker's)."""
    seconds = int((punch_out - punch_in).total_seconds())
    exact = Decimal(seconds) / Decimal(3600)
    return float(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: after punch-out, rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.50


def round_half_up(value: Any, places: int) -> float:
    """R8: reported numbers, 2 decimals; rates use 4 - caller picks places."""
    quant = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def derive_fields(emp: dict, day: date, status: str, punch_in, punch_out) -> dict:
    """Recompute all derived fields from the final times (used by punch-out
    and by corrections, so both paths stay identical)."""
    if status not in PRESENCE_STATUSES:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp["shift_start"], day) if punch_in else 0
    if punch_in is None or punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": late,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime_minutes(
            punch_out, day, emp["shift_start"], emp["shift_end"]
        ),
        "half_day": compute_half_day(work_hours),
    }


def working_days(start: date, end: date) -> int:
    """R7: Mon-Fri count between two dates inclusive (0 when start > end)."""
    if start > end:
        return 0
    total = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return first.isoformat(), last.isoformat()


def attendance_pct(present_days: float, working: int) -> Optional[float]:
    if working == 0:
        return None
    exact = Decimal(str(present_days)) * 100 / Decimal(working)
    return float(exact.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Serializers - MongoDB documents -> API JSON
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]) if doc.get("created_at") else None,
    }


def history_entry_out(entry: dict) -> dict:
    changes = {}
    for field, delta in (entry.get("changes") or {}).items():
        before, after = delta.get("from"), delta.get("to")
        if field in ("punch_in", "punch_out"):
            before, after = dt_to_ms(before), dt_to_ms(after)
        changes[field] = {"from": before, "to": after}
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by", ""),
        "reason": entry.get("reason", ""),
        "changes": changes,
    }


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [history_entry_out(e) for e in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
def _epoch_field() -> Any:
    return Field(..., strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=EMP_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_PATTERN)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: date

    @model_validator(mode="after")
    def shifts_must_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: PresenceLiteral = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeRequest(BaseModel):
    status: Optional[StatusLiteral] = None
    punch_in: Optional[int] = Field(None, strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[int] = Field(None, strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> "RegularizeRequest":
        # the schema types are not nullable: sending null means "clear", which
        # is done through status ABSENT/LEAVE instead -> 422
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: Optional[int] = None


class EmployeePage(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int


class HistoryEntryOut(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict[str, dict[str, Any]]


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntryOut]


class AttendancePage(BaseModel):
    items: list[AttendanceRecordOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    department: str
    items: list[TrendItem]


class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: dict


# --------------------------------------------------------------------------- #
# Error helpers
# --------------------------------------------------------------------------- #
def err_422(location: list[str], message: str) -> HTTPException:
    """Manual validation failures keep FastAPI's array-shaped 422 body."""
    return HTTPException(status_code=422, detail=[{"loc": location, "msg": message, "type": "value_error"}])


def parse_date_param(value: Optional[date], name: str) -> Optional[date]:
    return value  # FastAPI/pydantic already rejected malformed dates with 422


# --------------------------------------------------------------------------- #
# Aggregation pipelines (shared by the endpoints and by /admin/explain)
# --------------------------------------------------------------------------- #
def present_days_expression() -> dict:
    """Contribution of one attendance record to present_days (R5 + R7).
    All field lookups are guarded so a record with missing fields counts 0,
    never an error (legacy documents)."""
    day_of = {"$dateFromString": {"dateString": {"$ifNull": ["$date", "1970-01-01"]}}}
    is_weekday = {"$in": [{"$dayOfWeek": day_of}, [2, 3, 4, 5, 6]]}
    is_presence = {"$in": [{"$ifNull": ["$status", ""]}, list(PRESENCE_STATUSES)]}
    is_half_day = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
    return {
        "$cond": [
            {"$and": [is_weekday, is_presence]},
            {"$cond": [is_half_day, 0.5, 1]},
            0,
        ]
    }


def present_count_expression() -> dict:
    """Trend variant: presence-status records that day, weekday or weekend
    (the daily trend counts every record, unlike R7 present_days)."""
    is_presence = {"$in": [{"$ifNull": ["$status", ""]}, list(PRESENCE_STATUSES)]}
    is_half_day = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
    return {
        "$cond": [
            is_presence,
            {"$cond": [is_half_day, 0.5, 1]},
            0,
        ]
    }


def late_expression() -> dict:
    return {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def attendance_list_filter(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[str],
) -> dict:
    flt: dict = {}
    if emp_code:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        bounds: dict = {}
        if date_from:
            bounds["$gte"] = date_from.isoformat()
        if date_to:
            bounds["$lte"] = date_to.isoformat()
        flt["date"] = bounds
    if status:
        flt["status"] = status
    return flt


def attendance_list_sort() -> dict:
    return {"date": DESCENDING, "emp_code": ASCENDING}


def employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list[dict]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": present_days_expression()},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [late_expression(), 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month_start: str, month_end: str, department: Optional[str]) -> list[dict]:
    match: dict = {"joined_on": {"$lte": month_end}}
    if department:
        match["department"] = department
    # Average over records that are present-status and carry a work_hours
    # value: $ifNull(-1) distinguishes "missing/null" from a real 0.0.
    avg_candidate = {
        "$cond": [
            {
                "$and": [
                    {"$in": [{"$ifNull": ["$status", ""]}, list(PRESENCE_STATUSES)]},
                    {"$gte": [{"$ifNull": ["$work_hours", -1]}, 0]},
                ]
            },
            "$work_hours",
            None,
        ]
    }
    return [
        {"$match": match},  # headcount base: R9, includes people with zero logs
        # The month window is pushed into the join itself: MongoDB then probes
        # the unique (emp_code, date) index per employee instead of building a
        # hash-join over a collection scan of attendance_logs (explain reports
        # collectionScans: 0, indexesUsed: [emp_code_1_date_1]).
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": month_start, "$lte": month_end}}}
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        # promote the unwound log to the root so the shared R5-R7 expressions
        # (which read root fields) keep working; employees without logs get {}
        {
            "$replaceRoot": {
                "newRoot": {"$mergeObjects": ["$$ROOT", {"$ifNull": ["$logs", {}]}]}
            }
        },
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": present_days_expression()},
                "avg_work_hours": {"$avg": avg_candidate},
                "late_count": {"$sum": {"$cond": [late_expression(), 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 1,
                "headcount": {"$size": "$emp_codes"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(
    month_start: str, month_end: str, department: Optional[str], limit: int
) -> list[dict]:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [late_expression(), 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # logs whose employee does not exist are ignored (inner join)
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    # Full order BEFORE the window stage; $rank then only sees one sort key so
    # ties share a rank (1, 2, 2, 4). The final sort uses the computed `rank`
    # field, so the planner cannot push it into the window and break ties.
    pipeline += [
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"rank": ASCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def department_trend_pipeline(department: str, start: date, end: date) -> list[dict]:
    from_str, to_str = start.isoformat(), end.isoformat()
    # upper bound of $densify is exclusive -> next day covers `end` inclusive
    upper = datetime(end.year, end.month, end.day) + timedelta(days=1)
    lower = datetime(start.year, start.month, start.day)
    day_str = {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}
    return [
        {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": {"$dateFromString": {"dateString": "$date"}},
                "present_count": {"$sum": present_count_expression()},
                "late_count": {"$sum": {"$cond": [late_expression(), 1, 0]}},
            }
        },
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        {"$sort": {"day": ASCENDING}},
        # gap filling in the database: one row per calendar day, weekends and
        # empty days included; works even when the department has no records
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [{"$match": {"department": department, "joined_on": {"$lte": to_str}}}],
                "as": "dept",
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
                # headcount per day (R9): joined_on <= that day, computed in
                # the database from the department's employee list
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept",
                            "as": "emp",
                            "cond": {"$lte": ["$$emp.joined_on", day_str]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                # raw attendance_rate (division without rounding); Python will apply half-up rounding
                "attendance_rate_raw": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"day": ASCENDING}},
        {
            "$project": {
                "date": day_str,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate_raw": 1,
            }
        },
    ]


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="mongodb unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, response_model=EmployeeOut)
def create_employee(body: EmployeeCreate) -> dict:
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on.isoformat(),
        "created_at": now_utc(),
    }
    try:
        # uniqueness is enforced by the unique index, not by a read-then-write
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc.pop("_id", None)
    return employee_out(doc)


@app.get("/employees", response_model=EmployeePage)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    flt = {"department": department} if department else {}
    total = db.employees.count_documents(flt)
    cursor = (
        db.employees.find(flt, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecordOut)
def punch_in(body: PunchInRequest) -> dict:
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # the unique (emp_code, date) index is what makes concurrent punch-ins
        # race-safe: exactly one insert wins, the rest hit DuplicateKeyError
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    doc.pop("_id", None)
    return record_out(doc)


@app.post("/attendance/punch-out", response_model=AttendanceRecordOut)
def punch_out(body: PunchOutRequest) -> dict:
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # most recent record whose punch_in is at or before punched_at - works
    # across midnight for overnight shifts
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        # no record at or before punched_at: either the employee never punched
        # in (404) or punched_at precedes their latest punch-in (422 below)
        record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", DESCENDING)],
        )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    punch_in = record["punch_in"]
    if punched_at <= punch_in:
        raise HTTPException(status_code=422, detail=[{
            "loc": ["body", "punched_at"], "msg": "punch_out must be after punch_in",
            "type": "value_error",
        }])
    if punched_at - punch_in > timedelta(hours=24):
        raise HTTPException(status_code=422, detail=[{
            "loc": ["body", "punched_at"],
            "msg": "punch_out must be within 24 hours of punch_in",
            "type": "value_error",
        }])

    day = date.fromisoformat(record["date"])
    updates = derive_fields(emp, day, record["status"], punch_in, punched_at)
    updates["punch_out"] = punched_at
    # conditional update: if another request already punched out this record
    # the filter matches nothing -> exactly one 200, the rest 409
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None}, {"$set": updates}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="already punched out")

    saved = db.attendance_logs.find_one({"_id": record["_id"]})
    return record_out(saved)


@app.get("/attendance", response_model=AttendancePage)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[StatusLiteral] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    if date_from and date_to and date_from > date_to:
        raise err_422(["query", "date_from"], "date_from must be on or before date_to")
    flt = attendance_list_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(flt)
    cursor = (
        db.attendance_logs.find(flt, {"_id": 0})
        .sort(attendance_list_sort().items())
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_out(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecordOut)
def regularize_attendance(emp_code: str, date: date, body: RegularizeRequest) -> dict:  # noqa: A002
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    day_str = date.isoformat()
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day_str})
    if record is None:
        raise HTTPException(status_code=404, detail="no attendance record for this date")

    current_status = record["status"]
    current_in = record.get("punch_in")
    current_out = record.get("punch_out")

    new_status = body.status if body.status is not None else current_status
    supplied_in = body.punch_in is not None
    supplied_out = body.punch_out is not None
    new_in = ms_to_dt(body.punch_in) if supplied_in else current_in
    new_out = ms_to_dt(body.punch_out) if supplied_out else current_out

    if new_status in ABSENCE_STATUSES:
        if supplied_in or supplied_out:
            raise err_422(
                ["body", "punch_in" if supplied_in else "punch_out"],
                f"punch times cannot be set when status is {new_status}",
            )
        new_in = new_out = None
    elif new_in is None:
        raise err_422(["body", "status"], "a presence status requires a punch_in")

    if new_in is not None:
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise err_422(["body", "punch_in"], "punch_in must fall on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise err_422(["body", "punch_out"], "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise err_422(["body", "punch_out"], "punch_out must be within 24 hours of punch_in")

    if new_status == current_status and new_in == current_in and new_out == current_out:
        raise err_422(["body"], "request does not change anything")

    derived = derive_fields(emp, date, new_status, new_in, new_out)

    changes: dict[str, dict] = {}

    def note(field: str, before, after) -> None:
        if before != after:
            changes[field] = {"from": before, "to": after}

    note("status", current_status, new_status)
    note("punch_in", current_in, new_in)
    note("punch_out", current_out, new_out)
    note("work_hours", record.get("work_hours"), derived["work_hours"])
    note("late_minutes", record.get("late_minutes") or 0, derived["late_minutes"])
    note("overtime_minutes", record.get("overtime_minutes") or 0, derived["overtime_minutes"])
    note("half_day", bool(record.get("half_day")), derived["half_day"])

    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # optimistic concurrency: the update only applies if nobody else changed
    # status / punch times since we read them, so two edits cannot both append
    # history over the same base state (the loser gets 409)
    snapshot = {
        "emp_code": emp_code,
        "date": day_str,
        "status": current_status,
        "punch_in": current_in,
        "punch_out": current_out,
    }
    updated = db.attendance_logs.find_one_and_update(
        snapshot,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                **derived,
            },
            "$push": {"history": entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was modified concurrently")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly)
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=MONTH_PATTERN),
) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    month_start, month_end = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month_start, month_end)))
    row = rows[0] if rows else {}

    joined = date.fromisoformat(emp["joined_on"])
    year, mon = int(month[:4]), int(month[5:7])
    period_start = date(year, mon, 1)
    period_end = date(year, mon, calendar.monthrange(year, mon)[1])
    total_working = working_days(max(joined, period_start), period_end)

    present = row.get("present_days", 0) or 0
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": total_working,
        "present_days": round_half_up(present, 2),
        "leave_days": int(row.get("leave_days", 0) or 0),
        "late_count": int(row.get("late_count", 0) or 0),
        "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct(present, total_working),
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary)
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
) -> dict:
    month_start, month_end = month_bounds(month)
    rows = list(db.employees.aggregate(department_summary_pipeline(month_start, month_end, department)))
    items = [
        {
            "department": row["_id"],
            "headcount": int(row.get("headcount", 0) or 0),
            "present_days": round_half_up(row.get("present_days", 0) or 0, 2),
            "avg_work_hours": (
                None if row.get("avg_work_hours") is None else round_half_up(row["avg_work_hours"], 2)
            ),
            "late_count": int(row.get("late_count", 0) or 0),
            "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
            "leave_count": int(row.get("leave_count", 0) or 0),
            "on_duty_count": int(row.get("on_duty_count", 0) or 0),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard)
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict:
    month_start, month_end = month_bounds(month)
    rows = list(
        db.attendance_logs.aggregate(
            late_leaderboard_pipeline(month_start, month_end, department, limit)
        )
    )
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", response_model=Trend)
def department_trend(
    department: str,
    from_: date = Query(..., alias="from"),  # noqa: N806
    to: date = Query(...),
) -> dict:
    if to < from_:
        raise err_422(["query", "to"], "to must be on or after from")
    if (to - from_).days + 1 > 92:
        raise err_422(["query", "to"], "range must not exceed 92 days")
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(status_code=404, detail="unknown department")

    rows = list(db.attendance_logs.aggregate(department_trend_pipeline(department, from_, to)))

    # Apply R8 half-up rounding in Python (MongoDB $round uses banker's rounding)
    rounded_rows = []
    attendance_rates = []  # store rounded rates for moving average computation
    for row in rows:
        rate_raw = row.get("attendance_rate_raw")
        if rate_raw is not None:
            rate = round_half_up(rate_raw, 4)
        else:
            rate = None
        attendance_rates.append(rate)

    # Compute moving_avg_7d from rounded attendance_rates (R8: mean of non-null rounded rates, 4dp half-up)
    for i, row in enumerate(rows):
        window_start = max(0, i - 6)
        window_rates = [r for r in attendance_rates[window_start : i + 1] if r is not None]
        if window_rates:
            ma = round_half_up(sum(window_rates) / len(window_rates), 4)
        else:
            ma = None

        rate_raw = row.get("attendance_rate_raw")
        rate = round_half_up(rate_raw, 4) if rate_raw is not None else None

        rounded_rows.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": row["present_count"],
            "late_count": row["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": ma,
        })

    return {"department": department, "items": rounded_rows}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _require(value: Any, name: str) -> None:
    if value is None:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["query", name], "msg": "Field required", "type": "missing"}],
        )


@app.get("/admin/explain/{endpoint}", response_model=ExplainOut)
def explain_endpoint(  # noqa: PLR0913
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[StatusLiteral] = None,
    from_: Optional[date] = Query(None, alias="from"),  # noqa: N806
    to: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    """Runs explain (executionStats) on the exact same query or pipeline the
    named endpoint would run with these parameters."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise err_422(["query", "date_from"], "date_from must be on or before date_to")
        command: dict = {
            "find": "attendance_logs",
            "filter": attendance_list_filter(emp_code, date_from, date_to, status),
            "sort": attendance_list_sort(),
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "projection": {"_id": 0},
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        _require(emp_code, "emp_code")
        _require(month, "month")
        month_start, month_end = month_bounds(month)
        command = {
            "aggregate": "attendance_logs",
            "pipeline": employee_monthly_pipeline(emp_code, month_start, month_end),
            "cursor": {},
        }
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        _require(month, "month")
        month_start, month_end = month_bounds(month)
        command = {
            "aggregate": "employees",
            "pipeline": department_summary_pipeline(month_start, month_end, department),
            "cursor": {},
        }
        collection = "employees"
    elif endpoint == "late_leaderboard":
        _require(month, "month")
        month_start, month_end = month_bounds(month)
        command = {
            "aggregate": "attendance_logs",
            "pipeline": late_leaderboard_pipeline(month_start, month_end, department, limit),
            "cursor": {},
        }
        collection = "attendance_logs"
    else:  # department_trend
        _require(department, "department")
        _require(from_, "from")
        _require(to, "to")
        if to < from_:
            raise err_422(["query", "to"], "to must be on or after from")
        if (to - from_).days + 1 > 92:
            raise err_422(["query", "to"], "range must not exceed 92 days")
        command = {
            "aggregate": "attendance_logs",
            "pipeline": department_trend_pipeline(department, from_, to),
            "cursor": {},
        }
        collection = "attendance_logs"

    try:
        result = db.command("explain", command, verbosity="executionStats")
    except PyMongoError as exc:
        raise HTTPException(status_code=422, detail=[{
            "loc": ["query", "endpoint"], "msg": str(exc), "type": "value_error",
        }])
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(result)),
    }
