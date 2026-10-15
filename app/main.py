"""Employee Attendance & Analytics API for the HROne engineering assignment."""
from __future__ import annotations
import os, re, calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv()
UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
MIN_EPOCH = 100_000_000_000
MAX_EPOCH = 4_102_444_800_000
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2500, tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_emp_date_status")
    logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)], name="attendance_date_late")

# ---------- validation / time helpers ----------
def epoch_ms(value: Optional[int], default_now=False) -> datetime:
    if value is None and default_now:
        value = int(datetime.now(UTC).timestamp() * 1000)
    if isinstance(value, bool) or not isinstance(value, int) or value < MIN_EPOCH or value > MAX_EPOCH:
        raise HTTPException(422, "Timestamp must be an integer epoch millisecond value in range")
    return datetime.fromtimestamp(value // 1000, UTC)

def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000

def round_half_up(value: float, places=2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))

def parse_date(s: str) -> date:
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s: raise ValueError()
        return d
    except Exception: raise HTTPException(422, "Date must be YYYY-MM-DD")

def validate_month(month: str) -> tuple[date,date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month): raise HTTPException(422, "month must be YYYY-MM")
    y,m = map(int, month.split("-")); return date(y,m,1), date(y,m,calendar.monthrange(y,m)[1])

def shift_dt(day: date, hhmm: str) -> datetime:
    h,m = map(int, hhmm.split(":")); return datetime.combine(day, time(h,m), IST)

def attendance_day(punch: datetime, emp: dict) -> date:
    local = punch.astimezone(IST)
    start = emp.get("shift_start", "09:30"); end = emp.get("shift_end", "18:30")
    if end <= start and local.strftime("%H:%M") < end:
        return local.date() - timedelta(days=1)
    return local.date()

def shift_bounds(att_day: date, emp: dict) -> tuple[datetime, datetime]:
    start_s, end_s = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    start = shift_dt(att_day, start_s); end_day = att_day + (timedelta(days=1) if end_s <= start_s else timedelta())
    return start, shift_dt(end_day, end_s)

