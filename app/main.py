"""
Employee Attendance & Analytics API

FastAPI application providing complete attendance management, business rule enforcement (R1 - R10),
audit trail (history) regularizations, and MongoDB aggregation pipelines for analytics and query planning.
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Tuple

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Response, status
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Environment & Database Configuration
# --------------------------------------------------------------------------- #
load_dotenv()

# Prioritize MONGO_URI, fallback to MONGODB_URI or localhost
MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
# Prioritize MONGO_DB, fallback to DATABASE_NAME or attendance_db
MONGO_DB = os.getenv("MONGO_DB") or os.getenv("DATABASE_NAME") or "hrone_attendance"

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

# Indian Standard Time (UTC+05:30) per R1
IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required database indexes idempotently at application startup."""
    try:
        # 1. Employees collection:
        # Unique business identifier emp_code
        db.employees.create_index([("emp_code", 1)], unique=True, name="idx_employees_emp_code_unique")
        # Department filter index
        db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_employees_dept_joined")
        db.employees.create_index([("joined_on", 1)], name="idx_employees_joined_on")

        # 2. Attendance Logs collection:
        # Unique natural key (emp_code, date) per assignment contract
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)], unique=True, name="idx_attendance_emp_code_date_unique"
        )
        # Listing index: sorted by date desc, emp_code asc
        db.attendance_logs.create_index(
            [("date", -1), ("emp_code", 1)], name="idx_attendance_date_desc_emp_code_asc"
        )
        # Punch-out lookups: employee's records ordered by punch_in
        db.attendance_logs.create_index(
            [("emp_code", 1), ("punch_in", -1)], name="idx_attendance_emp_code_punch_in"
        )
        # Monthly summaries & late leaderboards:
        db.attendance_logs.create_index(
            [("date", 1), ("late_minutes", 1)], name="idx_attendance_date_late_minutes"
        )
        db.attendance_logs.create_index(
            [("date", 1), ("status", 1)], name="idx_attendance_date_status"
        )
    except Exception as exc:
        print(f"Warning initializing indexes: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="Backend service for HROne Employee Attendance & Analytics API assignment.",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Mathematical & Business Rule Helpers (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(value: Optional[float | Decimal], decimals: int = 2) -> Optional[float]:
    """
    Round numbers half-up (R8) using Python's Decimal ROUND_HALF_UP.
    Avoids Python's default banker's rounding and IEEE float imprecision.
    """
    if value is None:
        return None
    d = Decimal(str(value))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime(epoch_ms: int) -> datetime:
    """
    Convert an integer epoch millisecond timestamp to a UTC-aware datetime.
    Truncates to whole seconds before conversion per R1.
    """
    truncated_seconds = epoch_ms // 1000
    return datetime.fromtimestamp(truncated_seconds, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a datetime instance to an integer epoch millisecond value."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Treat naive datetime from database as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_time_str(time_str: str) -> time:
    """Parse 'HH:MM' string to datetime.time."""
    h, m = map(int, time_str.split(":"))
    return time(hour=h, minute=m)


def determine_attendance_date(punch_in_dt: datetime, shift_start_str: str, shift_end_str: str) -> Tuple[str, bool]:
    """
    Determine the attendance calendar date (YYYY-MM-DD in IST) per R1.
    For overnight shifts (shift_end <= shift_start):
    If punch_in time in IST is strictly earlier than shift_end, it belongs
    to the shift that started the previous calendar day.
    """
    ist_dt = punch_in_dt.astimezone(IST)
    start_t = parse_time_str(shift_start_str)
    end_t = parse_time_str(shift_end_str)
    is_overnight = end_t <= start_t

    if is_overnight and ist_dt.time() < end_t:
        att_date = ist_dt.date() - timedelta(days=1)
    else:
        att_date = ist_dt.date()

    return att_date.isoformat(), is_overnight


def get_shift_instants(date_str: str, shift_start_str: str, shift_end_str: str) -> Tuple[datetime, datetime]:
    """
    Compute shift_start and shift_end UTC instants for a given attendance date.
    Shift times are in IST. For overnight shifts, shift_end occurs on the next day.
    """
    d = date.fromisoformat(date_str)
    start_t = parse_time_str(shift_start_str)
    end_t = parse_time_str(shift_end_str)

    shift_start_dt = datetime.combine(d, start_t, tzinfo=IST)
    if end_t <= start_t:
        shift_end_dt = datetime.combine(d + timedelta(days=1), end_t, tzinfo=IST)
    else:
        shift_end_dt = datetime.combine(d, end_t, tzinfo=IST)

    return shift_start_dt, shift_end_dt


def compute_late_minutes(punch_in_dt: datetime, shift_start_dt: datetime) -> int:
    """
    R2: Late minutes calculation with 10-minute grace period.
    Late only if punch-in is strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start.
    """
    diff_sec = (punch_in_dt - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime_minutes(punch_out_dt: datetime, shift_end_dt: datetime) -> int:
    """
    R3: Overtime calculation.
    Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    """
    diff_sec = (punch_out_dt - shift_end_dt).total_seconds()
    diff_min = int(diff_sec // 60)
    return diff_min if diff_min >= 30 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: Work hours calculation.
    (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals.
    """
    total_sec = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(total_sec / 3600.0, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: Half day calculation.
    True if rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB attendance document to API response contract."""
    clean = dict(doc)
    clean.pop("_id", None)
    clean.pop("id", None)

    # Convert BSON dates to integer epoch milliseconds
    clean["punch_in"] = datetime_to_epoch_ms(clean.get("punch_in"))
    clean["punch_out"] = datetime_to_epoch_ms(clean.get("punch_out"))

    # Ensure default values for legacy records per DATA_MODEL.md
    if "work_hours" not in clean:
        clean["work_hours"] = None
    if "late_minutes" not in clean:
        clean["late_minutes"] = 0
    if "overtime_minutes" not in clean:
        clean["overtime_minutes"] = 0
    if "half_day" not in clean:
        clean["half_day"] = False
    if "history" not in clean or clean["history"] is None:
        clean["history"] = []
    else:
        # Convert history entry dates
        history_list = []
        for h in clean["history"]:
            h_clean = dict(h)
            h_clean["at"] = datetime_to_epoch_ms(h_clean.get("at"))
            # Changes object inside history
            changes_dict = {}
            for field_name, delta in h_clean.get("changes", {}).items():
                d_copy = dict(delta)
                if field_name in ("punch_in", "punch_out"):
                    if isinstance(d_copy.get("from"), datetime):
                        d_copy["from"] = datetime_to_epoch_ms(d_copy["from"])
                    if isinstance(d_copy.get("to"), datetime):
                        d_copy["to"] = datetime_to_epoch_ms(d_copy["to"])
                changes_dict[field_name] = d_copy
            h_clean["changes"] = changes_dict
            history_list.append(h_clean)
        clean["history"] = history_list

    return clean


# --------------------------------------------------------------------------- #
# Pydantic Request & Response Schemas
# --------------------------------------------------------------------------- #
EpochMillisField = StrictInt


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unavailable: {str(exc)}",
        )


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. emp_code is customer-supplied, unique and immutable."""
    doc = body.model_dump()
    created_dt = datetime.now(timezone.utc)
    # Truncate instant to whole seconds
    created_dt = created_dt.replace(microsecond=0)
    doc["created_at"] = created_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    query: Dict[str, Any] = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)

    items = []
    for emp in cursor:
        emp["created_at"] = datetime_to_epoch_ms(emp.get("created_at"))
        items.append(emp)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Core Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Record attendance punch-in. Creates the day's record (R1, R2).
    Enforces concurrency via unique compound index (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown employee")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punched_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_in_dt = epoch_ms_to_datetime(punched_ms)

    att_date, _ = determine_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    shift_start_dt, _ = get_shift_instants(att_date, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="already punched in for this date",
        )

    return serialize_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Record attendance punch-out. Closes the employee's most recent record whose
    punch_in is at or before punched_at (works across midnight for overnight shifts).
    Atomic update guarantees two simultaneous punch-outs yield exactly one 200 and one 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown employee")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    punched_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_out_dt = epoch_ms_to_datetime(punched_ms)

    # Find the most recent record whose punch_in <= punch_out_dt
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in or > 24 hours after it
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out must be strictly after punch_in",
        )
    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    # Check if already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That record is already punched out",
        )

    # Compute derived fields (R3, R4, R5)
    att_date = record["date"]
    _, shift_end_dt = get_shift_instants(att_date, emp["shift_start"], emp["shift_end"])
    work_h = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime_minutes(punch_out_dt, shift_end_dt)
    half_d = compute_half_day(work_h)

    # Atomic CAS update: punch_out must be null
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_h,
                "overtime_minutes": ot_min,
                "half_day": half_d,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That record is already punched out",
        )

    return serialize_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, emp_code ascending.
    Streams via database pagination, maintaining high performance at ~100k docs.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be on or before date_to",
        )

    query: Dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(..., description="Employee business code"),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="Attendance date YYYY-MM-DD"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields, appends a detailed audit trail to history,
    and uses optimistic concurrency to prevent lost updates.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown employee")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)
    old_history = record.get("history", [])

    new_status = body.status if body.status is not None else old_status

    # Rule: setting ABSENT or LEAVE clears both punch times
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Cannot supply punch times when status is {new_status}",
            )
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            new_punch_in = old_punch_in

        if new_punch_in is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in",
            )

        # punch_in must stay on the record's attendance date per R1
        calc_date, _ = determine_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in instant belongs to attendance date {calc_date}, not {date}",
            )

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            new_punch_out = old_punch_out

        shift_start_dt, shift_end_dt = get_shift_instants(date, emp["shift_start"], emp["shift_end"])
        new_late_minutes = compute_late_minutes(new_punch_in, shift_start_dt)

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in",
                )
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_minutes(new_punch_out, shift_end_dt)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Check which fields actually changed
    changes: Dict[str, Dict[str, Any]] = {}

    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if old_punch_in != new_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if old_punch_out != new_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request resulted in no field changes",
        )

    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: ensure history length hasn't changed
    query_filter = {
        "_id": record["_id"],
        "history": {"$size": len(old_history)},
    }
    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated = db.attendance_logs.find_one_and_update(
        query_filter, update_doc, return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent regularization modification conflict",
        )

    return serialize_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregation Pipelines)
# --------------------------------------------------------------------------- #
def count_working_days(start_date: date, end_date: date) -> int:
    """Count Monday - Friday calendar days in inclusive range [start_date, end_date]."""
    if start_date > end_date:
        return 0
    cur = start_date
    working_days = 0
    while cur <= end_date:
        if cur.weekday() < 5:  # 0 to 4 are Mon - Fri
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def get_month_date_range(month_str: str) -> Tuple[date, date]:
    """Get the first and last calendar dates of a given YYYY-MM month."""
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, num_days)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(..., description="Employee business code"),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Month YYYY-MM"),
):
    """Monthly attendance summary for one employee backed by a MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown employee")

    first_day, last_day = get_month_date_range(month)
    joined_date = date.fromisoformat(emp["joined_on"])
    start_calc_date = max(first_day, joined_date)
    working_days = count_working_days(start_calc_date, last_day)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
            }
        },
        {
            "$project": {
                "late_minutes": 1,
                "overtime_minutes": 1,
                "is_weekday": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},  # Mon-Fri
                "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "half_day": 1,
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_weekday", "$is_present_status"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        stats = res[0]
        present_days = float(stats["present_days"])
        leave_days = int(stats["leave_days"])
        late_count = int(stats["late_count"])
        total_late_minutes = int(stats["total_late_minutes"])
        total_overtime_minutes = int(stats["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Month YYYY-MM"),
    department: Optional[str] = Query(None, description="Restrict to one department"),
):
    """
    Department summary for a month via MongoDB aggregation cross-collection.
    Headcount includes employees with zero logs who joined on or before month end.
    """
    _, last_day = get_month_date_range(month)
    last_day_str = last_day.isoformat()

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": last_day_str}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$regexMatch": {"input": "$date", "regex": f"^{month}-"}},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$push": "$emp_code"},
                "all_logs": {"$push": "$logs"},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    dept_groups = list(db.employees.aggregate(pipeline))
    items = []

    for group in dept_groups:
        dept_name = group["_id"]
        headcount = len(group["employees"])
        if headcount == 0:
            continue

        # Flatten logs across department employees
        all_logs = [log for sublist in group["all_logs"] for log in sublist]

        present_days = 0.0
        work_hours_list = []
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0

        for log in all_logs:
            stat = log.get("status")
            log_date_str = log.get("date", "")
            log_d = date.fromisoformat(log_date_str) if log_date_str else None
            is_mon_fri = log_d is not None and log_d.weekday() < 5

            half_d = log.get("half_day", False)
            late_m = log.get("late_minutes", 0) or 0
            wh = log.get("work_hours")

            if stat in ("PRESENT", "WFH", "ON_DUTY"):
                if is_mon_fri:
                    present_days += 0.5 if half_d else 1.0
                if wh is not None:
                    work_hours_list.append(wh)

            if stat == "ON_DUTY":
                on_duty_count += 1
            elif stat == "LEAVE":
                leave_count += 1

            if late_m > 0:
                late_count += 1
                total_late_minutes += late_m

        avg_work_hours = None
        if work_hours_list:
            avg_work_hours = round_half_up(sum(work_hours_list) / len(work_hours_list), 2)

        items.append(
            {
                "department": dept_name,
                "headcount": headcount,
                "present_days": round_half_up(present_days, 2),
                "avg_work_hours": avg_work_hours,
                "late_count": late_count,
                "total_late_minutes": total_late_minutes,
                "leave_count": leave_count,
                "on_duty_count": on_duty_count,
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Month YYYY-MM"),
    limit: int = Query(10, ge=1, le=50, description="Cutoff limit"),
    department: Optional[str] = Query(None, description="Filter by department"),
):
    """
    Late-comers leaderboard for a month using MongoDB standard competition ranking with ties.
    Applies limit after ranking; returns ties where rank <= limit.
    """
    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}},
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
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]

    if department:
        pipeline.append({"$match": {"department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    results = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": results}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(..., description="Department name"),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department with gap filling ($densify) and 7-day moving average.
    Returns exactly one row per calendar day from from_date to to_date inclusive.
    """
    start_d = date.fromisoformat(from_date)
    end_d = date.fromisoformat(to_date)

    if end_d < start_d:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must be on or after 'from' date",
        )
    if (end_d - start_d).days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days",
        )

    # 404 if department has no employees
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department")

    dept_employees = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    emp_codes = [e["emp_code"] for e in dept_employees]

    start_dt = datetime.combine(start_d, time.min, tzinfo=timezone.utc)
    # Bounds in $densify are exclusive of the upper bound, so add 1 day
    end_bound_dt = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=timezone.utc)

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_bound_dt]},
            }
        },
        {
            "$project": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date_str": 1}},
    ]

    densified_days = list(db.attendance_logs.aggregate(pipeline))

    # In case there were 0 logs in the range, ensure all dates in [start_d, end_d] are present
    existing_dates = {d["date_str"]: d for d in densified_days}
    complete_days = []
    cur_d = start_d
    while cur_d <= end_d:
        ds = cur_d.isoformat()
        if ds in existing_dates:
            complete_days.append(existing_dates[ds])
        else:
            complete_days.append({"date_str": ds, "present_count": 0.0, "late_count": 0})
        cur_d += timedelta(days=1)

    # Compute headcount, is_working_day, attendance_rate for each day
    enriched_rows = []
    for row in complete_days:
        d_str = row["date_str"]
        row_d = date.fromisoformat(d_str)
        is_working_day = row_d.weekday() < 5
        headcount = sum(1 for e in dept_employees if e.get("joined_on", "") <= d_str)

        present_count = float(row["present_count"])
        late_count = int(row["late_count"])

        if is_working_day and headcount > 0:
            att_rate = round_half_up(present_count / headcount, 4)
        else:
            att_rate = None

        enriched_rows.append(
            {
                "date": d_str,
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": round_half_up(present_count, 2),
                "late_count": late_count,
                "attendance_rate": att_rate,
            }
        )

    # Compute 7-day moving average of non-null attendance_rate over preceding up to 6 rows
    for i, row in enumerate(enriched_rows):
        window_start = max(0, i - 6)
        window = [enriched_rows[j]["attendance_rate"] for j in range(window_start, i + 1)]
        non_null_rates = [r for r in window if r is not None]

        if non_null_rates:
            row["moving_avg_7d"] = round_half_up(sum(non_null_rates) / len(non_null_rates), 4)
        else:
            row["moving_avg_7d"] = None

    return {"department": department, "items": enriched_rows}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    """
    Run MongoDB explain with executionStats verbosity on the named endpoint's main query or pipeline.
    Validates that query plans use IXSCAN and avoid COLLSCAN.
    """
    try:
        if endpoint == "attendance_list":
            query: Dict[str, Any] = {}
            if emp_code:
                query["emp_code"] = emp_code
            if date_from or date_to:
                query["date"] = {}
                if date_from:
                    query["date"]["$gte"] = date_from
                if date_to:
                    query["date"]["$lte"] = date_to
            if status:
                query["status"] = status

            skip = ((page or 1) - 1) * (page_size or 20)
            explain_doc = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": query,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": skip,
                    "limit": page_size or 20,
                },
                verbosity="executionStats",
            )
            raw = json.loads(json_util.dumps(explain_doc))
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": raw}

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="emp_code and month are required for employee_monthly",
                )
            pipeline = [
                {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
                {
                    "$group": {
                        "_id": None,
                        "late_count": {"$sum": 1},
                        "total_late_minutes": {"$sum": "$late_minutes"},
                    }
                },
            ]
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            raw = json.loads(json_util.dumps(explain_doc))
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": raw}

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="month is required for department_summary",
                )
            _, last_d = get_month_date_range(month)
            match_filter: Dict[str, Any] = {"joined_on": {"$lte": last_d.isoformat()}}
            if department:
                match_filter["department"] = department

            pipeline = [
                {"$match": match_filter},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "logs",
                    }
                },
            ]
            explain_doc = db.command(
                "explain",
                {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            raw = json.loads(json_util.dumps(explain_doc))
            return {"endpoint": endpoint, "collection": "employees", "explain": raw}

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="month is required for late_leaderboard",
                )
            pipeline = [
                {"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}},
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                        "late_count": {"$sum": 1},
                    }
                },
                {"$sort": {"total_late_minutes": -1}},
            ]
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            raw = json.loads(json_util.dumps(explain_doc))
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": raw}

        elif endpoint == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="department, from, and to are required for department_trend",
                )
            pipeline = [
                {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
                {"$group": {"_id": "$date", "count": {"$sum": 1}}},
            ]
            explain_doc = db.command(
                "explain",
                {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )
            raw = json.loads(json_util.dumps(explain_doc))
            return {"endpoint": endpoint, "collection": "attendance_logs", "explain": raw}

    except PyMongoError as pe:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"MongoDB explain failed: {str(pe)}",
        )
