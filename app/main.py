
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_TS = 100_000_000_000
MAX_TS = 4_102_444_800_000
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def truncate_second(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def from_ms(value):
    if value is None:
        return now_utc()
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be an integer in epoch milliseconds")
    if not MIN_TS <= value <= MAX_TS:
        raise HTTPException(422, "Timestamp is outside the allowed range")
    try:
        return truncate_second(datetime.fromtimestamp(value / 1000, UTC))
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, "Invalid timestamp")


def to_ms(value):
    if value is None:
        return None
    return int(truncate_second(value).timestamp() * 1000)


def iso_date(value):
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, "Date must be YYYY-MM-DD")


def valid_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "Month must be YYYY-MM")
    return value


def rounded(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))



def compute_late_minutes(punch_in, shift_start):
    local = punch_in.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    elapsed_seconds = (local - start).total_seconds()

    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0



def shift_end_datetime(date_str, shift_start, shift_end):
    day = iso_date(date_str)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    end_day = day + timedelta(days=1) if (eh, em) <= (sh, sm) else day
    return datetime.combine(end_day, datetime.min.time(), IST).replace(
        hour=eh, minute=em
    ).astimezone(UTC)


def compute_overtime(punch_out, shift_end, date_str, shift_start="09:30"):
    end = shift_end_datetime(date_str, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = (punch_out - punch_in).total_seconds()
    return rounded(seconds / 3600, 2)


def attendance_date(punch_in, employee):
    local = punch_in.astimezone(IST)
    day = local.date()
    start = tuple(map(int, employee["shift_start"].split(":")))
    end = tuple(map(int, employee["shift_end"].split(":")))
    if end <= start and (local.hour, local.minute) < end:
        day -= timedelta(days=1)
    return day.isoformat()


def serialize(doc):
    if not doc:
        return doc
    result = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("punch_in", "punch_out", "created_at"):
        if key in result:
            result[key] = to_ms(result[key])
    for entry in result.get("history", []):
        entry["at"] = to_ms(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = to_ms(change[side])
    return result


def create_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined_on")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("late_minutes", ASCENDING)], name="ix_attendance_date_status_late")


@app.on_event("startup")
def startup():
    create_indexes()


def page_check(page, page_size):
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be 1..100")


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
    def check_joined_on(cls, value):
        iso_date(value)
        return value

    @field_validator("shift_end")
    @classmethod
    def check_shift(cls, value, info):
        start = info.data.get("shift_start", "09:30")
        if value == start:
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            from_ms(value)
        return value

    @field_validator("status")
    @classmethod
    def check_status(cls, value):
        if value not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            from_ms(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def check_status(cls, value):
        if value is not None and value not in STATUSES:
            raise ValueError("Invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_timestamp(cls, value):
        if value is not None:
            from_ms(value)
        return value


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "Shift times must differ")
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    items = list(employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        item["created_at"] = to_ms(item["created_at"])
    return {"items": items, "total": employees.count_documents(q), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    ts = from_ms(body.punched_at)
    date_str = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code, "date": date_str, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date")
    return serialize(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    ts = from_ms(body.punched_at)
    record = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    elapsed = (ts - record["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 86400:
        raise HTTPException(422, "Punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    updated = logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Already punched out")
    return serialize(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        if status not in STATUSES:
            raise HTTPException(422, "Invalid status")
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            iso_date(date_from)
            q["date"]["$gte"] = date_from
        if date_to:
            iso_date(date_to)
            q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not exceed date_to")
    items = list(logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize(x) for x in items], "total": logs.count_documents(q), "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    iso_date(date)

    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    supplied = body.model_fields_set

    if body.status in {"ABSENT", "LEAVE"} and (
        "punch_in" in supplied or "punch_out" in supplied
    ):
        raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")

    new_status = body.status if "status" in supplied else record["status"]

    if new_status in {"ABSENT", "LEAVE"}:
        new_in = None
        new_out = None
    else:
        new_in = (
            from_ms(body.punch_in)
            if "punch_in" in supplied and body.punch_in is not None
            else record.get("punch_in")
        )
        new_out = (
            from_ms(body.punch_out)
            if "punch_out" in supplied and body.punch_out is not None
            else record.get("punch_out")
        )

        if new_in is None:
            raise HTTPException(422, "Presence status requires punch_in")

        if attendance_date(new_in, emp) != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")

        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new_late = compute_late_minutes(new_in, emp["shift_start"]) if new_in else 0
    new_hours = compute_work_hours(new_in, new_out) if new_in and new_out else None
    new_overtime = (
        compute_overtime(new_out, emp["shift_end"], date, emp["shift_start"])
        if new_in and new_out else 0
    )
    new_half_day = new_hours is not None and new_hours < 4.50

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }

    changes = {}
    for field, new_value in new_values.items():
        old_value = record.get(field)
        if old_value != new_value:
            if field in {"punch_in", "punch_out"}:
                old_value = to_ms(old_value)
                new_value_for_history = to_ms(new_value)
            else:
                new_value_for_history = new_value

            changes[field] = {
                "from": old_value,
                "to": new_value_for_history,
            }

    if not changes:
        raise HTTPException(422, "The request does not change the record")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    original_history = record.get("history", [])
    updated = logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": original_history,
        },
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(409, "Record changed concurrently; retry the correction")

    return serialize(updated)



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    valid_month(month)

    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, month_num = map(int, month.split("-"))
    start_day = datetime(year, month_num, 1).date()
    next_month = (
        datetime(year + 1, 1, 1).date()
        if month_num == 12
        else datetime(year, month_num + 1, 1).date()
    )
    end_day = next_month - timedelta(days=1)

    joined = iso_date(emp["joined_on"])
    first_working_day = max(start_day, joined)

    working_days = 0
    current_day = first_working_day
    while current_day <= end_day:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_day.isoformat(), "$lt": next_month.isoformat()},
            }
        },
        {
            "$addFields": {
                "_weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
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
                                    {"$in": ["$status", list(PRESENCE)]},
                                    {"$gte": ["$_weekday", 2]},
                                    {"$lte": ["$_weekday", 6]},
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
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    result = list(logs.aggregate(pipeline))
    stats = result[0] if result else {}

    present_days = stats.get("present_days", 0)
    attendance_pct = (
        rounded(present_days * 100 / working_days, 2)
        if working_days > 0 else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    valid_month(month)

    year, month_num = map(int, month.split("-"))
    start = datetime(year, month_num, 1).date()
    next_month = (
        datetime(year + 1, 1, 1).date()
        if month_num == 12
        else datetime(year, month_num + 1, 1).date()
    )
    month_end = (next_month - timedelta(days=1)).isoformat()

    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lt": ["$date", next_month.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_weekday": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            }
                        },
                    },
                ],
                "as": "records",
            }
        },
        {
            "$addFields": {
                "_present_records": {
                    "$filter": {
                        "input": "$records",
                        "as": "r",
                        "cond": {"$in": ["$$r.status", list(PRESENCE)]},
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$sum": {
                            "$map": {
                                "input": "$_present_records",
                                "as": "r",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$gte": ["$$r._weekday", 2]},
                                                {"$lte": ["$$r._weekday", 6]},
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$$r.half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                },
                            }
                        }
                    }
                },
                
                
                
                
                "avg_work_hours": {
                    "$sum": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$_present_records",
                                        "as": "r",
                                        "cond": {
                                            "$ne": ["$$r.work_hours", None]
                                        }
                                    }
                                },
                                "as": "r",
                                "in": "$$r.work_hours"
                            }
                        }
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$size": {
                            "$filter": {
                                "input": "$_present_records",
                                "as": "r",
                                "cond": {
                                    "$ne": ["$$r.work_hours", None]
                                }
                            }
                        }
                    }
                },




                "late_count": {
                    "$sum": {
                        "$size": {
                            "$filter": {
                                "input": "$records",
                                "as": "r",
                                "cond": {"$gt": ["$$r.late_minutes", 0]},
                            }
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$sum": {
                            "$map": {
                                "input": "$records",
                                "as": "r",
                                "in": "$$r.late_minutes",
                            }
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$size": {
                            "$filter": {
                                "input": "$records",
                                "as": "r",
                                "cond": {"$eq": ["$$r.status", "LEAVE"]},
                            }
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$size": {
                            "$filter": {
                                "input": "$records",
                                "as": "r",
                                "cond": {"$eq": ["$$r.status", "ON_DUTY"]},
                            }
                        }
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
        
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                "$avg_work_hours",
                                "$work_hours_count"
                            ]
                        },
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },

    ]

    items = list(employees.aggregate(pipeline))
    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = rounded(item["avg_work_hours"], 2)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    valid_month(month)

    year, month_num = map(int, month.split("-"))
    start = f"{month}-01"
    next_month = (
        f"{year + 1:04d}-01-01"
        if month_num == 12
        else f"{year:04d}-{month_num + 1:02d}-01"
    )

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start, "$lt": next_month},
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
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return {"month": month, "items": list(logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if not from_date or not to_date:
        raise HTTPException(422, "Both from and to dates are required")

    start_day = iso_date(from_date)
    end_day = iso_date(to_date)

    if end_day < start_day:
        raise HTTPException(422, "to must not be earlier than from")

    days = (end_day - start_day).days + 1
    if days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if not employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    start_dt = datetime.combine(start_day, datetime.min.time(), UTC)
    end_exclusive = datetime.combine(
        end_day + timedelta(days=1), datetime.min.time(), UTC
    )

    pipeline = [
        {
            "$documents": [
                {"day": start_dt},
                {"day": end_exclusive},
            ]
        },
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {"$match": {"day": {"$lt": end_exclusive}}},
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "date": "$day",
                        "format": "%Y-%m-%d",
                        "timezone": "+05:30",
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$day"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$day"}, 6]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$trend_date"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "eligible_employees",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "trend_date": "$date",
                    "codes": "$eligible_employees.emp_code",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$trend_date"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "daily_logs",
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$eligible_employees"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$daily_logs",
                                    "as": "r",
                                    "cond": {"$in": ["$$r.status", list(PRESENCE)]},
                                }
                            },
                            "as": "r",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$r.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$daily_logs",
                            "as": "r",
                            "cond": {"$gt": ["$$r.late_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount",
                                    ]
                                },
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {"$round": ["$attendance_rate", 4]},
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.aggregate(pipeline))
    return {"department": department, "items": items}