def derived(emp: dict, pin: Optional[datetime], pout: Optional[datetime]) -> dict:
    late = overtime = 0; hours = None; half = False
    if pin:
        local = pin.astimezone(IST); day = attendance_day(pin, emp); start, end = shift_bounds(day, emp)
        delta = (local - start).total_seconds()
        late = int(delta // 60) if delta > 600 else 0
    if pin and pout:
        seconds = int((pout-pin).total_seconds())
        hours = round_half_up(seconds / 3600, 2)
        _, shift_end = shift_bounds(attendance_day(pin, emp), emp)
        ot = int((pout.astimezone(IST)-shift_end).total_seconds() // 60)
        overtime = ot if ot >= 30 else 0
        half = hours < 4.5
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": half}

def public_employee(d: dict) -> dict:
    return {"emp_code":d["emp_code"],"name":d["name"],"email":d["email"],"department":d["department"],"shift_start":d["shift_start"],"shift_end":d["shift_end"],"joined_on":d["joined_on"],"created_at":to_ms(d.get("created_at"))}

def public_log(d: dict) -> dict:
    history=[]
    for h in d.get("history", []):
        changes={}
        for k,v in h.get("changes",{}).items():
            changes[k]={"from": to_ms(v.get("from")) if k in ("punch_in","punch_out") else v.get("from"), "to":to_ms(v.get("to")) if k in ("punch_in","punch_out") else v.get("to")}
        history.append({"at":to_ms(h.get("at")),"by":h.get("by"),"reason":h.get("reason"),"changes":changes})
    return {"emp_code":d["emp_code"],"date":d["date"],"status":d["status"],"punch_in":to_ms(d.get("punch_in")),"punch_out":to_ms(d.get("punch_out")),"work_hours":d.get("work_hours"),"late_minutes":d.get("late_minutes",0),"overtime_minutes":d.get("overtime_minutes",0),"half_day":d.get("half_day",False),"history":history}

def paged(collection, filt, sort, page, page_size, mapper):
    total=collection.count_documents(filt)
    items=[mapper(x) for x in collection.find(filt).sort(sort).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}

class EmployeeCreate(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1,max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1,max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def date_valid(cls,v): parse_date(v); return v
    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self
class PunchIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int]=None
    status: Literal["PRESENT","WFH","ON_DUTY"]="PRESENT"
class PunchOut(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int]=None
class Regularize(BaseModel):
    model_config=ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"]]=None
    punch_in: Optional[int]=None
    punch_out: Optional[int]=None
    reason: str=Field(min_length=5,max_length=200)
    regularized_by: str=Field(min_length=1,max_length=50)
    @field_validator("punch_in","punch_out")
    @classmethod
    def check_ts(cls,v):
        if v is not None: epoch_ms(v)
        return v

@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except Exception: raise HTTPException(503,"MongoDB unavailable")

@app.post("/employees",status_code=201)
def create_employee(body: EmployeeCreate):
    doc=body.model_dump(); doc["created_at"]=datetime.now(UTC).replace(microsecond=0)
    try: employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return public_employee(doc)

@app.get("/employees")
def list_employees(department: Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    filt={"department":department} if department is not None else {}
    return paged(employees,filt,[("emp_code",ASCENDING)],page,page_size,public_employee)

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchIn):
    emp=employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"Employee not found")
    pin=epoch_ms(body.punched_at,True); day=attendance_day(pin,emp).isoformat(); calc=derived(emp,pin,None)
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":pin,"punch_out":None,"work_hours":None,"late_minutes":calc["late_minutes"],"overtime_minutes":0,"half_day":False,"history":[]}
    try: logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"Attendance already exists for this employee and date")
    return public_log(doc)

