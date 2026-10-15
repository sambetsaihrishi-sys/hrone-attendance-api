import os
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pymongo import ASCENDING, DESCENDING
from bson import json_util
from fastapi.responses import Response

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_attendance_employee_punch_in",
    )
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def epoch_ms(value: datetime) -> int:
    return int(to_utc(value).timestamp() * 1000)


def serialize(value):
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items() if key != "_id"}
    return value


def parse_timestamp(value: Optional[int]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    return datetime.fromtimestamp(value // 1000, tz=timezone.utc)


def valid_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Date must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid calendar date") from exc
    return value


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    hour, minute = map(int, shift_start.split(":"))
    scheduled_start = datetime.combine(
        date.fromisoformat(attendance_date),
        datetime.min.time(),
        tzinfo=IST,
    ).replace(hour=hour, minute=minute)
    late_seconds = (to_utc(punch_in).astimezone(IST) - scheduled_start).total_seconds()
    return int(late_seconds // 60) if late_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((to_utc(punch_out) - to_utc(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    scheduled_end = datetime.combine(
        date.fromisoformat(attendance_date), datetime.min.time(), tzinfo=IST
    ).replace(hour=end_hour, minute=end_minute)
    if (end_hour, end_minute) <= (start_hour, start_minute):
        scheduled_end += timedelta(days=1)
    seconds = (to_utc(punch_out).astimezone(IST) - scheduled_end).total_seconds()
    return int(seconds // 60) if seconds >= 1800 else 0


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = to_utc(punch_in).astimezone(IST)
    start = tuple(map(int, shift_start.split(":")))
    end = tuple(map(int, shift_end.split(":")))
    day = local.date()
    if end <= start and (local.hour, local.minute, local.second) < (*end, 0):
        day -= timedelta(days=1)
    return day.isoformat()

def to_utc_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(
        value // 1000,
        tz=timezone.utc,
    )


def to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)

import calendar


def get_month_range(month: str) -> tuple[str, str]:
    try:
        if len(month) != 7 or month[4] != "-":
            raise ValueError()

        year = int(month[:4])
        month_number = int(month[5:7])

        if year < 1 or not 1 <= month_number <= 12:
            raise ValueError()

        last_day = calendar.monthrange(year, month_number)[1]

        return (
            f"{year:04d}-{month_number:02d}-01",
            f"{year:04d}-{month_number:02d}-{last_day:02d}",
        )

    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Month must be in YYYY-MM format",
        )

def validate_date_range(date_from: str, date_to: str):
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()

        if start.isoformat() != date_from or end.isoformat() != date_to:
            raise ValueError()

    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must be in YYYY-MM-DD format",
        )

    if start > end:
        raise HTTPException(
            status_code=422,
            detail="Start date cannot be after end date",
        )

    if (end - start).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    return start, end


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field(default="18:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        return valid_date(value)

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_end must differ from shift_start")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000, strict=True)

class AttendanceCorrectionIn(BaseModel):
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None

    punch_in: Optional[int] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        strict=True,
    )

    punch_out: Optional[int] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
        strict=True,
    )

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    timestamp = parse_timestamp(body.punched_at)
    attendance_date = get_attendance_date(timestamp, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": timestamp,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(timestamp, employee["shift_start"], attendance_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="Attendance already exists for this date") from exc
    return serialize(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    timestamp = parse_timestamp(body.punched_at)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": timestamp},
            "status": {"$in": PRESENT_STATUSES},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No eligible punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance already punched out")
    started = to_utc(record["punch_in"])
    elapsed = (timestamp - started).total_seconds()
    if elapsed <= 0 or elapsed > 86400:
        raise HTTPException(status_code=422, detail="Punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(started, timestamp)
    overtime = compute_overtime(timestamp, employee["shift_start"], employee["shift_end"], record["date"])
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": timestamp, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="Attendance already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize(updated)


@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(
    emp_code: str,
    date: str,
    body: AttendanceCorrectionIn,
):
    try:
        if datetime.fromisoformat(date).date().isoformat() != date:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, "Invalid attendance date")

    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )

    if record is None:
        raise HTTPException(404, "Attendance record not found")

    original = record.copy()
    updated = record.copy()
    supplied = body.model_fields_set

    if "status" in supplied:
        if body.status is None:
            raise HTTPException(422, "Status cannot be null")
        updated["status"] = body.status

    if "punch_in" in supplied:
        updated["punch_in"] = (
            to_utc_datetime(body.punch_in)
            if body.punch_in is not None
            else None
        )

    if "punch_out" in supplied:
        updated["punch_out"] = (
            to_utc_datetime(body.punch_out)
            if body.punch_out is not None
            else None
        )

    presence_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    if updated["status"] in {"ABSENT", "LEAVE"}:
        if (
            ("punch_in" in supplied and body.punch_in is not None)
            or ("punch_out" in supplied and body.punch_out is not None)
        ):
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot contain punch times",
            )

        updated["punch_in"] = None
        updated["punch_out"] = None
        updated["work_hours"] = None
        updated["late_minutes"] = 0
        updated["overtime_minutes"] = 0
        updated["half_day"] = False

    elif updated["status"] in presence_statuses:
        punch_in_time = updated.get("punch_in")
        punch_out_time = updated.get("punch_out")

        if punch_in_time is None:
            raise HTTPException(
                422,
                "Presence status requires punch_in",
            )

        if get_attendance_date(
            punch_in_time,
            employee["shift_start"],
            employee["shift_end"],
        ) != date:
            raise HTTPException(
                422,
                "Punch-in does not match attendance date",
            )

        updated["late_minutes"] = compute_late_minutes(
            punch_in_time,
            employee["shift_start"],
            date,
        )

        if punch_out_time is None:
            updated["work_hours"] = None
            updated["overtime_minutes"] = 0
            updated["half_day"] = False
        else:
            if punch_in_time.tzinfo is None:
                punch_in_time = punch_in_time.replace(
                    tzinfo=timezone.utc
                )

            if punch_out_time.tzinfo is None:
                punch_out_time = punch_out_time.replace(
                    tzinfo=timezone.utc
                )

            duration = (
                punch_out_time - punch_in_time
            ).total_seconds()

            if duration <= 0 or duration > 86400:
                raise HTTPException(
                    422,
                    "Punch-out must be after punch-in and within 24 hours",
                )

            updated["work_hours"] = compute_work_hours(
                punch_in_time,
                punch_out_time,
            )

            updated["overtime_minutes"] = compute_overtime(
                punch_out_time,
                employee["shift_start"],
                employee["shift_end"],
                date,
            )

            updated["half_day"] = updated["work_hours"] < 4.50

    fields = [
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ]

    changes = {}

    for field in fields:
        old_value = original.get(field)
        new_value = updated.get(field)

        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(
            422,
            "Correction must change at least one field",
        )

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        {
            "$set": {
                field: updated[field]
                for field in fields
            },
            "$push": {
                "history": history_entry
            },
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "Attendance record changed concurrently",
        )

    corrected = db.attendance_logs.find_one(
        {"_id": record["_id"]},
        {"_id": 0},
    )

    corrected["punch_in"] = to_epoch_ms(
        corrected.get("punch_in")
    )
    corrected["punch_out"] = to_epoch_ms(
        corrected.get("punch_out")
    )

    for entry in corrected.get("history", []):
        entry["at"] = to_epoch_ms(entry["at"])

        for change in entry["changes"].values():
            for key in ("from", "to"):
                if isinstance(change[key], datetime):
                    change[key] = to_epoch_ms(change[key])

    return corrected
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    for value in (date_from, date_to):
        if value is not None:
            try:
                if datetime.fromisoformat(value).date().isoformat() != value:
                    raise ValueError()
            except ValueError:
                raise HTTPException(422, "Invalid date format")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if status:
        query["status"] = status

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            query["date"]["$gte"] = date_from

        if date_to:
            query["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(query)

    records = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for record in records:
        record["punch_in"] = to_epoch_ms(record.get("punch_in"))
        record["punch_out"] = to_epoch_ms(record.get("punch_out"))

        record.setdefault("history", [])
        record.setdefault("half_day", False)

        for entry in record["history"]:
            entry["at"] = to_epoch_ms(entry["at"])

            for change in entry["changes"].values():
                for key in ("from", "to"):
                    if isinstance(change[key], datetime):
                        change[key] = to_epoch_ms(change[key])

    return {
        "items": records,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str,
):
    start_date, end_date = get_month_range(month)

    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(404, "Employee not found")

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_date,
                    "$lte": end_date,
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$lt": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d",
                                                    }
                                                }
                                            },
                                            6,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = run_aggregation(db.attendance_logs, pipeline)

    summary = results[0] if results else {}

    joined_on = employee["joined_on"]
    effective_start = max(start_date, joined_on)

    working_days = 0

    if effective_start <= end_date:
        current = datetime.strptime(
            effective_start, "%Y-%m-%d"
        ).date()

        last = datetime.strptime(
            end_date, "%Y-%m-%d"
        ).date()

        while current <= last:
            if current.weekday() < 5:
                working_days += 1

            current += timedelta(days=1)

    present_days = summary.get("present_days", 0)

    attendance_pct = (
        float(
            Decimal(str(present_days * 100 / working_days)).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        )
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get(
            "total_late_minutes", 0
        ),
        "total_overtime_minutes": summary.get(
            "total_overtime_minutes", 0
        ),
        "attendance_pct": attendance_pct,
    }
