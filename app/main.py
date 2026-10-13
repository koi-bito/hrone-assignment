import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, timedelta, timezone

date = dt_date
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
from typing import Annotated, Any, Literal, Optional

import bson.json_util
from bson import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# =====================================================================
# Constants & Database Connection
# =====================================================================

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

# =====================================================================
# Lifespan & App Setup
# =====================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # employees indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    # attendance_logs indexes
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)]
    )

    yield

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

# =====================================================================
# Half-Up Rounding Helper (R4, R8)
# =====================================================================

def round_half_up(value: float | Decimal | int | Decimal128, decimals: int) -> Decimal:
    """Half-up rounding to `decimals` places. Used for all reported numbers (R8)."""
    quantizer = Decimal(10) ** -decimals
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, Decimal128):
        d = value.to_decimal()
    else:
        d = Decimal(str(value))
    return d.quantize(quantizer, rounding=ROUND_HALF_UP)

# =====================================================================
# Datetime Helpers
# =====================================================================

def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime, truncated to whole seconds (R1)."""
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """UTC-aware datetime -> epoch milliseconds. Treats naive as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def to_ist(dt: datetime) -> datetime:
    """Convert any aware datetime to IST."""
    return dt.astimezone(IST)


def now_utc() -> datetime:
    """Current time as UTC-aware datetime, second-truncated."""
    ts = int(datetime.now(timezone.utc).timestamp())
    return datetime.fromtimestamp(ts, tz=timezone.utc)

# =====================================================================
# Attendance Date & Shift Bounds (R1 - Overnight Aware)
# =====================================================================

def attendance_date(punch_in_dt: datetime, emp: dict) -> str:
    """Compute the IST attendance date string for a punch-in per R1."""
    ist_dt = to_ist(punch_in_dt)
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))

    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight:
        if (ist_dt.hour, ist_dt.minute, ist_dt.second) < (eh, em, 0):
            return (ist_dt - timedelta(days=1)).date().isoformat()

    return ist_dt.date().isoformat()


def shift_bounds(date_str: str, emp: dict) -> tuple[datetime, datetime]:
    """Returns (shift_start_utc, shift_end_utc) for the given attendance date."""
    d = date.fromisoformat(date_str)
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))

    shift_start_ist = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    end_d = d + timedelta(days=1) if is_overnight else d
    shift_end_ist = datetime(end_d.year, end_d.month, end_d.day, eh, em, tzinfo=IST)

    return (
        shift_start_ist.astimezone(timezone.utc),
        shift_end_ist.astimezone(timezone.utc),
    )

# =====================================================================
# Derived Field Calculators (R2 - R5)
# =====================================================================

def calc_late_minutes(punch_in_dt: datetime, shift_start_dt: datetime) -> int:
    """R2: Grace 10 min. Late only if strictly > 600 seconds after shift_start."""
    diff_seconds = int((punch_in_dt - shift_start_dt).total_seconds())
    if diff_seconds > 600:
        return diff_seconds // 60
    return 0


