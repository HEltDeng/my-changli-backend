"""Installation observations v2. Existing users/devices/installations stay authoritative.

All dates are epoch milliseconds; calendar windows use the existing UTC+8 business
timezone. Legacy events establish links only when they name an installation.
"""
import os
import hashlib
import hmac
import json
import re
import threading
import uuid
import copy
import school_bindings
from collections import OrderedDict
from datetime import datetime
from core import ApiError, now_ms, BUSINESS_TIMEZONE, business_day_bounds_ms, parse_pagination, paginated_response
from runtime_config import RUNTIME

PROTOCOL = 2
DAY = 86400000
# Actual service actions only. Background APP_ACTIVE and administrator actions
# must never become evidence of a person's foreground use.
SERVICE_EVENTS = (
    'LOGIN_SUCCESS', 'USER_REGISTER', 'RESERVATION_CREATED', 'RESERVATION_CANCELLED',
    'LOST_FOUND_PUBLISHED', 'LOST_FOUND_EDITED', 'LOST_FOUND_DELETED',
    'LOST_FOUND_FAVORITED', 'LOST_FOUND_UNFAVORITED', 'LOST_FOUND_CLAIMED',
    'LOST_FOUND_REPORTED', 'RESOURCE_VIEWED', 'RESOURCE_FAVORITED',
    'RESOURCE_UNFAVORITED', 'RESOURCE_COMPLAINT_SUBMITTED', 'FEEDBACK_SUBMITTED',
)
_snapshots = OrderedDict()
_snapshot_lock = threading.RLock()

def invalidate_snapshots():
    with _snapshot_lock:
        _snapshots.clear()

def purge_expired_events(db):
    """Global retention also runs without a new client event."""
    return db.execute("DELETE FROM installation_events WHERE received_at<?",(now_ms()-180*DAY,)).rowcount

def forget_account(db, user_id):
    """Remove optional observations when an associated account is deleted."""
    invalidate_snapshots()
    db.execute('DELETE FROM school_bindings WHERE user_id=?',(user_id,))
    db.execute('UPDATE installation_events SET event_user_id=NULL WHERE event_user_id=?',(user_id,))
    rows=list(db.execute("SELECT i.installation_id,i.device_id FROM installations i JOIN installation_accounts a ON a.installation_id=i.installation_id WHERE a.user_id=?",(user_id,)))
    for row in rows:
        db.execute("""UPDATE installations SET manufacturer=NULL,brand=NULL,model_raw=NULL,
            android_version=NULL,sdk_int=NULL,device_id=NULL,legacy=1,foreground_at=NULL,
            consent_version=NULL,withdrawn_at=?,profile_updated_at=NULL WHERE installation_id=?""",
            (now_ms(),row["installation_id"]))
        db.execute("DELETE FROM installation_events WHERE installation_id=?",(row["installation_id"],))
        if row["device_id"] and not db.execute("SELECT 1 FROM installations WHERE device_id=?",(row["device_id"],)).fetchone():
            db.execute("DELETE FROM devices WHERE id=?",(row["device_id"],))
    db.execute("UPDATE installations SET current_user_id=NULL WHERE current_user_id=?",(user_id,))
    db.execute("DELETE FROM installation_accounts WHERE user_id=?",(user_id,))