@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None,
):
    start_date, end_date = get_month_range(month)

    match_employees = {
        "joined_on": {"$lte": end_date}
    }

    if department is not None:
        match_employees["department"] = department

    pipeline = [
        {
            "$match": match_employees
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$emp_code", "$$code"]
                            },
                            "date": {
                                "$gte": start_date,
                                "$lte": end_date,
                            },
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employee_codes": {
                    "$addToSet": "$emp_code"
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$lt": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date",
                                                        "format": "%Y-%m-%d",
                                                        "onError": None,
                                                        "onNull": None,
                                                    }
                                                }
                                            },
                                            6,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours_total": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            "$logs.work_hours",
                                            None,
                                        ]
                                    },
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            "$logs.work_hours",
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
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$logs.late_minutes", 0]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {
                    "$size": "$employee_codes"
                },
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$work_hours_total",
                                        "$work_hours_count",
                                    ]
                                },
                                2,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {
            "$sort": {
                "department": 1
            }
        },
    ]

    results = run_aggregation(db.employees, pipeline)

    return {
        "month": month,
        "departments": results,
    }

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(default=10, ge=1),
    department: Optional[str] = None,
):
    start_date, end_date = get_month_range(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": start_date,
                    "$lte": end_date,
                },
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {
                        "total_late_minutes": -1
                    },
                    "output": {
                        "rank": {
                            "$rank": {}
                        }
                    },
                }
            },
            {
                "$match": {
                    "rank": {"$lte": limit}
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "late_count": 1,
                    "total_late_minutes": 1,
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
        ]
    )

    results = run_aggregation(db.attendance_logs, pipeline)

    return {
        "month": month,
        "leaderboard": results,
    }