@app.post("/attendance/punch-out")
def punch_out(body:PunchOut):
    emp=employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"Employee not found")
    pout=epoch_ms(body.punched_at,True)
    rec=logs.find_one({"emp_code":body.emp_code,"punch_in":{"$ne":None,"$lte":pout}},sort=[("punch_in",DESCENDING)])
    if not rec: raise HTTPException(404,"No punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409,"Record already punched out")
    if pout <= rec["punch_in"] or (pout-rec["punch_in"]).total_seconds()>86400: raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    calc=derived(emp,rec["punch_in"],pout)
    updated=logs.find_one_and_update({"_id":rec["_id"],"punch_out":None},{"$set":{"punch_out":pout,**calc}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"Record already punched out")
    return public_log(updated)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    filt={}
    if emp_code is not None:filt["emp_code"]=emp_code
    if date_from is not None:parse_date(date_from);filt.setdefault("date",{})["$gte"]=date_from
    if date_to is not None:parse_date(date_to);filt.setdefault("date",{})["$lte"]=date_to
    if date_from and date_to and date_from>date_to:raise HTTPException(422,"date_from must not be after date_to")
    if status is not None:
        if status not in STATUSES:raise HTTPException(422,"Invalid status")
        filt["status"]=status
    return paged(logs,filt,[("date",DESCENDING),("emp_code",ASCENDING)],page,page_size,public_log)

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:str,body:Regularize):
    parse_date(date); emp=employees.find_one({"emp_code":emp_code})
    if not emp:raise HTTPException(404,"Employee not found")
    rec=logs.find_one({"emp_code":emp_code,"date":date})
    if not rec:raise HTTPException(404,"Attendance record not found")
    old={"status":rec.get("status"),"punch_in":rec.get("punch_in"),"punch_out":rec.get("punch_out"),"work_hours":rec.get("work_hours"),"late_minutes":rec.get("late_minutes",0),"overtime_minutes":rec.get("overtime_minutes",0),"half_day":rec.get("half_day",False)}
    status=body.status if body.status is not None else rec["status"]
    if status in {"ABSENT","LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:raise HTTPException(422,"ABSENT/LEAVE cannot include punch times")
        pin=pout=None
    else:
        pin=epoch_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        pout=epoch_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if pin is None:raise HTTPException(422,"Presence status requires punch_in")
        if attendance_day(pin,emp).isoformat()!=date:raise HTTPException(422,"punch_in must belong to the record attendance date")
        if pout is not None and (pout<=pin or (pout-pin).total_seconds()>86400):raise HTTPException(422,"Invalid punch_out")
    calc=derived(emp,pin,pout) if status in PRESENCE else {"late_minutes":0,"work_hours":None,"overtime_minutes":0,"half_day":False}
    new={"status":status,"punch_in":pin,"punch_out":pout,**calc}
    changes={}
    for k,v in new.items():
        before=old[k]
        if before!=v:
            changes[k]={"from":before,"to":v}
    if not changes:raise HTTPException(422,"Regularization does not change anything")
    entry={"at":datetime.now(UTC).replace(microsecond=0),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    # Compare-and-swap on old fields prevents concurrent updates overwriting audit history.
    query={"_id":rec["_id"]}
    if "history" in rec: query["history"]=rec["history"]
    else: query["history"]={"$exists":False}
    for k in ("status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day"):
        query[k]=rec.get(k)
    updated=logs.find_one_and_update(query,{"$set":new,"$push":{"history":entry}},return_document=ReturnDocument.AFTER)
    if not updated:raise HTTPException(409,"Record changed concurrently; retry regularization")
    return public_log(updated)

# Mongo aggregation expressions use stored derived values, as required by the contract.
def present_expr():
    return {"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1.0]},0.0]}
def month_match(month):
    first,last=validate_month(month); return first.isoformat(),last.isoformat()
def working_days(start:date,end:date)->int:
    if start>end:return 0
    count=0; cur=start
    while cur<=end:
        if cur.weekday()<5:count+=1
        cur+=timedelta(days=1)
    return count

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str):
    first,last=month_match(month); emp=employees.find_one({"emp_code":emp_code})
    if not emp:raise HTTPException(404,"Employee not found")
    start=max(first,parse_date(emp["joined_on"]))
    wd=working_days(start,last)
    pipeline=[{"$match":{"emp_code":emp_code,"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}}},{"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$lt":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},7]},{"$gt":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},1]}]},present_expr(),0]}},"leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},"total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"total_overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}]
    got=list(logs.aggregate(pipeline)); x=got[0] if got else {}
    return {"emp_code":emp_code,"month":month,"working_days":wd,"present_days":round_half_up(x.get("present_days",0),2),"leave_days":x.get("leave_days",0),"late_count":x.get("late_count",0),"total_late_minutes":x.get("total_late_minutes",0),"total_overtime_minutes":x.get("total_overtime_minutes",0),"attendance_pct":None if wd==0 else round_half_up(x.get("present_days",0)/wd*100,2)}

@app.get("/analytics/departments/summary")
def department_summary(month:str,department:Optional[str]=None):
    first,last=month_match(month)
    match={"joined_on":{"$lte":last.isoformat()}}
    if department is not None:match["department"]=department
    pipeline=[{"$match":match},{"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",first.isoformat()]},{"$lte":["$date",last.isoformat()]}]}}}],"as":"month_logs"}},{"$group":{"_id":"$department","headcount":{"$sum":1},"present_days":{"$sum":{"$sum":{"$map":{"input":"$month_logs","as":"l","in":{"$cond":[{"$and":[{"$in":["$$l.status",list(PRESENCE)]},{"$lt":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$$l.date"}}},7]},{"$gt":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$$l.date"}}},1]}]},{"$cond":[{"$eq":[{"$ifNull":["$$l.half_day",False]},True]},0.5,1.0]},0.0]}}}}},"work_hours_values":{"$push":{"$map":{"input":{"$filter":{"input":"$month_logs","as":"l","cond":{"$and":[{"$in":["$$l.status",list(PRESENCE)]},{"$ne":["$$l.work_hours",None]}]}}},"as":"l","in":"$$l.work_hours"}}},"late_count":{"$sum":{"$size":{"$filter":{"input":"$month_logs","as":"l","cond":{"$gt":[{"$ifNull":["$$l.late_minutes",0]},0]}}}}},"total_late_minutes":{"$sum":{"$sum":{"$map":{"input":"$month_logs","as":"l","in":{"$ifNull":["$$l.late_minutes",0]}}}}},"leave_count":{"$sum":{"$size":{"$filter":{"input":"$month_logs","as":"l","cond":{"$eq":["$$l.status","LEAVE"]}}}}},"on_duty_count":{"$sum":{"$size":{"$filter":{"input":"$month_logs","as":"l","cond":{"$eq":["$$l.status","ON_DUTY"]}}}}}}},{"$project":{"_id":0,"department":"$_id","headcount":1,"present_days":1,"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1,"flat_hours":{"$reduce":{"input":"$work_hours_values","initialValue":[],"in":{"$concatArrays":["$$value","$$this"]}}}}},{"$set":{"avg_work_hours":{"$cond":[{"$gt":[{"$size":"$flat_hours"},0]},{"$round":[{"$avg":"$flat_hours"},2]},None]}}},{"$unset":"flat_hours"},{"$sort":{"department":1}}]
    rows=list(employees.aggregate(pipeline))
    return {"month":month,"items":rows}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    first,last=month_match(month)
    pipeline=[{"$match":{"date":{"$gte":first.isoformat(),"$lte":last.isoformat()},"late_minutes":{"$gt":0}}},{"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},{"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},{"$unwind":"$employee"}]
    if department is not None:pipeline.append({"$match":{"employee.department":department}})
    pipeline += [{"$set":{"emp_code":"$_id","name":"$employee.name","department":"$employee.department"}},{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},{"$match":{"rank":{"$lte":limit}}},{"$sort":{"total_late_minutes":-1,"emp_code":1}},{"$project":{"_id":0,"rank":1,"emp_code":1,"name":1,"department":1,"total_late_minutes":1,"late_count":1}}]
    return {"month":month,"items":list(logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_:str=Query(...,alias="from"),to:str=Query(...)):
    start=parse_date(from_); end=parse_date(to)
    if end<start: raise HTTPException(422,"to must not be before from")
    days=(end-start).days+1
    if days>92: raise HTTPException(422,"Range must not exceed 92 days")
    if not employees.find_one({"department":department}): raise HTTPException(404,"Department not found")
    # A MongoDB-generated date spine guarantees a row for every date, even with zero logs.
    spine_start=datetime.combine(start,time.min,UTC)
    pipeline=[
      {"$documents":[{"start":spine_start,"days":days}]},
      {"$project":{"dates":{"$map":{"input":{"$range":[0,"$days"]},"as":"n","in":{"$dateAdd":{"startDate":"$start","unit":"day","amount":"$$n"}}}}}},
      {"$unwind":"$dates"},
      {"$set":{"date":{"$dateToString":{"date":"$dates","format":"%Y-%m-%d","timezone":"UTC"}}}},
      {"$lookup":{"from":"attendance_logs","let":{"day":"$date"},"pipeline":[
        {"$match":{"$expr":{"$eq":["$date","$$day"]}}},
        {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"employee"}},
        {"$unwind":"$employee"}, {"$match":{"employee.department":department}},
        {"$group":{"_id":None,
          "present_count":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1.0]},0.0]}},
          "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}}
      ],"as":"daily"}},
      {"$lookup":{"from":"employees","let":{"day":"$date"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$department",department]},{"$lte":["$joined_on","$$day"]}]}}},{"$count":"n"}],"as":"headcount_result"}},
      {"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$headcount_result.n",0]},0]},
        "present_count":{"$ifNull":[{"$arrayElemAt":["$daily.present_count",0]},0.0]},
        "late_count":{"$ifNull":[{"$arrayElemAt":["$daily.late_count",0]},0]},
        "is_working_day":{"$in":[{"$dayOfWeek":"$dates"},[2,3,4,5,6]]}}},
      {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$round":[{"$divide":["$present_count","$headcount"]},4]},None]}}},
      {"$setWindowFields":{"sortBy":{"date":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
      {"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,
        "moving_avg_7d":{"$cond":[{"$eq":[{"$type":"$moving_avg_7d"},"missing"]},None,{"$round":["$moving_avg_7d",4]}]}}},
      {"$sort":{"date":1}}
    ]
    try:
        rows=list(db.aggregate(pipeline,allowDiskUse=True))
    except PyMongoError as exc:
        raise HTTPException(503,f"Trend aggregation failed: {exc.__class__.__name__}")
    return {"department":department,"items":rows}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),from_:Optional[str]=Query(None,alias="from"),to:Optional[str]=None):
    if endpoint not in {"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}:raise HTTPException(422,"Unsupported endpoint")
    if endpoint=="attendance_list":
        filt={}
        if emp_code:filt["emp_code"]=emp_code
        if date_from:filt.setdefault("date",{})["$gte"]=parse_date(date_from).isoformat()
        if date_to:filt.setdefault("date",{})["$lte"]=parse_date(date_to).isoformat()
        if status:filt["status"]=status
        if date_from and date_to and date_from>date_to:raise HTTPException(422,"Invalid date range")
        coll=logs; operation={"find":"attendance_logs","filter":filt,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size}
    elif endpoint=="employee_monthly":
        if not emp_code or not month:raise HTTPException(422,"emp_code and month are required")
        first,last=month_match(month); coll=logs; operation={"aggregate":"attendance_logs","pipeline":[{"$match":{"emp_code":emp_code,"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}}},{"$group":{"_id":"$emp_code","late":{"$sum":"$late_minutes"}}}],"cursor":{}}
    elif endpoint=="department_summary":
        if not month:raise HTTPException(422,"month is required")
        first,last=month_match(month); coll=employees; operation={"aggregate":"employees","pipeline":[{"$match":{"joined_on":{"$lte":last.isoformat()},**({"department":department} if department else {})}},{"$lookup":{"from":"attendance_logs","localField":"emp_code","foreignField":"emp_code","as":"logs"}}],"cursor":{}}
    elif endpoint=="late_leaderboard":
        if not month:raise HTTPException(422,"month is required")
        first,last=month_match(month); coll=logs; operation={"aggregate":"attendance_logs","pipeline":[{"$match":{"date":{"$gte":first.isoformat(),"$lte":last.isoformat()},"late_minutes":{"$gt":0}}},{"$group":{"_id":"$emp_code","total":{"$sum":"$late_minutes"}}}],"cursor":{}}
    else:
        if not department or not from_ or not to:raise HTTPException(422,"department, from and to are required")
        s=parse_date(from_); e=parse_date(to)
        if e<s or (e-s).days+1>92:raise HTTPException(422,"Invalid trend range")
        coll=logs; operation={"aggregate":"attendance_logs","pipeline":[{"$match":{"date":{"$gte":s.isoformat(),"$lte":e.isoformat()}}},{"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"e"}}],"cursor":{}}
    try:
        # explain command returns raw query planner + execution statistics.
        raw=db.command({"explain":operation,"verbosity":"executionStats"})
        return {"endpoint":endpoint,"collection":coll.name,"explain":json_util.loads(json_util.dumps(raw))}
    except PyMongoError as e:raise HTTPException(503,f"MongoDB explain failed: {e.__class__.__name__}")
