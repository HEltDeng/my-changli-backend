"""Campus activities: same SQLite, sessions and administrator roles as the host.

Identity provenance is CLIENT_SCHOOL_LOGIN, not official server attestation.
Registration never accepts editable identity fields. All mutations serialize on
SQLite BEGIN IMMEDIATE, including capacity edits, cancellation and reactivation.
"""
import base64
import hashlib
import io
import re
import uuid
from datetime import datetime, timezone, timedelta

from core import ApiError, now_ms

MIGRATION_ID = "20261004_campus_activities_v1"
CATEGORIES = {"学术讲座", "校园活动", "志愿活动", "比赛竞赛", "文体活动", "其他"}


def migrate(db):
    statements = [
        """CREATE TABLE IF NOT EXISTS activity_profiles(
          user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          student_number TEXT NOT NULL, student_name TEXT NOT NULL, college_id TEXT NOT NULL,
          source TEXT NOT NULL, consented_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          phone_encrypted TEXT)""",
        """CREATE TABLE IF NOT EXISTS activities(
          id TEXT PRIMARY KEY, public_id TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL, category TEXT NOT NULL, organizer_name TEXT NOT NULL,
          college_id TEXT NOT NULL, audience TEXT NOT NULL CHECK(audience IN ('SCHOOL','COLLEGE')),
          location TEXT NOT NULL, summary TEXT NOT NULL, description TEXT NOT NULL,
          latest_update TEXT NOT NULL DEFAULT '', contact TEXT NOT NULL DEFAULT '',
          start_at INTEGER NOT NULL, end_at INTEGER NOT NULL,
          signup_start_at INTEGER NOT NULL, signup_end_at INTEGER NOT NULL,
          capacity INTEGER CHECK(capacity IS NULL OR capacity>0), requires_phone INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL CHECK(status IN ('DRAFT','PUBLISHED','UNLISTED','CANCELLED')),
          created_by TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL, published_at INTEGER,
          checkin_enabled INTEGER NOT NULL DEFAULT 0, checkin_start_at INTEGER, checkin_end_at INTEGER,
          CHECK(signup_start_at<signup_end_at AND signup_end_at<=start_at AND start_at<end_at))""",
        """CREATE TABLE IF NOT EXISTS activity_registrations(
          id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES activities(id),
          user_id TEXT NOT NULL REFERENCES users(id), student_name TEXT NOT NULL,
          student_number TEXT NOT NULL, college_id TEXT NOT NULL, phone_snapshot TEXT,
          registration_status TEXT NOT NULL CHECK(registration_status IN ('ACTIVE','CANCELLED')),
          registered_at INTEGER NOT NULL, cancelled_at INTEGER, updated_at INTEGER NOT NULL,
          checkin_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED', checked_in_at INTEGER,
          UNIQUE(activity_id,user_id), UNIQUE(activity_id,student_number))""",
        "CREATE INDEX IF NOT EXISTS activity_listing ON activities(status,start_at,college_id)",
        "CREATE INDEX IF NOT EXISTS activity_registration_count ON activity_registrations(activity_id,registration_status)",
        "CREATE TABLE IF NOT EXISTS schema_migrations(id TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)",
    ]
    db.execute("BEGIN IMMEDIATE")
    try:
        for sql in statements:
            db.execute(sql)
        columns = {r[1] for r in db.execute("PRAGMA table_info(users)")}
        for name, definition in [("activity_college_id", "TEXT"), ("activity_publish_scope", "TEXT NOT NULL DEFAULT 'COLLEGE'")]:
            if name not in columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(?,?)", (MIGRATION_ID, now_ms()))
        db.commit()
    except Exception:
        db.rollback()
        raise


def text(data, key, maximum=200, required=True):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value.strip()) > maximum or (required and not value.strip()):
        raise ApiError(422, "INVALID_ACTIVITY", f"请检查 {key} 的内容和长度")
    return value.strip()


def profile(db, user):
    row = db.execute("SELECT * FROM activity_profiles WHERE user_id=?", (user["id"],)).fetchone()
    if row is None:
        return None
    result = dict(row)
    # School SSO may return an organization path such as 本科生/教学机构/学院.
    result["college_id"] = next((part.strip() for part in reversed(row["college_id"].split('/')) if part.strip()), "")
    return result