def calc_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (out-in) seconds / 3600, rounded half-up to 2 decimals."""
    seconds = int((punch_out_dt - punch_in_dt).total_seconds())
    raw = Decimal(seconds) / Decimal(3600)
    return float(round_half_up(raw, 2))


def calc_overtime_minutes(punch_out_dt: datetime, shift_end_dt: datetime) -> int:
    """R3: Floor minutes from shift_end to punch_out. Only if >= 30, else 0."""
    diff_seconds = int((punch_out_dt - shift_end_dt).total_seconds())
    minutes = diff_seconds // 60
    return minutes if minutes >= 30 else 0


def calc_half_day(work_hours: float) -> bool:
    """R5: True when rounded work_hours < 4.50."""
    return round_half_up(work_hours, 2) < Decimal("4.50")

# =====================================================================
# Calendar Functions (R7)
# =====================================================================

def count_working_days(start: date, end: date) -> int:
    """Count Mon-Fri days from start to end inclusive. No holiday calendar (R7)."""
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon=0, Fri=4
            count += 1
        current += timedelta(days=1)
    return count

# =====================================================================
# Serialization Helpers
# =====================================================================

def _dt_or_passthrough(val: Any) -> Any:
    if isinstance(val, datetime):
        return dt_to_ms(val)
    return val


def serialize_record(doc: dict) -> dict:
    """Prepare attendance_logs doc for API response."""
    res = dict(doc)
    res.pop("_id", None)

    res["punch_in"] = _dt_or_passthrough(res.get("punch_in"))
    res["punch_out"] = _dt_or_passthrough(res.get("punch_out"))

    res.setdefault("late_minutes", 0)
    res.setdefault("overtime_minutes", 0)
    res.setdefault("half_day", False)
    res.setdefault("history", [])

    formatted_history = []
    for entry in res.get("history", []):
        entry_copy = dict(entry)
        entry_copy["at"] = _dt_or_passthrough(entry_copy.get("at"))
        changes = dict(entry_copy.get("changes", {}))
        for field in ("punch_in", "punch_out"):
            if field in changes:
                change_item = dict(changes[field])
                change_item["from"] = _dt_or_passthrough(change_item.get("from"))
                change_item["to"] = _dt_or_passthrough(change_item.get("to"))
                changes[field] = change_item
        entry_copy["changes"] = changes
        formatted_history.append(entry_copy)

    res["history"] = formatted_history
    return res


def serialize_employee(doc: dict) -> dict:
    """Prepare employees doc for API response."""
    res = dict(doc)
    res.pop("_id", None)
    res["created_at"] = dt_to_ms(res["created_at"])
    return res

# =====================================================================
# Pydantic Schemas & Validation
# =====================================================================

EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
OptionalEpochMs = Annotated[
    Optional[int],
    Field(default=None, strict=True, ge=100_000_000_000, le=4_102_444_800_000),
]

StatusEnum = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

_HH_MM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_EMP_RE = re.compile(r"^EMP\d{4,6}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmployeeIn(BaseModel):
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    def v_emp_code(cls, v: str):
        if not _EMP_RE.match(v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("email")
    def v_email(cls, v: str):
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("shift_start", "shift_end")
    def v_shift(cls, v: str):
        if not _HH_MM_RE.match(v):
            raise ValueError("must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    def v_joined(cls, v: str):
        if not _DATE_RE.match(v):
            raise ValueError("must be YYYY-MM-DD")
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("not a valid calendar date")
        return v

    @model_validator(mode="after")
    def v_shift_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: OptionalEpochMs = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: OptionalEpochMs = None


class RegularizeIn(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: OptionalEpochMs = None
    punch_out: OptionalEpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

# =====================================================================
# Shared Query & Pipeline Builders (for Endpoints and Explain)
# =====================================================================

def _attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    return q


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    year, mon = int(month[:4]), int(month[5:])
    month_start = f"{year:04d}-{mon:02d}-01"
    month_end = f"{year:04d}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}"

    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def _department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict]:
    year, mon = int(month[:4]), int(month[5:])
    month_start = f"{year:04d}-{mon:02d}-01"
    month_end = f"{year:04d}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}"

    dept_filter = {"department": department} if department else {}

    return [
        {"$match": {**dept_filter, "joined_on": {"$lte": month_end}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$ec"]},
                    {"$gte": ["$date", month_start]},
                    {"$lte": ["$date", month_end]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emp_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}}}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "wh_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                {"$ifNull": ["$logs.work_hours", 0]}, 0,
            ]}},
            "wh_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                1, 0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def _late_leaderboard_pipeline(month: str, department: Optional[str] = None, limit: int = 10) -> list[dict]:
    year, mon = int(month[:4]), int(month[5:])
    month_start = f"{year:04d}-{mon:02d}-01"
    month_end = f"{year:04d}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}"

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    return pipeline


def _department_trend_pipeline(department: str, from_str: str, to_str: str) -> list[dict]:
    from_d = date.fromisoformat(from_str)
    to_d = date.fromisoformat(to_str)
    num_days = (to_d - from_d).days + 1
    from_iso = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)

    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$addFields": {"offsets": {"$range": [0, num_days]}}},
        {"$unwind": "$offsets"},
        {"$addFields": {
            "day_utc": {"$dateAdd": {"startDate": from_iso, "unit": "day", "amount": "$offsets"}},
        }},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day_utc", "timezone": "+05:30"}},
            "is_working_day": {"$in": [{"$dayOfWeek": {"date": "$day_utc", "timezone": "+05:30"}}, [2, 3, 4, 5, 6]]},
        }},
        {"$addFields": {
            "active_emps": {"$filter": {
                "input": "$employees",
                "cond": {"$lte": ["$$this.joined_on", "$date"]},
            }},
        }},
        {"$addFields": {
            "headcount": {"$size": "$active_emps"},
            "active_codes": {"$map": {"input": "$active_emps", "in": "$$this.emp_code"}},
            "all_codes": {"$map": {"input": "$employees", "in": "$$this.emp_code"}},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date", "codes": "$all_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$date", "$$day"]},
                    {"$in": ["$emp_code", "$$codes"]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$addFields": {
            "present_count": {"$reduce": {
                "input": "$logs",
                "initialValue": 0,
                "in": {"$add": ["$$value", {"$cond": [
                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1]},
                    0,
                ]}]},
            }},
            "late_count": {"$size": {"$filter": {
                "input": "$logs",
                "cond": {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]},
            }}},
        }},
        {"$addFields": {
            "attendance_rate_raw": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d_raw": {
                    "$avg": "$attendance_rate_raw",
                    "window": {"documents": [-6, 0]},
                },
            },
        }},
        {"$sort": {"date": 1}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate_raw": 1,
            "moving_avg_7d_raw": 1,
        }},
    ]

# =====================================================================
# API Endpoints
# =====================================================================

@app.get("/health", tags=["system"])
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    punch_in_dt = (
        ms_to_dt(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )
    date_str = attendance_date(punch_in_dt, emp)
    shift_start_dt, _ = shift_bounds(date_str, emp)
    late = calc_late_minutes(punch_in_dt, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_record(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    punched_at_dt = (
        ms_to_dt(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "no corresponding punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_dt = record["punch_in"]
    if punched_at_dt <= punch_in_dt:
        raise HTTPException(
            422,
            detail=[{"msg": "punch_out must be strictly greater than punch_in", "type": "value_error"}],
        )
    if punched_at_dt > punch_in_dt + timedelta(hours=24):
        raise HTTPException(
            422,
            detail=[{"msg": "punch_out cannot be more than 24 hours after punch_in", "type": "value_error"}],
        )

    wh = calc_work_hours(punch_in_dt, punched_at_dt)
    _, shift_end_dt = shift_bounds(record["date"], emp)
    ot = calc_overtime_minutes(punched_at_dt, shift_end_dt)
    hd = calc_half_day(wh)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None, "punch_in": punch_in_dt},
        {
            "$set": {
                "punch_out": punched_at_dt,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out or record modified concurrently")

    return serialize_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from:
        if not _DATE_RE.match(date_from):
            raise HTTPException(422, detail=[{"msg": "invalid date_from", "type": "value_error"}])
        try:
            dt_date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(422, detail=[{"msg": "invalid date_from", "type": "value_error"}])
    if date_to:
        if not _DATE_RE.match(date_to):
            raise HTTPException(422, detail=[{"msg": "invalid date_to", "type": "value_error"}])
        try:
            dt_date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(422, detail=[{"msg": "invalid date_to", "type": "value_error"}])
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail=[{"msg": "date_from must be <= date_to", "type": "value_error"}])

    q = _attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    items = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    if not _DATE_RE.match(date):
        raise HTTPException(422, detail=[{"msg": "invalid date format", "type": "value_error"}])
    try:
        dt_date.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, detail=[{"msg": "invalid date format", "type": "value_error"}])

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    new_status = body.status if body.status is not None else record["status"]
    new_punch_in = (
        body.punch_in
        if body.punch_in is not None
        else (dt_to_ms(record["punch_in"]) if record.get("punch_in") else None)
    )
    new_punch_out = (
        body.punch_out
        if body.punch_out is not None
        else (dt_to_ms(record["punch_out"]) if record.get("punch_out") else None)
    )

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                detail=[{"msg": "ABSENT or LEAVE status cannot have punch times", "type": "value_error"}],
            )
        final_punch_in_dt = None
        final_punch_out_dt = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        if new_punch_in is None:
            raise HTTPException(
                422,
                detail=[{"msg": "presence status requires punch_in", "type": "value_error"}],
            )
        punch_in_dt = ms_to_dt(new_punch_in)
        if attendance_date(punch_in_dt, emp) != date:
            raise HTTPException(
                422,
                detail=[{"msg": "punch_in must stay on the record's attendance date", "type": "value_error"}],
            )

        punch_out_dt = ms_to_dt(new_punch_out) if new_punch_out is not None else None
        if punch_out_dt is not None:
            if punch_out_dt <= punch_in_dt:
                raise HTTPException(
                    422,
                    detail=[{"msg": "punch_out must be strictly greater than punch_in", "type": "value_error"}],
                )
            if punch_out_dt > punch_in_dt + timedelta(hours=24):
                raise HTTPException(
                    422,
                    detail=[{"msg": "punch_out cannot exceed 24 hours after punch_in", "type": "value_error"}],
                )

            shift_start_dt, shift_end_dt = shift_bounds(date, emp)
            final_late_minutes = calc_late_minutes(punch_in_dt, shift_start_dt)
            final_work_hours = calc_work_hours(punch_in_dt, punch_out_dt)
            final_overtime_minutes = calc_overtime_minutes(punch_out_dt, shift_end_dt)
            final_half_day = calc_half_day(final_work_hours)
        else:
            shift_start_dt, _ = shift_bounds(date, emp)
            final_late_minutes = calc_late_minutes(punch_in_dt, shift_start_dt)
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_punch_in_dt = punch_in_dt
        final_punch_out_dt = punch_out_dt

    old_status = record.get("status")
    old_punch_in_dt = record.get("punch_in")
    old_punch_out_dt = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if final_punch_in_dt != old_punch_in_dt:
        changes["punch_in"] = {"from": old_punch_in_dt, "to": final_punch_in_dt}
    if final_punch_out_dt != old_punch_out_dt:
        changes["punch_out"] = {"from": old_punch_out_dt, "to": final_punch_out_dt}
    wh_changed = False
    if final_work_hours is None and old_work_hours is None:
        wh_changed = False
    elif final_work_hours is None or old_work_hours is None:
        wh_changed = True
    elif round_half_up(final_work_hours, 2) != round_half_up(old_work_hours, 2):
        wh_changed = True

    if wh_changed:
        changes["work_hours"] = {"from": old_work_hours, "to": final_work_hours}
    if final_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": final_late_minutes}
    if final_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": final_overtime_minutes}
    if final_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": final_half_day}

    if not changes:
        raise HTTPException(
            422,
            detail=[{"msg": "no changes detected in regularize request", "type": "value_error"}],
        )

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    set_fields = {
        "status": new_status,
        "punch_in": final_punch_in_dt,
        "punch_out": final_punch_out_dt,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
    }

    for attempt in range(3):
        old_hist_len = len(record.get("history") or [])
        updated = db.attendance_logs.find_one_and_update(
            {
                "_id": record["_id"],
                "status": record["status"],
                "punch_in": record.get("punch_in"),
                "punch_out": record.get("punch_out"),
                "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, old_hist_len]},
            },
            {"$set": set_fields, "$push": {"history": history_entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return serialize_record(updated)

        record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not record:
            raise HTTPException(404, "attendance record not found")

    raise HTTPException(409, "concurrent modification conflict")


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    pipeline = _employee_monthly_pipeline(emp_code, month)
    rows = list(db.attendance_logs.aggregate(pipeline))
    row = rows[0] if rows else {}

    year, mon = int(month[:4]), int(month[5:])
    month_start_d = date(year, mon, 1)
    month_end_d = date(year, mon, calendar.monthrange(year, mon)[1])
    joined_d = date.fromisoformat(emp["joined_on"])
    effective_start = max(month_start_d, joined_d)
    working_days = count_working_days(effective_start, month_end_d)

    present_days = float(round_half_up(row.get("present_days", 0), 2))
    attendance_pct = (
        float(round_half_up((Decimal(str(present_days)) / Decimal(working_days)) * 100, 2))
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = _department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        headcount = len(r.get("emp_codes", []))
        if headcount == 0:
            continue
        wh_count = r.get("wh_count", 0)
        wh_sum = r.get("wh_sum", 0)
        avg_wh = (
            float(round_half_up(Decimal(str(wh_sum)) / Decimal(wh_count), 2))
            if wh_count > 0
            else None
        )
        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": float(round_half_up(r.get("present_days", 0), 2)),
            "avg_work_hours": avg_wh,
            "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0),
            "leave_count": r.get("leave_count", 0),
            "on_duty_count": r.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = _late_leaderboard_pipeline(month, department, limit)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_: str = Query(alias="from"),
    to: str = Query(alias="to"),
):
    if not _DATE_RE.match(from_) or not _DATE_RE.match(to):
        raise HTTPException(422, detail=[{"msg": "dates must be YYYY-MM-DD", "type": "value_error"}])
    try:
        from_d = dt_date.fromisoformat(from_)
        to_d = dt_date.fromisoformat(to)
    except ValueError:
        raise HTTPException(422, detail=[{"msg": "invalid calendar date", "type": "value_error"}])
    if to_d < from_d:
        raise HTTPException(422, detail=[{"msg": "to must be >= from", "type": "value_error"}])
    num_days = (to_d - from_d).days + 1
    if num_days > 92:
        raise HTTPException(422, detail=[{"msg": "range exceeds 92 days", "type": "value_error"}])

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")

    pipeline = _department_trend_pipeline(department, from_, to)
    items = []
    for row in db.employees.aggregate(pipeline):
        row["attendance_rate"] = (
            float(round_half_up(row["attendance_rate_raw"], 4))
            if row.get("attendance_rate_raw") is not None
            else None
        )
        row["moving_avg_7d"] = (
            float(round_half_up(row["moving_avg_7d_raw"], 4))
            if row.get("moving_avg_7d_raw") is not None
            else None
        )
        row["present_count"] = float(round_half_up(row["present_count"], 2))
        row.pop("attendance_rate_raw", None)
        row.pop("moving_avg_7d_raw", None)
        items.append(row)

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusEnum] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from:
            if not _DATE_RE.match(date_from):
                raise HTTPException(422, detail=[{"msg": "invalid date_from", "type": "value_error"}])
            try:
                dt_date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(422, detail=[{"msg": "invalid date_from", "type": "value_error"}])
        if date_to:
            if not _DATE_RE.match(date_to):
                raise HTTPException(422, detail=[{"msg": "invalid date_to", "type": "value_error"}])
            try:
                dt_date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(422, detail=[{"msg": "invalid date_to", "type": "value_error"}])
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, detail=[{"msg": "date_from must be <= date_to", "type": "value_error"}])
        q = _attendance_list_query(emp_code, date_from, date_to, status)
        raw = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        coll = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail=[{"msg": "emp_code and month are required", "type": "value_error"}])
        pipeline = _employee_monthly_pipeline(emp_code, month)
        raw = db.command({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        coll = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail=[{"msg": "month is required", "type": "value_error"}])
        pipeline = _department_summary_pipeline(month, department)
        raw = db.command({
            "explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        coll = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail=[{"msg": "month is required", "type": "value_error"}])
        pipeline = _late_leaderboard_pipeline(month, department, limit)
        raw = db.command({
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        coll = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, detail=[{"msg": "department, from, and to are required", "type": "value_error"}])
        if not _DATE_RE.match(from_) or not _DATE_RE.match(to):
            raise HTTPException(422, detail=[{"msg": "dates must be YYYY-MM-DD", "type": "value_error"}])
        try:
            from_d = dt_date.fromisoformat(from_)
            to_d = dt_date.fromisoformat(to)
        except ValueError:
            raise HTTPException(422, detail=[{"msg": "invalid calendar date", "type": "value_error"}])
        if to_d < from_d:
            raise HTTPException(422, detail=[{"msg": "to must be >= from", "type": "value_error"}])
        if (to_d - from_d).days + 1 > 92:
            raise HTTPException(422, detail=[{"msg": "range exceeds 92 days", "type": "value_error"}])
        pipeline = _department_trend_pipeline(department, from_, to)
        raw = db.command({
            "explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })
        coll = "employees"

    return {
        "endpoint": endpoint,
        "collection": coll,
        "explain": json.loads(bson.json_util.dumps(raw)),
    }