@app.get("/analytics/departments/{department}/trend")
def department_attendance_trend(
    department: str,
    date_from: str = Query(alias="from"),
    date_to: str = Query(alias="to"),
):
    start, end = validate_date_range(date_from, date_to)
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    start_text = start.isoformat()
    end_text = end.isoformat()
    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "first": {"$dateFromString": {"dateString": start_text}}}},
        {"$project": {"dates": {"$map": {
            "input": {"$range": [0, (end - start).days + 1]},
            "as": "offset",
            "in": {"$dateAdd": {"startDate": "$first", "unit": "day", "amount": "$$offset"}}
        }}}},
        {"$unwind": "$dates"},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates"}},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$dates"}, 5]}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "count"}
            ],
            "as": "employee_count"
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee"
                }},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENT_STATUSES)]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
                }}
            ],
            "as": "daily_logs"
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$employee_count.count", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily_logs.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily_logs.late_count", 0]}, 0]}
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [
                {"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}},
                10000
            ]},
            None
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"raw_moving_average": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$raw_moving_average", None]},
                None,
                {"$divide": [{"$floor": {"$add": [{"$multiply": ["$raw_moving_average", 10000]}, 0.5]}}, 10000]}
            ]}
        }},
        {"$sort": {"date": 1}}
    ]
    days = run_aggregation(db.employees, pipeline)
    return {"department": department, "from": start_text, "to": end_text, "days": days}