def scope(db, user, activity, admin=False):
    if admin:
        if user["admin_role"] == "SUPER_ADMIN":
            return
        if user["admin_role"] == "ACTIVITY_PUBLISHER" and user["activity_college_id"] == activity["college_id"]:
            return
    else:
        p = profile(db, user)
        registered = db.execute("SELECT 1 FROM activity_registrations WHERE activity_id=? AND user_id=?", (activity["id"], user["id"])).fetchone()
        if registered or (activity["status"] in {"PUBLISHED", "CANCELLED"} and activity["published_at"] is not None and (activity["audience"] == "SCHOOL" or p and p["college_id"] == activity["college_id"])):
            return
    raise ApiError(404, "ACTIVITY_NOT_FOUND", "活动不存在或无权查看")


def get(db, key):
    row = db.execute("SELECT * FROM activities WHERE id=? OR public_id=?", (key,key)).fetchone()
    if row is None:
        raise ApiError(404,"ACTIVITY_NOT_FOUND","活动不存在")
    return row


def serialize(db, a, user, timestamp=None):
    timestamp = now_ms() if timestamp is None else timestamp
    active, cancelled = db.execute("SELECT count(CASE WHEN registration_status='ACTIVE' THEN 1 END),count(CASE WHEN registration_status='CANCELLED' THEN 1 END) FROM activity_registrations WHERE activity_id=?",(a["id"],)).fetchone()
    registration = db.execute("SELECT registration_status FROM activity_registrations WHERE activity_id=? AND user_id=?", (a["id"],user["id"])).fetchone()
    mine = registration[0] if registration else None
    state = a["status"]
    if state == "PUBLISHED":
        state = ("ENDED" if timestamp >= a["end_at"] else "ONGOING" if timestamp >= a["start_at"] else
                 "CLOSED" if timestamp >= a["signup_end_at"] else "UPCOMING" if timestamp < a["signup_start_at"] else
                 "FULL" if a["capacity"] is not None and active >= a["capacity"] else "OPEN")
    labels = {"OPEN":"报名中","FULL":"名额已满","CLOSED":"已截止","UPCOMING":"即将开始","ONGOING":"进行中","ENDED":"已结束","CANCELLED":"已取消","UNLISTED":"已下架","DRAFT":"草稿"}
    result = {k:a[k] for k in ("id","public_id","title","category","organizer_name","college_id","audience","location","summary","description","latest_update","contact","start_at","end_at","signup_start_at","signup_end_at","capacity","status","updated_at")}
    result.update(state=state,state_label=labels[state],registered_count=active,cancelled_count=cancelled,
                  remaining=None if a["capacity"] is None else max(0,a["capacity"]-active),
                  registration_status=mine,requires_phone=bool(a["requires_phone"]),
                  can_register=state=="OPEN" and mine!="ACTIVE",
                  can_cancel=mine=="ACTIVE" and timestamp<a["signup_end_at"] and a["status"]!="CANCELLED",
                  server_now=timestamp,duration_minutes=(a["end_at"]-a["start_at"])//60000,
                  share_url="mychangli://activity/"+a["public_id"])
    return result


def validate(data, user):
    values = {k:text(data,k,limit,required) for k,limit,required in [
        ("title",120,True),("category",30,True),("organizer_name",120,True),("college_id",120,True),
        ("location",200,True),("summary",500,True),("description",12000,True),("latest_update",2000,False),("contact",200,False)]}
    if values["category"] not in CATEGORIES:
        raise ApiError(422,"INVALID_CATEGORY","活动类型无效")
    values["audience"] = data.get("audience","COLLEGE")
    values["status"] = data.get("status","DRAFT")
    if values["audience"] not in {"SCHOOL","COLLEGE"} or values["status"] not in {"DRAFT","PUBLISHED","UNLISTED","CANCELLED"}:
        raise ApiError(422,"INVALID_ACTIVITY","活动范围或状态无效")
    if user["admin_role"] != "SUPER_ADMIN":
        if not user["activity_college_id"] or values["college_id"] != user["activity_college_id"] or values["organizer_name"] != user["activity_college_id"]:
            raise ApiError(403,"COLLEGE_FORBIDDEN","只能发布所属学院的活动")
        if values["audience"]=="SCHOOL" and user["activity_publish_scope"]!="SCHOOL":
            raise ApiError(403,"SCOPE_FORBIDDEN","账号未获全校发布范围授权")
    for key in ("start_at","end_at","signup_start_at","signup_end_at"):
        if type(data.get(key)) is not int or not 0<data[key]<4102444800000:
            raise ApiError(422,"INVALID_TIME","请填写有效的活动和报名时间")
        values[key]=data[key]
    if not values["signup_start_at"]<values["signup_end_at"]<=values["start_at"]<values["end_at"]:
        raise ApiError(422,"INVALID_TIME","报名开始须早于截止，截止不得晚于活动开始，结束须晚于开始")
    cap=data.get("capacity")
    if cap is not None and (type(cap) is not int or not 1<=cap<=1000000):
        raise ApiError(422,"INVALID_CAPACITY","人数须为正整数，留空表示不限")
    if type(data.get("requires_phone",False)) is not bool:
        raise ApiError(422,"INVALID_PHONE_SETTING","联系电话选项无效")
    values.update(capacity=cap,requires_phone=int(data.get("requires_phone",False)))
    return values


