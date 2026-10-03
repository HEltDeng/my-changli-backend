"""Installation observations v2. Existing users/devices/installations stay authoritative.

All dates are epoch milliseconds; calendar windows use the existing UTC+8 business
timezone. Legacy events establish links only when they name an installation.
"""
import hashlib
import hmac
import json
import re
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from core import ApiError, now_ms, BUSINESS_TIMEZONE, business_day_bounds_ms, parse_pagination, paginated_response
from runtime_config import RUNTIME

PROTOCOL = 2
DAY = 86400000
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
    packages={"com.mychangli.app"} if RUNTIME.is_production else {"com.mychangli.app","com.mychangli.app.debug"}
    if data.get("protocolVersion")!=PROTOCOL or data.get("packageName") not in packages:
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
            if previous["withdrawn_at"] and client<=previous["withdrawn_at"]:
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
                first_evidence_at=COALESCE(first_evidence_at,?),protocol_version=2,
                consent_version=1,withdrawn_at=NULL WHERE installation_id=?""",
                (device_id,0 if device_id else 1,received,source,received,effective,iid))
            if kind in {"ACTIVATE","FOREGROUND"} and effective is not None:
                db.execute("UPDATE installations SET foreground_at=MAX(COALESCE(foreground_at,0),?) WHERE installation_id=?",(effective,iid))
            if device_id:
                db.execute("""UPDATE devices SET install_count=(SELECT count(*) FROM installations WHERE device_id=?),
                    last_app_version_name=?,last_app_version_code=?,updated_at=? WHERE id=?""",
                    (device_id,profile["appVersionName"],profile["appVersionCode"],received,device_id))
            if user and user["account_type"]=="PHONE": associate(db,iid,user["id"],received,"VERIFIED_EVENT")
        db.execute("INSERT INTO installation_events VALUES(?,?,?,?,?,?,?,2)",
            (iid,eid,kind,client,received,effective,"VALID" if effective is not None else "CLOCK_INVALID"))
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
    if scope not in {"accounts","guests","installations","devices","legacy"}:
        raise ApiError(422,"INVALID_SCOPE","统计范围无效")
    try: as_of=int(q.get("asOf") or now_ms())
    except (TypeError,ValueError): raise ApiError(422,"INVALID_TIME","统计时间无效")
    if as_of>now_ms()+300000 or as_of<0: raise ApiError(422,"INVALID_TIME","统计时间无效")
    if q.get("activity","") not in {"","today","30d"}: raise ApiError(422,"INVALID_FILTER","活跃范围无效")
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
    for i in installs:
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
                foreground_at=max([i["foreground_at"] or 0 for i in associated],default=0) or None,
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
                "foreground_at":max((i["foreground_at"] or 0 for i in items),default=0) or None})
    else:
        for i in installs:
            if not i["activation_at"] or i["activation_at"]>as_of: continue
            if scope=="guests" and i["ever_registered"]: continue
            rows.append({**i,"id":i["installation_id"],"short_code":code("I",i["installation_id"]),
                "display_name":"游客 · "+model_label(i) if not i["ever_registered"] else model_label(i),
                "association_state":"曾关联注册账号" if i["ever_registered"] else "未关联注册账号"})
    if q.get("snapshot"):
        with _snapshot_lock:
            cached=_snapshots.get(q["snapshot"])
            if not cached or cached["expires"]<now_ms():
                raise ApiError(409,"SNAPSHOT_EXPIRED","统计快照已失效，请刷新")
            if cached["owner"]!=q.get("_admin"):
                raise ApiError(403,"FORBIDDEN","统计快照不属于当前管理员")
            rows=[dict(r) for r in cached["rows"][scope]]
            as_of=cached["asOf"]
    selected=[]
    for row in rows:
        status=q.get("status","")
        if status and row.get("account_status")!=status: continue
        if not status and not q.get("_allStatuses") and row.get("account_status")=="DELETED": continue
        if q.get("deviceId") and row.get("device_id")!=q["deviceId"]: continue
        if q.get("unidentified")=="true" and row.get("device_id"): continue
        if q.get("firstActivation")=="today" and not (
            row.get("activation_source")=="NEW_INSTALL" and start<=(row.get("activation_at") or 0)<end): continue
        activity=q.get("activity")
        if activity:
            threshold=start if activity=="today" else as_of-30*DAY
            if not threshold<=(row.get("foreground_at") or 0)<=as_of: continue
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
        if needle and needle not in " ".join(str(row.get(k) or "") for k in
            ("display_name","short_code","phone","reservation_name","reservation_student_id","brand","model_raw","real_name","student_id","legacy_guest_codes")).lower(): continue
        if advanced and advanced!=row["id"]: continue
        # Never serialize installation proof hashes or raw device keys.
        for key in ("identity_secret_hash","device_key_hash"): row.pop(key,None)
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

def snapshot(db, owner=""):
    at=now_ms()
    definitions={
        "activated":{"scope":"installations"},"devices":{"scope":"devices"},
        "registered":{"scope":"accounts"},"guests":{"scope":"guests"},
        "unidentified":{"scope":"installations","unidentified":"true"},
        "todayActive":{"scope":"installations","activity":"today"},
        "monthActive":{"scope":"installations","activity":"30d"},
        "todayFirst":{"scope":"installations","firstActivation":"today"},
        "legacy":{"scope":"legacy"},
    }
    key=uuid.uuid4().hex
    with _snapshot_lock:
        _snapshots[key]={"owner":owner,"asOf":at,"expires":at+600000,"rows":{
            scope:query_rows(db,{"scope":scope,"asOf":str(at),"_allStatuses":True})[0]
            for scope in ("accounts","guests","installations","devices","legacy")}}
        while len(_snapshots)>16: _snapshots.popitem(last=False)
    return {"asOf":at,"timezone":"Asia/Shanghai","metrics":{
        name:{"count":len(query_rows(db,{**q,"asOf":str(at)})[0]),"filters":{**q,"asOf":str(at),"snapshot":key}}
        for name,q in definitions.items()}}