_pipeline_capture = ContextVar("pipeline_capture", default=None)


class PipelineCaptured(Exception):
    pass


def run_aggregation(collection, pipeline):
    captured = _pipeline_capture.get()

    if captured is not None and collection.name == captured["collection_name"]:
        captured["pipeline"] = pipeline
        raise PipelineCaptured()

    return list(collection.aggregate(pipeline))


def capture_aggregation_pipeline(function, *args, collection_name):
    captured = {
        "collection_name": collection_name,
        "pipeline": None,
    }

    token = _pipeline_capture.set(captured)

    try:
        try:
            function(*args)
        except PipelineCaptured:
            pass
    finally:
        _pipeline_capture.reset(token)

    if captured["pipeline"] is None:
        raise HTTPException(
            status_code=500,
            detail="Unable to capture aggregation pipeline",
        )

    return captured["pipeline"]
@app.get("/admin/explain/{endpoint}")
def explain_query(
    endpoint: str,
    month: Optional[str] = None,
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, alias="from"),
    date_to: Optional[str] = Query(default=None, alias="to"),
    limit: int = Query(default=10, ge=1),
):
    allowed_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed_endpoints:
        raise HTTPException(
            status_code=422,
            detail="Invalid endpoint name",
        )

    if endpoint == "attendance_list":
        query = {}

        if emp_code is not None:
            query["emp_code"] = emp_code

        if date_from is not None or date_to is not None:
            date_filter = {}

            if date_from is not None:
                date_filter["$gte"] = date_from

            if date_to is not None:
                date_filter["$lte"] = date_to

            query["date"] = date_filter

        explain = db.command(
            {
                "explain": {
                    "find": "attendance_logs",
                    "filter": query,
                    "sort": {"date": -1, "emp_code": 1},
                },
                "verbosity": "executionStats",
            }
        )

        return Response(
            content=json_util.dumps({
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": explain,
            }),
            media_type="application/json",
        )

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        get_month_range(month)

        pipeline = capture_aggregation_pipeline(
            employee_monthly_analytics,
            emp_code,
            month,
            collection_name="attendance_logs",
        )

        explain = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )

        return Response(
            content=json_util.dumps({
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": explain,
            }),
            media_type="application/json",
        )

        if endpoint == "department_summary":
         if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        get_month_range(month)

        pipeline = capture_aggregation_pipeline(
            department_summary,
            month,
            department,
            collection_name="employees",
        )

        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        return Response(
            content=json_util.dumps({
                "endpoint": endpoint,
                "collection": "employees",
                "explain": explain,
            }),
            media_type="application/json",
        )

        if endpoint == "late_leaderboard":
          if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        get_month_range(month)

        pipeline = capture_aggregation_pipeline(
            late_leaderboard,
            month,
            limit,
            department,
            collection_name="attendance_logs",
        )

        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        return Response(
            content=json_util.dumps({
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": explain,
            }),
            media_type="application/json",
        )
        if endpoint == "department_trend":
          if not department or not date_from or not date_to:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required",
            )

        validate_date_range(date_from, date_to)

        pipeline = capture_aggregation_pipeline(
            department_attendance_trend,
            department,
            date_from,
            date_to,
            collection_name="employees",
        )

        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        return Response(
            content=json_util.dumps({
                "endpoint": endpoint,
                "collection": "employees",
                "explain": explain,
            }),
            media_type="application/json",
        )

    return {
        "endpoint": endpoint,
        "collection": None,
        "explain": {},
    }