def register(db, user, key, data, encrypt, token_hash):
    if set(data)-{"phone","phone_confirmed"}:
        raise ApiError(422,"IDENTITY_READ_ONLY","报名身份由学校绑定资料取得，不允许修改")
    db.execute("BEGIN IMMEDIATE")
    try:
        bound=db.execute("SELECT 1 FROM activity_profiles p JOIN school_bindings b ON b.student_id=p.student_number JOIN sessions s ON s.installation_id=b.installation_id WHERE p.user_id=? AND s.user_id=? AND s.token_hash=? AND b.status='ACTIVE'",(user["id"],user["id"],token_hash)).fetchone()
        if not bound: raise ApiError(409,"SCHOOL_BINDING_REQUIRED","学校绑定已变化，请重新确认报名身份")
        a=get(db,key);scope(db,user,a)
        current=serialize(db,a,user)
        if current["registration_status"]=="ACTIVE":
            db.commit();return current
        if not current["can_register"]:
            raise ApiError(409,"ACTIVITY_"+current["state"],"很遗憾，活动名额刚刚已满。" if current["state"]=="FULL" else current["state_label"])
        p=profile(db,user)
        if not p:
            raise ApiError(409,"SCHOOL_IDENTITY_REQUIRED","请先从已绑定学校账号确认姓名、学号和学院")
        phone=None
        if a["requires_phone"]:
            raw=data.get("phone","")
            if data.get("phone_confirmed") is not True or not isinstance(raw,str) or not re.fullmatch(r"1[3-9]\d{9}",raw):
                raise ApiError(422,"PHONE_CONFIRMATION_REQUIRED","请确认本次活动使用的联系电话")
            phone=encrypt(raw)
            db.execute("UPDATE activity_profiles SET phone_encrypted=? WHERE user_id=?",(phone,user["id"]))
        old=db.execute("SELECT * FROM activity_registrations WHERE activity_id=? AND (user_id=? OR student_number=?)",(a["id"],user["id"],p["student_number"])).fetchone()
        if old and old["user_id"]!=user["id"]:
            raise ApiError(409,"STUDENT_ALREADY_REGISTERED","该学号已通过其他账号报名，请使用原账号管理")
        timestamp=now_ms()
        if old:
            db.execute("UPDATE activity_registrations SET student_name=?,student_number=?,college_id=?,phone_snapshot=?,registration_status='ACTIVE',registered_at=?,cancelled_at=NULL,updated_at=? WHERE id=?",(p["student_name"],p["student_number"],p["college_id"],phone,timestamp,timestamp,old["id"]))
        else:
            db.execute("INSERT INTO activity_registrations(id,activity_id,user_id,student_name,student_number,college_id,phone_snapshot,registration_status,registered_at,updated_at) VALUES(?,?,?,?,?,?,?,'ACTIVE',?,?)",(str(uuid.uuid4()),a["id"],user["id"],p["student_name"],p["student_number"],p["college_id"],phone,timestamp,timestamp))
        result=serialize(db,a,user);db.commit();return result
    except Exception:
        db.rollback();raise