def migrate(db):
    def add(table, name, definition):
        if name not in {r["name"] for r in db.execute("PRAGMA table_info(" + table + ")")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    db.execute("BEGIN IMMEDIATE")
    try:
        for name, definition in {
            "identity_secret_hash":"TEXT", "activation_at":"INTEGER",
            "activation_source":"TEXT NOT NULL DEFAULT 'SERVICE_ONLY'",
            "first_received_at":"INTEGER", "first_evidence_at":"INTEGER",
            "foreground_at":"INTEGER", "profile_updated_at":"INTEGER",
            "protocol_version":"INTEGER NOT NULL DEFAULT 1",
            "manufacturer":"TEXT", "brand":"TEXT", "model_raw":"TEXT",
            "android_version":"TEXT", "sdk_int":"INTEGER",
            "consent_version":"INTEGER", "withdrawn_at":"INTEGER",
            "ever_registered":"INTEGER NOT NULL DEFAULT 0",
            "current_user_id":"TEXT REFERENCES users(id) ON DELETE SET NULL",
            "anonymous_user_id":"TEXT REFERENCES users(id) ON DELETE SET NULL",
            "is_test":"INTEGER NOT NULL DEFAULT 0",
            "device_identity_state":"TEXT NOT NULL DEFAULT 'UNCONFIRMED'",
            "school_sync_ended_at":"INTEGER",
        }.items(): add("installations",name,definition)
        for name in ("display_model", "model_mapping_source", "admin_note"):
            add("devices",name,"TEXT")
        add("users","admin_note","TEXT NOT NULL DEFAULT ''")
        add("users","phone_verified_at","INTEGER")
        add("sessions","installation_id","TEXT")
        db.execute("""CREATE TABLE IF NOT EXISTS installation_accounts(
            installation_id TEXT NOT NULL REFERENCES installations(installation_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            first_linked_at INTEGER NOT NULL,last_linked_at INTEGER NOT NULL,
            source TEXT NOT NULL,PRIMARY KEY(installation_id,user_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS installation_events(
            installation_id TEXT NOT NULL REFERENCES installations(installation_id) ON DELETE CASCADE,
            event_id TEXT NOT NULL,kind TEXT NOT NULL,client_at INTEGER NOT NULL,
            received_at INTEGER NOT NULL,effective_at INTEGER,time_quality TEXT NOT NULL,
            protocol_version INTEGER NOT NULL,PRIMARY KEY(installation_id,event_id))""")
        add("installation_events","event_user_id","TEXT REFERENCES users(id) ON DELETE SET NULL")
        add("installation_events","event_device_id","TEXT")
        school_bindings.migrate(db)
        db.execute("CREATE INDEX IF NOT EXISTS installation_events_time ON installation_events(effective_at,kind)")
        db.execute("CREATE INDEX IF NOT EXISTS installation_accounts_user ON installation_accounts(user_id)")
        db.execute("CREATE TABLE IF NOT EXISTS identity_migrations(version INTEGER PRIMARY KEY,applied_at INTEGER NOT NULL)")
        if not db.execute("SELECT 1 FROM identity_migrations WHERE version=2").fetchone():
            # Only existing report rows are backfilled; no invented models or device joins.
            db.execute("""UPDATE installations SET activation_at=first_seen_at,
                activation_source='LEGACY_REPORT',first_received_at=first_seen_at,
                first_evidence_at=first_seen_at""")
            db.execute("""INSERT OR IGNORE INTO installation_accounts
                SELECT e.installation_id,e.user_id,MIN(e.created_at),MAX(e.created_at),'LEGACY_EVENT'
                FROM user_behavior_events e JOIN installations i ON i.installation_id=e.installation_id
                JOIN users u ON u.id=e.user_id WHERE u.account_type='PHONE' AND u.role='USER'
                AND u.account_status!='DELETED' AND e.result='SUCCESS'
                GROUP BY e.installation_id,e.user_id""")
            db.execute("""UPDATE installations SET ever_registered=1 WHERE EXISTS(
                SELECT 1 FROM installation_accounts a WHERE a.installation_id=installations.installation_id)""")
            db.execute("INSERT INTO identity_migrations VALUES(2,?)",(now_ms(),))
        db.commit()
    except Exception:
        db.rollback()
        raise

def secret_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()

def validate_identity(db, data, *, required=False):
    iid = str(data.get("installationId") or "").strip()
    if not iid or len(iid)>100:
        raise ApiError(422,"INVALID_INSTALLATION_ID","安装标识无效")
    secret = str(data.get("installationSecret") or "")
    row = db.execute("SELECT * FROM installations WHERE installation_id=?",(iid,)).fetchone()
    protected = row and row["identity_secret_hash"]
    if protected and (not secret or not hmac.compare_digest(protected,secret_hash(secret))):
        raise ApiError(403,"INSTALLATION_PROOF_REQUIRED","安装凭证不匹配")
    if (required or secret) and not re.fullmatch(r"[0-9a-fA-F-]{36,128}",secret):
        raise ApiError(422,"INVALID_INSTALLATION_SECRET","安装凭证格式无效")
    return row

def protect_identity(db,data):
    secret=str(data.get("installationSecret") or "")
    if secret:
        db.execute("""UPDATE installations SET identity_secret_hash=COALESCE(identity_secret_hash,?)
            WHERE installation_id=?""",(secret_hash(secret),data["installationId"]))

def associate(db,iid,user_id,at,source="AUTHENTICATED"):
    user=db.execute("SELECT account_type,account_status,role FROM users WHERE id=?",(user_id,)).fetchone()
    if not user or user["role"]!="USER" or user["account_status"]=="DELETED": return
    if user["account_type"]=="PHONE":
        db.execute("""INSERT INTO installation_accounts VALUES(?,?,?,?,?)
            ON CONFLICT(installation_id,user_id) DO UPDATE SET
            last_linked_at=MAX(last_linked_at,excluded.last_linked_at)""",(iid,user_id,at,at,source))
        db.execute("UPDATE installations SET ever_registered=1,current_user_id=? WHERE installation_id=?",(user_id,iid))
    else:
        db.execute("UPDATE installations SET anonymous_user_id=?,current_user_id=NULL WHERE installation_id=?",(user_id,iid))

def bind_session(db,token,iid):
    if iid:
        db.execute("UPDATE sessions SET installation_id=? WHERE token_hash=?",(iid,secret_hash(token)))

def receive(db,data,user,device_key_hash):
    """Requires installation proof; account linkage comes only from validated bearer."""
    previous=validate_identity(db,data,required=True)
    if previous is None or not previous["identity_secret_hash"]:
        raise ApiError(409,"INSTALLATION_NOT_ENROLLED","请先建立安装服务身份")
    kind=str(data.get("kind",""))
    if kind not in {"ACTIVATE","FOREGROUND","PROFILE","WITHDRAW"}:
        raise ApiError(422,"INVALID_EVENT","事件类型无效")
    eid=str(data.get("eventId") or "")
    if not re.fullmatch(r"[a-zA-Z0-9-]{16,80}",eid):
        raise ApiError(422,"INVALID_EVENT_ID","事件编号无效")
    packages=({"com.mychangli.app.candidate", "com.mychangli.app.debug"} if os.environ.get("MY_CHANGLI_CANDIDATE_ONLY") == "1" else ({"com.mychangli.app"} if RUNTIME.is_production else {"com.mychangli.app","com.mychangli.app.debug"}))
    if data.get("protocolVersion") not in {2,3} or data.get("packageName") not in packages:
        raise ApiError(422,"INVALID_PROTOCOL","上报协议无效")
    iid=data["installationId"]
    received=now_ms()
    try: client=int(data.get("clientAt"))
    except (TypeError,ValueError): raise ApiError(422,"INVALID_TIME","事件时间无效")
    if not 0<=client<=9223372036854775807:
        raise ApiError(422,"INVALID_TIME","事件时间无效")
    effective=client if received-30*DAY<=client<=received+300000 else None
    if effective is not None: effective=min(effective,received)
    # Validate all profile data before any mutation.
    profile={}
    if kind!="WITHDRAW":
        if data.get("consentVersion")!=1:
            raise ApiError(422,"CONSENT_REQUIRED","需要设备统计专项同意")
        for key in ("manufacturer","brand","modelRaw","androidVersion","appVersionName"):
            value=data.get(key)
            if value is not None and (not isinstance(value,str) or len(value)>100):
                raise ApiError(422,"INVALID_PROFILE","设备资料格式无效")
            profile[key]=(value or "").strip() or None
        for key in ("sdkInt","appVersionCode"):
            value=data.get(key)
            if not isinstance(value,int) or isinstance(value,bool) or not 1<=value<=1000000:
                raise ApiError(422,"INVALID_PROFILE","版本格式无效")
            profile[key]=value
        scoped=data.get("deviceScopedId")
        if scoped and not re.fullmatch("[0-9a-f]{64}",str(scoped)):
            raise ApiError(422,"INVALID_DEVICE_KEY","设备辅助标识无效")
    if data.get("protocolVersion")==3 and data.get("accountIdAtEvent") != (user["id"] if user and user["account_type"]=="PHONE" else None):
        # A delayed event from account A must never become activity of account B.
        user=None
    db.execute("BEGIN IMMEDIATE")
    try:
        previous=validate_identity(db,data,required=True)
        event=db.execute("SELECT kind,client_at FROM installation_events WHERE installation_id=? AND event_id=?",(iid,eid)).fetchone()
        if event:
            if event["kind"]!=kind or event["client_at"]!=client:
                raise ApiError(409,"EVENT_CONFLICT","事件编号已使用")
            db.commit()
            return {"accepted":True,"duplicate":True}
        if kind=="WITHDRAW":
            invalidate_snapshots()
            device=previous["device_id"]
            db.execute("""UPDATE installations SET withdrawn_at=?,consent_version=NULL,
                manufacturer=NULL,brand=NULL,model_raw=NULL,android_version=NULL,sdk_int=NULL,
                foreground_at=NULL,device_id=NULL,legacy=1,profile_updated_at=NULL
                WHERE installation_id=?""",(received,iid))
            db.execute("DELETE FROM installation_events WHERE installation_id=?",(iid,))
            # Keep minimal activation evidence; optional device/profile/activity data is removed.
            if device and not db.execute("SELECT 1 FROM installations WHERE device_id=?",(device,)).fetchone():
                db.execute("DELETE FROM devices WHERE id=?",(device,))
        else:
            acknowledged_withdrawal = data.get("afterWithdrawalEventId") and db.execute(
                "SELECT 1 FROM installation_events WHERE installation_id=? AND event_id=? AND kind='WITHDRAW' AND received_at=?",
                (iid,data.get("afterWithdrawalEventId"),previous["withdrawn_at"])).fetchone()
            if previous["withdrawn_at"] and client<=previous["withdrawn_at"] and not (kind=="ACTIVATE" and acknowledged_withdrawal):
                raise ApiError(409,"CONSENT_WITHDRAWN","撤回前的事件不能恢复采集")
            if previous["withdrawn_at"] and kind!="ACTIVATE":
                raise ApiError(409,"CONSENT_WITHDRAWN","需要重新同意后激活")
            device_id=previous["device_id"]
            scoped=data.get("deviceScopedId")
            identity_state=previous["device_identity_state"]
            if device_id and scoped:
                known=db.execute("SELECT device_key_hash FROM devices WHERE id=?",(device_id,)).fetchone()
                if known and not hmac.compare_digest(known[0],device_key_hash(scoped)):
                    device_id=None
                    identity_state="CHANGED"
            if not scoped:
                device_id=None
                if identity_state!="CHANGED": identity_state="MISSING"
            if scoped and not device_id and identity_state!="CHANGED":
                key=device_key_hash(scoped)
                found=db.execute("SELECT id FROM devices WHERE device_key_hash=?",(key,)).fetchone()
                if found: device_id=found["id"]
                else:
                    import uuid
                    device_id=str(uuid.uuid4())
                    db.execute("""INSERT INTO devices(id,device_key_hash,first_seen_at,last_seen_at,
                        created_at,updated_at) VALUES(?,?,?,?,?,?)""",(device_id,key,received,received,received,received))
            if device_id: identity_state="OBSERVED"
            db.execute("UPDATE installations SET device_identity_state=? WHERE installation_id=?",(identity_state,iid))
            source=previous["activation_source"]
            if not previous["activation_at"]:
                first_install=data.get("firstInstallAt")
                source="NEW_INSTALL" if (data.get("installationOrigin")=="NEW" and
                    isinstance(first_install,int) and received-DAY<=first_install<=received+300000) else "UPGRADE_BACKFILL"
            # Older profiles must not overwrite newer facts.
            profile_at=effective if effective is not None else received
            if not previous["profile_updated_at"] or profile_at>=previous["profile_updated_at"]:
                db.execute("""UPDATE installations SET manufacturer=?,brand=?,model_raw=?,
                    android_version=?,sdk_int=?,last_app_version_name=?,last_app_version_code=?,
                    profile_updated_at=? WHERE installation_id=?""",
                    (profile["manufacturer"],profile["brand"],profile["modelRaw"],profile["androidVersion"],
                     profile["sdkInt"],profile["appVersionName"],profile["appVersionCode"],profile_at,iid))
            db.execute("""UPDATE installations SET device_id=?,legacy=?,activation_at=COALESCE(activation_at,?),
                activation_source=?,first_received_at=COALESCE(first_received_at,?),
                first_evidence_at=COALESCE(first_evidence_at,?),protocol_version=MAX(protocol_version,?),
                consent_version=1,withdrawn_at=NULL WHERE installation_id=?""",
                (device_id,0 if device_id else 1,received,source,received,effective,data['protocolVersion'],iid))
            if kind in {"ACTIVATE","FOREGROUND"} and effective is not None:
                db.execute("UPDATE installations SET foreground_at=MAX(COALESCE(foreground_at,0),?) WHERE installation_id=?",(effective,iid))
            if device_id:
                db.execute("""UPDATE devices SET install_count=(SELECT count(*) FROM installations WHERE device_id=?),
                    last_app_version_name=?,last_app_version_code=?,updated_at=? WHERE id=?""",
                    (device_id,profile["appVersionName"],profile["appVersionCode"],received,device_id))
            if user and user["account_type"]=="PHONE": associate(db,iid,user["id"],received,"VERIFIED_EVENT")
        db.execute("""INSERT INTO installation_events(installation_id,event_id,kind,client_at,received_at,effective_at,time_quality,protocol_version,event_user_id,event_device_id)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (iid,eid,kind,client,received,effective,"VALID" if effective is not None else "CLOCK_INVALID",data["protocolVersion"],
             user["id"] if data["protocolVersion"]==3 and user and user["account_type"]=="PHONE" else None,
             device_id if kind!="WITHDRAW" else None))
        purge_expired_events(db)
        db.commit()
        return {"accepted":True,"duplicate":False,"timeQuality":"VALID" if effective is not None else "CLOCK_INVALID"}
    except Exception:
        db.rollback()
        raise

def model_label(row):
    d=dict(row)
    return d.get("display_model") or " ".join(dict.fromkeys(v for v in (d.get("brand") or d.get("manufacturer"),d.get("model_raw")) if v)) or "型号未上报"

def code(prefix,value):
    return prefix+"-"+hashlib.sha256(str(value).encode()).hexdigest()[:8].upper()

def query_rows(db,q):
    """One selection pipeline feeds cards, pages, and exports."""
    scope=q.get("scope","accounts")
    if scope not in {"accounts","guests","installations","devices","legacy","students"}:
        raise ApiError(422,"INVALID_SCOPE","统计范围无效")
    try: as_of=int(q.get("asOf") or now_ms())
    except (TypeError,ValueError): raise ApiError(422,"INVALID_TIME","统计时间无效")
    if as_of>now_ms()+300000 or as_of<0: raise ApiError(422,"INVALID_TIME","统计时间无效")
    if q.get("activity","") not in {"","5m","today","7d","30d","month","day"}: raise ApiError(422,"INVALID_FILTER","活跃范围无效")
    if q.get("activitySource", "foreground") not in {"foreground", "account", "service", "login"}:
        raise ApiError(422,"INVALID_FILTER","使用记录来源无效")
    start,end=business_day_bounds_ms(datetime.fromtimestamp(as_of/1000,BUSINESS_TIMEZONE).date())
    rows=[]
    installs=[dict(r) for r in db.execute("""SELECT i.*,d.display_model,d.model_mapping_source,d.admin_note AS device_note
        FROM installations i LEFT JOIN devices d ON d.id=i.device_id WHERE i.created_at<=? AND i.is_test=0""",(as_of,))]
    links={}
    for r in db.execute("SELECT * FROM installation_accounts WHERE first_linked_at<=?",(as_of,)):
        links.setdefault(r["user_id"],[]).append(r["installation_id"])
    if q.get("userId"):
        installs=[i for i in installs if i["installation_id"] in links.get(q["userId"],[])]
    active={}
    for r in db.execute("""SELECT installation_id,MAX(effective_at) at FROM installation_events
        WHERE kind IN ('ACTIVATE','FOREGROUND') AND effective_at<=? AND received_at<=?
        GROUP BY installation_id""",(as_of,as_of)):
        active[r["installation_id"]]=r["at"]
    events=[dict(r) for r in db.execute("""SELECT e.* FROM installation_events e JOIN installations i ON i.installation_id=e.installation_id
        WHERE kind IN ('ACTIVATE','FOREGROUND') AND effective_at<=? AND received_at<=? AND i.is_test=0""",(as_of,as_of))]
    event_times={}
    account_times={}
    device_times={}
    service_times={}
    account_service_times={}
    login_times={}
    service_events=db.execute("""SELECT e.user_id,e.installation_id,e.created_at,e.event_type,u.account_type
        FROM user_behavior_events e JOIN users u ON u.id=e.user_id
        LEFT JOIN installations i ON i.installation_id=e.installation_id
        WHERE u.role='USER' AND u.account_status!='DELETED' AND e.result='SUCCESS'
        AND e.client_source='APP' AND e.created_at<=? AND COALESCE(i.is_test,0)=0
        AND e.event_type IN ("""+','.join('?' for _ in SERVICE_EVENTS)+')', (as_of,*SERVICE_EVENTS))
    for e in service_events:
        if e['installation_id']:
            service_times.setdefault(e['installation_id'],[]).append(e['created_at'])
        if e['account_type']=='PHONE':
            account_service_times.setdefault(e['user_id'],[]).append(e['created_at'])
            if e['event_type']=='LOGIN_SUCCESS':
                login_times.setdefault(e['user_id'],[]).append(e['created_at'])
    for e in events:
        event_times.setdefault(e["installation_id"],[]).append(e["effective_at"])
        if e["event_user_id"]: account_times.setdefault(e["event_user_id"],[]).append(e["effective_at"])
        did=e["event_device_id"]
        if not did and e["protocol_version"]==2:
            # Existing v2 device identity is usable; no account identity is inferred.
            did=next((i["device_id"] for i in installs if i["installation_id"]==e["installation_id"]),None)
        if did: device_times.setdefault(did,[]).append(e["effective_at"])
    for i in installs:
        i["_activity"]=event_times.get(i["installation_id"],[])
        i['_service_activity']=service_times.get(i['installation_id'],[])
        i['last_service_at']=max(i['_service_activity'],default=None)
        i["foreground_at"]=active.get(i["installation_id"])
        i["model_label"]=model_label(i)
    if scope in {"accounts","legacy"}:
        users=db.execute("""SELECT id,user_code,account_type,nickname,phone_masked,phone_encrypted,
            reservation_name,reservation_student_id,reservation_profile_consented_at,
            account_status,created_at,admin_note,campus_verified,student_id,real_name,
            phone_verified_at FROM users WHERE role='USER' AND account_type=? AND created_at<=?""",
            ("PHONE" if scope=="accounts" else "DEVICE",as_of))
        for u in users:
            row=dict(u); associated=[i for i in installs if i["installation_id"] in links.get(u["id"],[])]
            newest=max(associated,key=lambda i:i["profile_updated_at"] or 0,default={})
            row.update({k:newest.get(k) for k in ("brand","model_raw","model_label","android_version","last_app_version_name","last_app_version_code")})
            row.update(id=u["id"],display_name=u["nickname"] if scope=="accounts" else "游客 · 型号未上报",
                short_code=u["user_code"],first_evidence_at=min([i["first_evidence_at"] for i in associated if i["first_evidence_at"]],default=None),
                foreground_at=max(account_times.get(u["id"],[]),default=None), _activity=account_times.get(u["id"],[]),
                _account_activity=account_times.get(u['id'],[])+account_service_times.get(u['id'],[]),
                _login_activity=login_times.get(u['id'],[]),
                last_service_at=max(account_service_times.get(u['id'],[]),default=None),
                last_login_at=max(login_times.get(u['id'],[]),default=None),
                _installation_ids=[i["installation_id"] for i in associated],
                installation_count=len(associated),device_count=len({i["device_id"] for i in associated if i["device_id"]}),
                association_state="已验证安装关联" if associated else "历史关联未确认")
            rows.append(row)
    elif scope=="devices":
        grouped={}
        for i in installs:
            if i["device_id"] and i["activation_at"] and i["activation_at"]<=as_of:
                grouped.setdefault(i["device_id"],[]).append(i)
        for did,items in grouped.items():
            newest=max(items,key=lambda i:i["profile_updated_at"] or 0)
            rows.append({**newest,"id":did,"short_code":code("D",did),"display_name":model_label(newest),
                "installation_count":len(items),"first_evidence_at":min(i["first_evidence_at"] or i["first_seen_at"] for i in items),
                "foreground_at":max(device_times.get(did,[]),default=None),"_activity":device_times.get(did,[]),
                "_installation_ids":[i["installation_id"] for i in items]})
    elif scope!="students":
        for i in installs:
            if not q.get('_includeService') and q.get('includeService')!='true' and (not i["activation_at"] or i["activation_at"]>as_of): continue
            if scope=="guests" and i["ever_registered"]: continue
            rows.append({**i,"id":i["installation_id"],"short_code":code("I",i["installation_id"]),
                "display_name":"游客 · "+model_label(i) if not i["ever_registered"] else model_label(i),
                "association_state":"曾关联注册账号" if i["ever_registered"] else "未关联注册账号"})
    # Enrich every row from exact installation-account links, never model/IP matches.
    users={r["id"]:dict(r) for r in db.execute("SELECT id,nickname,user_code,phone_encrypted,account_status FROM users WHERE role='USER' AND account_type='PHONE'")}
    bindings=[dict(r) for r in db.execute("SELECT b.* FROM school_bindings b JOIN installations i ON i.installation_id=b.installation_id WHERE b.received_at<=? AND i.is_test=0",(as_of,))]
    if scope=="students":
        grouped={}
        for b in bindings:
            if b["status"]=="ACTIVE" and users.get(b["user_id"],{}).get("account_status")!="DELETED":
                grouped.setdefault((b["school_id"],b["student_id"]),[]).append(b)
        for (school,student),bs in grouped.items():
            rows.append({"id":school+":"+student,"display_name":student,"short_code":school,
                "school_id":school,"bound_student_id":student,"_installation_ids":[b["installation_id"] for b in bs],
                "_activity":[],"first_evidence_at":min(b["received_at"] for b in bs)})
    for row in rows:
        ids=row.get("_installation_ids") or ([row["installation_id"]] if row.get("installation_id") else [])
        row["_installation_ids"]=ids
        associations=[]
        for uid,iids in links.items():
            if uid not in users or users[uid]["account_status"]=="DELETED" or not set(ids).intersection(iids): continue
            assoc=dict(users[uid])
            assoc["current"]=any(i["installation_id"] in ids and i["current_user_id"]==uid for i in installs)
            related=[dict(b) for b in bindings if b["user_id"]==uid and b["installation_id"] in ids]
            for b in related:
                b["current"]=b["status"]=="ACTIVE" and any(i["installation_id"]==b["installation_id"] and i["current_user_id"]==uid for i in installs)
                b["conflict"]=len({x["user_id"] for x in bindings if x["school_id"]==b["school_id"] and x["student_id"]==b["student_id"] and x["status"]=="ACTIVE"})>1
            assoc["school_bindings"]=related
            associations.append(assoc)
        if scope=="accounts":
            # Keep only this account's school details in its row.
            associations=[a for a in associations if a["id"]==row["id"]]
        row["associated_accounts"]=associations
        current_bindings=[b for a in associations for b in a["school_bindings"] if b["current"] or (scope=="accounts" and b["status"]=="ACTIVE")]
        row["school_bindings"]=[b for a in associations for b in a["school_bindings"]]
        if scope != "accounts":
            anonymous = [dict(b, current=b["status"]=="ACTIVE", conflict=len({(x["user_id"] or x["installation_id"]) for x in bindings if x["school_id"]==b["school_id"] and x["student_id"]==b["student_id"] and x["status"]=="ACTIVE"})>1) for b in bindings if b["user_id"] is None and b["installation_id"] in ids]
            row["school_bindings"].extend(anonymous)
            current_bindings.extend(b for b in anonymous if b["current"])

        # Multiple current installations can carry different school identities. Show
        # the most recently evidenced one without hiding the other associations.
        current_bindings.sort(key=lambda b:(b["received_at"],b["id"]), reverse=True)
        row["bound_student_id"]=row.get("bound_student_id") or (current_bindings[0]["student_id"] if current_bindings else None)
        row["school_id"]=row.get("school_id") or (current_bindings[0]["school_id"] if current_bindings else None)
        row["platform_label"]=" / ".join(a["nickname"]+" · "+(a["user_code"] or "") for a in associations)
        if row["bound_student_id"]: row["display_name"]=row["bound_student_id"]
        elif scope not in {"accounts","legacy","students"}:
            current=next((a for a in associations if a["current"]),None)
            if current: row["display_name"]=current["nickname"]
            elif not associations and scope=="devices": row["display_name"]="游客 · "+model_label(row)
    if q.get("snapshot"):
        with _snapshot_lock:
            cached=_snapshots.get(q["snapshot"])
            if not cached or cached["expires"]<now_ms():
                raise ApiError(409,"SNAPSHOT_EXPIRED","统计快照已失效，请刷新")
            if cached["owner"]!=q.get("_admin"):
                raise ApiError(403,"FORBIDDEN","统计快照不属于当前管理员")
            rows=copy.deepcopy(cached["rows"][scope])
            as_of=cached["asOf"]
            start,end=business_day_bounds_ms(datetime.fromtimestamp(as_of/1000,BUSINESS_TIMEZONE).date())
    selected=[]
    for row in rows:
        if scope in {'installations','guests'} and q.get('includeService')!='true' and not q.get('_includeService'):
            if not row.get('activation_at') or row['activation_at']>as_of: continue
        if q.get("userId") and q["userId"] not in [a["id"] for a in row.get("associated_accounts",[])]: continue
        status=q.get("status","")
        if status and row.get("account_status")!=status: continue
        if not status and not q.get("_allStatuses") and row.get("account_status")=="DELETED": continue
        if q.get("deviceId") and row.get("device_id")!=q["deviceId"]: continue
        if q.get("unidentified")=="true" and row.get("device_id"): continue
        if q.get("firstActivation")=="today" and not (
            row.get("activation_source")=="NEW_INSTALL" and start<=(row.get("activation_at") or 0)<end): continue
        if q.get('created'):
            if q['created'] not in {'today','7d'}: raise ApiError(422,'INVALID_FILTER','注册时间范围无效')
            created_start,_=activity_window(q['created'],as_of)
            if not created_start<=row.get('created_at',0)<=as_of: continue
        if q.get('profileReported')=='yes' and not row.get('model_raw'): continue
        if q.get('profileReported')=='no' and row.get('model_raw'): continue
        activity=q.get("activity")
        if activity:
            threshold,until=activity_window(activity,as_of,q.get("day"))
            field={'foreground':'_activity','account':'_account_activity','service':'_service_activity','login':'_login_activity'}[q.get('activitySource','foreground')]
            if not any(threshold<=t<=until for t in row.get(field,[])): continue
        if q.get("profile")=="complete" and not (row.get("reservation_name") and row.get("reservation_student_id")): continue
        if q.get("profile")=="missing" and row.get("reservation_name") and row.get("reservation_student_id"): continue
        if any(q.get(key) and q[key].lower() not in str(row.get(field) or "").lower() for key,field in
            (("brand","brand"),("model","model_raw"),("android","android_version"),("app","last_app_version_name"))): continue
        selected.append(row)
    return selected,as_of

def listing(db,q,decrypt_phone):
    q=dict(q)
    if not q.get("snapshot") and q.get("_admin"):
        captured=snapshot(db,q["_admin"])
        q["snapshot"]=captured["metrics"]["registered"]["filters"]["snapshot"]
        q["asOf"]=str(captured["asOf"])
    rows,at=query_rows(db,q)
    needle=str(q.get("q") or "").strip().lower()
    advanced=str(q.get("uuid") or "").strip()
    items=[]
    for row in rows:
        # Legacy GUEST aliases remain searchable for support; never merge by this display code.
        identifiers=[row.get("device_id"),row.get("anonymous_user_id")]
        if row.get("account_type")=="DEVICE": identifiers.append(row["id"])
        row["legacy_guest_codes"]=list(dict.fromkeys("GUEST-"+hashlib.sha256(str(v).encode()).hexdigest()[:10].upper() for v in identifiers if v))
        row["phone"]=decrypt_phone(row.pop("phone_encrypted",None))
        for a in row.get("associated_accounts",[]):
            a["phone"]=decrypt_phone(a.pop("phone_encrypted",None))
        if needle and needle not in " ".join(str(row.get(k) or "") for k in
            ("display_name","short_code","phone","reservation_name","reservation_student_id","brand","model_raw","real_name","student_id","legacy_guest_codes","bound_student_id","school_id","platform_label","associated_accounts")).lower(): continue
        if advanced and advanced!=row["id"]: continue
        # Never serialize installation proof hashes or raw device keys.
        for key in ("identity_secret_hash","device_key_hash"): row.pop(key,None)
        for k in list(row):
            if k.startswith("_"): row.pop(k)
        items.append(row)
    items.sort(key=lambda r:(r.get("first_evidence_at") or 0,r["id"]),reverse=True)
    limit,offset=parse_pagination(q,default_limit=50,max_limit=200)
    if q.get("export")=="true":
        result={"items":items,"total":len(items),"limit":len(items),"offset":0}
    else: result=paginated_response(items[offset:offset+limit],len(items),limit,offset)
    result["asOf"]=at
    result["snapshot"]=q.get("snapshot")
    result["timezone"]="Asia/Shanghai"
    return result

def activity_window(activity,at,day=None):
    date=datetime.fromtimestamp(at/1000,BUSINESS_TIMEZONE).date()
    start,end=business_day_bounds_ms(date)
    if activity=="day":
        try: return business_day_bounds_ms(datetime.strptime(day,"%Y-%m-%d").date())[0],min(business_day_bounds_ms(datetime.strptime(day,"%Y-%m-%d").date())[1]-1,at)
        except (TypeError,ValueError): raise ApiError(422,"INVALID_DAY","日期格式无效")
    if activity=="month": start=business_day_bounds_ms(date.replace(day=1))[0]
    elif activity=="7d": start-=6*DAY
    elif activity=="30d": start-=29*DAY
    elif activity=="5m": start=at-300000
    return start,at

def snapshot(db, owner=""):
    # Own a read transaction when called outside an HTTP transaction.
    own=not db.in_transaction
    if own: db.execute("BEGIN")
    try:
        # Establish SQLite's snapshot before taking its timestamp.
        db.execute("SELECT count(*) FROM installations").fetchone()
        at=now_ms()
        scopes=("accounts","guests","installations","devices","legacy","students")
        captured={scope:query_rows(db,{"scope":scope,"asOf":str(at),"_allStatuses":True,"_includeService":True})[0] for scope in scopes}
        coverage=min((t for r in captured['installations'] for t in r.get('_activity',[])),default=None)
        definitions={
            "activated":{"scope":"installations"},"devices":{"scope":"devices"},
            "activatedDevices":{"scope":"devices"},"registered":{"scope":"accounts"},
            "boundStudents":{"scope":"students"},"guests":{"scope":"guests"},
            "unidentified":{"scope":"installations","unidentified":"true"},
            "todayActive":{"scope":"installations","activity":"today"},
            "monthActive":{"scope":"installations","activity":"30d"},
            "todayFirst":{"scope":"installations","firstActivation":"today"},"legacy":{"scope":"legacy"},
        }
        for scope,prefix in (("devices","device"),("accounts","account")):
            for name,window in (("Recent","5m"),("Today","today"),("Week","7d"),("Month","30d"),("CalendarMonth","month")):
                definitions[prefix+name]={"scope":scope,"activity":window}
                if scope=='accounts': definitions[prefix+name]['activitySource']='account'
                definitions['service'+name]={'scope':'installations','includeService':'true','activitySource':'service','activity':window}
        definitions['registeredToday']={'scope':'accounts','created':'today'}
        definitions['registeredWeek']={'scope':'accounts','created':'7d'}
        definitions['loginToday']={'scope':'accounts','activity':'today','activitySource':'login'}
        definitions['profileReported']={'scope':'installations','includeService':'true','profileReported':'yes'}
        definitions['profileMissing']={'scope':'installations','includeService':'true','profileReported':'no'}
        definitions["activeUnidentified"]={"scope":"installations","activity":"today","unidentified":"true"}
        key=uuid.uuid4().hex
        with _snapshot_lock:
            _snapshots[key]={"owner":owner,"asOf":at,"expires":at+600000,"rows":captured}
            while len(_snapshots)>16: _snapshots.popitem(last=False)
        metrics={}
        for name,q in definitions.items():
            filters={**q,"asOf":str(at),"snapshot":key}
            metrics[name]={"count":len(query_rows(db,{**filters,"_admin":owner})[0]),"filters":filters}
        account_coverage=db.execute("""SELECT MIN(e.effective_at) FROM installation_events e JOIN installations i ON i.installation_id=e.installation_id
            WHERE e.protocol_version>=3 AND e.kind IN ('ACTIVATE','FOREGROUND') AND i.is_test=0
            AND e.effective_at<=? AND e.received_at<=?""",(at,at)).fetchone()[0]
        service_coverage=min((t for r in captured['installations'] for t in r.get('_service_activity',[])),default=None)
        account_activity_coverage=min((t for r in captured['accounts'] if r.get('account_status')!='DELETED' for t in r.get('_account_activity',[])),default=None)
        trend=[]
        start,_=business_day_bounds_ms(datetime.fromtimestamp(at/1000,BUSINESS_TIMEZONE).date())
        for offset in range(29,-1,-1):
            begin=start-offset*DAY;end=min(begin+DAY,at+1)
            day=datetime.fromtimestamp(begin/1000,BUSINESS_TIMEZONE).strftime("%Y-%m-%d")
            covered=coverage is not None and coverage<end
            counts={scope:sum(any(begin<=t<end for t in row.get('_account_activity' if scope=='accounts' else '_activity',[])) for row in captured[scope]
                if row.get("account_status")!="DELETED") for scope in ("devices","accounts")}
            trend.append({"day":day,"devices":counts["devices"] if covered else None,
                "accounts":counts["accounts"] if account_activity_coverage is not None and account_activity_coverage<end else None,
                'serviceInstallations':sum(any(begin<=t<end for t in row.get('_service_activity',[])) for row in captured['installations']) if service_coverage is not None and service_coverage<end else None,
                "unidentified":sum(not r.get("device_id") and any(begin<=t<end for t in r.get("_activity",[])) for r in captured["installations"]) if covered else None,
                "filters":{"scope":"devices","activity":"day","day":day,"asOf":str(at),"snapshot":key}})
        return {"asOf":at,"timezone":"Asia/Shanghai","metrics":metrics,"trend":trend,
            "coverage":{"firstTrustedEventAt":coverage,"firstAccountProtocolEventAt":account_coverage,
                        'firstServiceEventAt':service_coverage,'firstAccountActivityAt':account_activity_coverage,
                        "accountAttribution":"成功登录、注册、业务操作或可验证账号的前台事件按平台注册账号去重；匿名安装认证不算注册账号登录",
                        "note":"后端使用按收到成功业务记录的安装去重，不等于人数或完整 App 日活。账号使用另含已验证前台事件；设备前台统计仅覆盖授权并成功上报的设备。未观测到不代表无人使用。近30日按含今日的30个北京时间日。"}}
    finally:
        if own: db.commit()