from bson.json_util import dumps, RELAXED_JSON_OPTIONS


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed:
        raise HTTPException(422, "Unsupported explain endpoint")

    if endpoint == "attendance_list":
        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if status is not None:
            if status not in STATUSES:
                raise HTTPException(422, "Invalid status")
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                iso_date(date_from)
                query["date"]["$gte"] = date_from
            if date_to:
                iso_date(date_to)
                query["date"]["$lte"] = date_to
            if date_from and date_to and date_from > date_to:
                raise HTTPException(422, "date_from must not exceed date_to")

        explain = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        collection = "attendance_logs"

    else:
        if not month or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(422, "A valid month=YYYY-MM is required")

        year, month_num = map(int, month.split("-"))
        month_start = f"{month}-01"
        month_end = (
            f"{year + 1:04d}-01-01"
            if month_num == 12
            else f"{year:04d}-{month_num + 1:02d}-01"
        )

        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(422, "emp_code is required")
            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {"$gte": month_start, "$lt": month_end},
                    }
                },
                {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}},
            ]
            collection = "attendance_logs"

        elif endpoint == "department_summary":
            employee_match = {}
            if department is not None:
                employee_match["department"] = department
            employee_match["joined_on"] = {"$lt": month_end}
            pipeline = [
                {"$match": employee_match},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"code": "$emp_code"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$emp_code", "$$code"]},
                                            {"$gte": ["$date", month_start]},
                                            {"$lt": ["$date", month_end]},
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "records",
                    }
                },
            ]
            collection = "employees"

        elif endpoint == "late_leaderboard":
            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": month_start, "$lt": month_end},
                        "late_minutes": {"$gt": 0},
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                    }
                },
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
            ]
            collection = "attendance_logs"

        else:
            if not department or not from_date or not to_date:
                raise HTTPException(
                    422, "department, from and to are required for department_trend"
                )
            start_day = iso_date(from_date)
            end_day = iso_date(to_date)
            if end_day < start_day or (end_day - start_day).days + 1 > 92:
                raise HTTPException(422, "Invalid trend date range")
            if not employees.find_one({"department": department}):
                raise HTTPException(404, "Department not found")
            pipeline = [
                {
                    "$match": {
                        "date": {
                            "$gte": from_date,
                            "$lte": to_date,
                        }
                    }
                },
                {"$sort": {"date": 1, "emp_code": 1}},
            ]
            collection = "attendance_logs"

        explain = db.command({
            "explain": {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": __import__("json").loads(
            dumps(explain, json_options=RELAXED_JSON_OPTIONS)
        ),
    }
