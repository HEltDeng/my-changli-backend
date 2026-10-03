"""Minimal client-verified school binding; never accepts school credentials."""
import re
import uuid
from core import ApiError, now_ms

def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS school_bindings(
        id TEXT PRIMARY KEY, installation_id TEXT NOT NULL REFERENCES installations(installation_id) ON DELETE CASCADE,
        user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
        school_id TEXT NOT NULL, student_id TEXT NOT NULL, status TEXT NOT NULL,
        source TEXT NOT NULL, consent_version INTEGER NOT NULL, verified_client_at INTEGER NOT NULL,
        received_at INTEGER NOT NULL, ended_at INTEGER, event_id TEXT NOT NULL,
        UNIQUE(installation_id,event_id))""")
    # SQLite cannot drop NOT NULL in place. Rebuild this one relation in the caller's
    # migration transaction, preserving every row and all constraints; repeat is a no-op.
    columns = list(db.execute("PRAGMA table_info(school_bindings)"))
    if any(c[1] == 'user_id' and c[3] for c in columns):
        db.execute("ALTER TABLE school_bindings RENAME TO school_bindings_v1")
        db.execute("DROP INDEX IF EXISTS school_binding_current")
        db.execute("DROP INDEX IF EXISTS school_binding_student")
        db.execute("""CREATE TABLE school_bindings(
            id TEXT PRIMARY KEY, installation_id TEXT NOT NULL REFERENCES installations(installation_id) ON DELETE CASCADE,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            school_id TEXT NOT NULL, student_id TEXT NOT NULL, status TEXT NOT NULL,
            source TEXT NOT NULL, consent_version INTEGER NOT NULL, verified_client_at INTEGER NOT NULL,
            received_at INTEGER NOT NULL, ended_at INTEGER, event_id TEXT NOT NULL,
            UNIQUE(installation_id,event_id))""")
        db.execute("INSERT INTO school_bindings SELECT * FROM school_bindings_v1")
        db.execute("DROP TABLE school_bindings_v1")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS school_binding_current ON school_bindings(installation_id) WHERE status='ACTIVE'")
    db.execute("CREATE INDEX IF NOT EXISTS school_binding_student ON school_bindings(school_id,student_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS school_binding_operations(
        installation_id TEXT NOT NULL REFERENCES installations(installation_id) ON DELETE CASCADE,
        event_id TEXT NOT NULL, action TEXT NOT NULL, received_at INTEGER NOT NULL,
        PRIMARY KEY(installation_id,event_id))""")

def receive(db,data,user,validate_identity,associate):
    allowed={'installationId','installationSecret','eventId','action','schoolId','studentId','verifiedClientAt','consentVersion','source','afterOperationId'}
    if set(data)-allowed: raise ApiError(422,'INVALID_FIELDS','仅允许学校绑定必要字段，不接收凭据')
    action=data.get('action');iid=data.get('installationId');eid=data.get('eventId','')
    if action not in {'BIND','UNBIND','WITHDRAW'} or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',str(eid)):
        raise ApiError(422,'INVALID_BINDING','绑定操作无效')
    if action=='BIND':
        if user and (user['role']!='USER' or user['account_type'] not in {'PHONE','DEVICE'}):
            raise ApiError(403,'ACCOUNT_REQUIRED','账号不可用于学校关联')
        if data.get('consentVersion')!=1 or data.get('source') not in {'CLIENT_SCHOOL_LOGIN','CLIENT_EXISTING_BINDING'}:
            raise ApiError(422,'CONSENT_REQUIRED','请确认学校学号同步说明')
        if data.get('schoolId')!='csust' or not isinstance(data.get('studentId'),str) or not re.fullmatch(r'[A-Za-z0-9-]{1,32}',data['studentId']):
            raise ApiError(422,'INVALID_STUDENT','学校或学号格式无效')
        if not isinstance(data.get('verifiedClientAt'),int) or not 0<data['verifiedClientAt']<=now_ms()+300000:
            raise ApiError(422,'INVALID_TIME','绑定时间无效')
    uid = user['id'] if user and user['account_type']=='PHONE' else None
    db.execute('BEGIN IMMEDIATE')
    try:
        install=validate_identity(db,data,required=True)
        if install is None or not install["identity_secret_hash"]:
            raise ApiError(409,'INSTALLATION_NOT_ENROLLED','请先建立安装身份')
        receipt=db.execute('SELECT action FROM school_binding_operations WHERE installation_id=? AND event_id=?',(iid,eid)).fetchone()
        if receipt:
            if receipt['action']!=action: raise ApiError(409,'EVENT_CONFLICT','操作编号已使用')
            db.commit();return {'accepted':True,'duplicate':True}
        if action=='BIND':
            acknowledged_end=data.get('afterOperationId') and db.execute('SELECT 1 FROM school_binding_operations WHERE installation_id=? AND event_id=? AND received_at=?',(iid,data.get('afterOperationId'),install['school_sync_ended_at'])).fetchone()
            if install['school_sync_ended_at'] and data['verifiedClientAt']<=install['school_sync_ended_at'] and not acknowledged_end:
                raise ApiError(409,'BINDING_ENDED','解绑或撤回前的事件不能恢复关联')
            if uid is None and install['current_user_id']:
                raise ApiError(403,'ACCOUNT_REQUIRED','当前安装已登录平台账号，请使用有效会话')
            if uid is not None and install['current_user_id']!=uid:
                known=db.execute('SELECT 1 FROM installation_accounts WHERE installation_id=? AND user_id=?',(iid,uid)).fetchone()
                if install['current_user_id'] or not known:
                    raise ApiError(409,'ACCOUNT_CHANGED','安装的当前账号已变化，请重新登录确认')
                associate(db,iid,uid,now_ms(),'VERIFIED_SCHOOL_SYNC')
        prior=db.execute('SELECT * FROM school_bindings WHERE installation_id=? AND event_id=?',(iid,eid)).fetchone()
        if prior:
            if action!='BIND' or prior['user_id']!=uid or prior['student_id']!=data['studentId']:
                raise ApiError(409,'EVENT_CONFLICT','操作编号已使用')
            db.commit();return {'accepted':True,'duplicate':True}
        timestamp=now_ms()
        if action!='BIND':
            db.execute('UPDATE installations SET school_sync_ended_at=? WHERE installation_id=?',(timestamp,iid))
            db.execute('INSERT INTO school_binding_operations VALUES(?,?,?,?)',(iid,eid,action,timestamp))
        if action=='WITHDRAW':
            db.execute('DELETE FROM school_bindings WHERE installation_id=?',(iid,))
        else:
            db.execute("UPDATE school_bindings SET status='ENDED',ended_at=? WHERE installation_id=? AND status='ACTIVE'",(timestamp,iid))
            if action=='BIND':
                db.execute('INSERT INTO school_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (str(uuid.uuid4()),iid,uid,data['schoolId'],data['studentId'],'ACTIVE',data['source'],1,data['verifiedClientAt'],timestamp,None,eid))
        db.commit();return {'accepted':True,'source':'CLIENT_VERIFIED_NOT_OFFICIAL'}
    except Exception:
        db.rollback();raise