def route(handler, method, path, query, host):
    admin=path.startswith("/api/v1/admin/")
    prefix="/api/v1/admin/activities" if admin else "/api/v1/activities"
    suffix=path[len(prefix):].strip("/")
    with host.connection() as db:
        user=handler.current_user(db,"ADMIN" if admin else "USER")
        if admin:
            host.require_admin_path_access(user,method,path)
        else:
            host.require_account_operation(user)
        if not admin and suffix=="profile":
            if method=="GET":
                p=profile(db,user)
                return (None if not p else {"student_number":p["student_number"],"student_name":p["student_name"],"college_id":p["college_id"],"source":p["source"],"phone":host.decrypt_phone(p["phone_encrypted"]) if p["phone_encrypted"] else None}),200
            if method=="PUT":
                data=handler.body_json()
                if set(data)-{"student_number","student_name","college_id","source","consent"} or data.get("source")!="CLIENT_SCHOOL_LOGIN" or data.get("consent") is not True:
                    raise ApiError(422,"IDENTITY_CONSENT_REQUIRED","请确认活动报名身份信息使用说明")
                values=[text(data,k,120) for k in ("student_number","student_name","college_id")]
                values[2] = next((part.strip() for part in reversed(values[2].split('/')) if part.strip()), "")
                if not values[2]:
                    raise ApiError(422,"INVALID_COLLEGE","学校暂未提供学院")
                if not re.fullmatch(r"[A-Za-z0-9-]{1,32}",values[0]):
                    raise ApiError(422,"INVALID_STUDENT","学号格式无效")
                # Existing binding establishes the same school account. Client provenance
                # remains explicit; never label this as server-verified university identity.
                token_hash=hashlib.sha256(handler.headers.get("Authorization", "")[7:].strip().encode()).hexdigest()
                binding=db.execute("SELECT 1 FROM school_bindings b JOIN sessions s ON b.installation_id=s.installation_id WHERE s.token_hash=? AND s.user_id=? AND b.student_id=? AND b.status='ACTIVE'",(token_hash,user["id"],values[0])).fetchone()
                if not binding:
                    raise ApiError(409,"SCHOOL_BINDING_REQUIRED","请先同步当前账号的学校绑定")
                db.execute("INSERT INTO activity_profiles(user_id,student_number,student_name,college_id,source,consented_at,updated_at) VALUES(?,?,?,?,'CLIENT_SCHOOL_LOGIN',?,?) ON CONFLICT(user_id) DO UPDATE SET student_number=excluded.student_number,student_name=excluded.student_name,college_id=excluded.college_id,source=excluded.source,consented_at=excluded.consented_at,updated_at=excluded.updated_at,phone_encrypted=CASE WHEN activity_profiles.student_number=excluded.student_number THEN activity_profiles.phone_encrypted ELSE NULL END",(user["id"],*values,now_ms(),now_ms()))
                return {"saved":True},200
        if not suffix or suffix=="mine" and not admin:
            if method=="GET":
                # One snapshot for records and counts, including concurrent signups.
                db.execute("BEGIN")
                rows=db.execute("SELECT * FROM activities ORDER BY start_at,id").fetchall()
                items=[]
                for a in rows:
                    try: scope(db,user,a,admin)
                    except ApiError: continue
                    item=serialize(db,a,user)
                    if suffix=="mine" and not item["registration_status"]: continue
                    if suffix!="mine" and not admin and a["status"] not in {"PUBLISHED","CANCELLED"}: continue
                    if query.get("q") and query["q"].lower() not in (a["title"]+a["organizer_name"]).lower(): continue
                    if query.get("college") and a["college_id"]!=query["college"]: continue
                    if query.get("state") and item["state"]!=query["state"]: continue
                    if query.get("from") and str(a["start_at"])<query["from"]: continue
                    if query.get("to") and str(a["start_at"])>query["to"]: continue
                    if query.get("recent")=="true" and not item["can_register"]: continue
                    items.append(item)
                db.commit()
                return {"items":items[:3] if query.get("recent")=="true" else items},200
            if method=="POST" and admin:
                values=validate(handler.body_json(),user)
                timestamp=now_ms();key=str(uuid.uuid4())
                values.update(id=key,public_id=uuid.uuid4().hex,created_by=user["id"],created_at=timestamp,updated_at=timestamp,published_at=timestamp if values["status"]=="PUBLISHED" else None)
                db.execute("INSERT INTO activities("+",".join(values)+") VALUES("+",".join("?" for _ in values)+")",tuple(values.values()))
                host.write_audit(db,user["id"],"ACTIVITY_CREATED","ACTIVITY",key,{"status":values["status"]})
                return serialize(db,get(db,key),user),201
        pieces=suffix.split("/");key=pieces[0]
        if len(pieces)==2 and pieces[1]=="register" and not admin and method=="POST":
            token_hash=hashlib.sha256(handler.headers.get("Authorization", "")[7:].strip().encode()).hexdigest()
            return register(db,user,key,handler.body_json(),host.encrypt_phone,token_hash),200
        if len(pieces)==2 and pieces[1]=="registration" and not admin and method=="DELETE":
            db.execute("BEGIN IMMEDIATE")
            try:
                a=get(db,key);scope(db,user,a);item=serialize(db,a,user)
                if item["registration_status"]=="CANCELLED": db.commit();return item,200
                if not item["can_cancel"]: raise ApiError(409,"CANCELLATION_CLOSED","报名已截止或活动已取消，无法取消报名")
                db.execute("UPDATE activity_registrations SET registration_status='CANCELLED',cancelled_at=?,updated_at=? WHERE activity_id=? AND user_id=?",(now_ms(),now_ms(),a["id"],user["id"]))
                item=serialize(db,a,user);db.commit();return item,200
            except Exception: db.rollback();raise
        if admin and len(pieces)>=2 and pieces[1]=="registrations" and method=="GET":
            db.execute("BEGIN")
            a=get(db,key);scope(db,user,a,True)
            rows=[]
            for r in db.execute("SELECT student_name,student_number,college_id,phone_snapshot,registered_at,registration_status FROM activity_registrations WHERE activity_id=? ORDER BY registered_at",(a["id"],)):
                row={k:r[k] for k in ("student_name","student_number","college_id","registered_at","registration_status")}
                if a["requires_phone"]: row["phone"]=host.decrypt_phone(r["phone_snapshot"]) if r["phone_snapshot"] else None
                rows.append(row)
            result={"activity":serialize(db,a,user),"items":rows};db.commit()
            if len(pieces)==3 and pieces[2]=="export":
                from openpyxl import Workbook
                from openpyxl.styles import Font, Alignment
                wb=Workbook();ws=wb.active;ws.title="报名名单"
                keys=["student_name","student_number","college_id"]+(["phone"] if a["requires_phone"] else [])+["registered_at","registration_status"]
                ws.append([{"student_name":"姓名","student_number":"学号","college_id":"学院","phone":"联系电话","registered_at":"报名时间","registration_status":"报名状态"}[k] for k in keys])
                for r in rows:
                    values=[]
                    for k in keys:
                        v=r.get(k) or ""
                        if k=="registered_at": v=datetime.fromtimestamp(v/1000,timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                        if k=="registration_status": v="已报名" if v=="ACTIVE" else "已取消"
                        values.append(str(v))
                    ws.append(values)
                for row in ws:
                    for c in row:
                        c.data_type="s"  # Untrusted text never becomes an Excel formula.
                        c.font=Font(name="SimSun",size=11,bold=c.row==1)
                        c.alignment=Alignment(vertical="center")
                for col in ws.columns: ws.column_dimensions[col[0].column_letter].width=24
                ws.freeze_panes="A2";ws.auto_filter.ref=ws.dimensions
                output=io.BytesIO();wb.save(output)
                host.write_audit(db,user["id"],"ACTIVITY_EXPORT","ACTIVITY",a["id"],{"count":len(rows)})
                return {"filename":"activity-"+a["public_id"]+".xlsx","content_base64":base64.b64encode(output.getvalue()).decode()},200
            return result,200
        if len(pieces)==1 and method=="GET":
            db.execute("BEGIN");a=get(db,key);scope(db,user,a,admin);result=serialize(db,a,user);db.commit();return result,200
        if len(pieces)==1 and admin and method=="PUT":
            data=handler.body_json();values=validate(data,user)
            db.execute("BEGIN IMMEDIATE")
            try:
                a=get(db,key);scope(db,user,a,True)
                if values["college_id"]!=a["college_id"]:
                    raise ApiError(409,"COLLEGE_IMMUTABLE","发布学院不可更改，请另建活动")
                count=db.execute("SELECT count(*) FROM activity_registrations WHERE activity_id=? AND registration_status='ACTIVE'",(a["id"],)).fetchone()[0]
                if values["capacity"] is not None and values["capacity"]<count: raise ApiError(409,"CAPACITY_TOO_SMALL","人数上限不能小于有效报名人数")
                if count and (values["requires_phone"]!=a["requires_phone"] or values["audience"]!=a["audience"]): raise ApiError(409,"REGISTRATION_SETTINGS_LOCKED","已有报名，不能更改收集字段和可见范围")
                values["updated_at"]=now_ms()
                values["published_at"]=a["published_at"] or (now_ms() if values["status"]=="PUBLISHED" else None)
                db.execute("UPDATE activities SET "+",".join(k+"=?" for k in values)+" WHERE id=?",(*values.values(),a["id"]))
                host.write_audit(db,user["id"],"ACTIVITY_UPDATED","ACTIVITY",a["id"],{"status":values["status"]})
                result=serialize(db,get(db,key),user);db.commit();return result,200
            except Exception: db.rollback();raise
        raise ApiError(404,"NOT_FOUND","接口不存在")
