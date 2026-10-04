import base64
import concurrent.futures
import io
import secrets
import uuid
import unittest
from openpyxl import load_workbook
import test_api as existing
server = existing.server


class CampusActivityTest(unittest.TestCase):
    setUpClass = classmethod(existing.ApiTest.setUpClass.__func__)
    tearDownClass = classmethod(existing.ApiTest.tearDownClass.__func__)
    call = existing.ApiTest.call
    device_token = existing.ApiTest.device_token
    admin_token = existing.ApiTest.admin_token

    def setUp(self):
        self.admin=self.admin_token()
        self.teacher=self.publisher('国际工学院')
        self.other=self.publisher('其他学院')

    def publisher(self,college):
        username='teacher-'+secrets.token_hex(5);password=secrets.token_urlsafe(18)
        code,_=self.call('POST','/api/v1/admin/admins',dict(username=username,password=password,nickname='测试老师',adminRole='ACTIVITY_PUBLISHER',activityCollegeId=college,activityPublishScope='SCHOOL'),self.admin)
        self.assertEqual(201,code)
        code,body=self.call('POST','/api/v1/auth/login',dict(username=username,password=password))
        self.assertEqual(200,code)
        return body['data']['token']

    def student(self):
        auth={'installationId':str(uuid.uuid4()),'installationSecret':str(uuid.uuid4())}
        code,body=self.call('POST','/api/v1/auth/device',auth)
        self.assertEqual(200,code,body);token=body['data']['token']
        number='00'+secrets.token_hex(8)
        code,body=self.call('POST','/api/v1/school-binding',{**auth,'eventId':str(uuid.uuid4()),'action':'BIND','schoolId':'csust','studentId':number,'source':'CLIENT_SCHOOL_LOGIN','consentVersion':1,'verifiedClientAt':server.now_ms()},token)
        self.assertEqual(200,code,body)
        code,body=self.call('PUT','/api/v1/activities/profile',{'student_number':number,'student_name':'测试学生','college_id':'国际工学院','source':'CLIENT_SCHOOL_LOGIN','consent':True},token)
        self.assertEqual(200,code,body)
        return token

    def activity(self,**changes):
        now=server.now_ms()
        data=dict(title='【测试】讲座',category='学术讲座',organizer_name='国际工学院',college_id='国际工学院',audience='SCHOOL',location='测试报告厅',summary='测试简介',description='测试说明',start_at=now+3600000,end_at=now+7200000,signup_start_at=now-60000,signup_end_at=now+1800000,capacity=2,status='PUBLISHED',requires_phone=False)
        data.update(changes)
        code,body=self.call('POST','/api/v1/admin/activities',data,self.teacher)
        self.assertEqual(201,code,body)
        return body['data'],data

    def test_complete_flow_and_privacy(self):
        a,_=self.activity();key=a['id'];student=self.student()
        code,b=self.call('POST',f'/api/v1/activities/{key}/register',{},student)
        self.assertEqual(200,code,b);self.assertEqual(1,b['data']['registered_count'])
        self.assertEqual(200,self.call('POST',f'/api/v1/activities/{key}/register',{},student)[0])
        _,b=self.call('GET',f'/api/v1/admin/activities/{key}/registrations',token=self.teacher)
        self.assertEqual(1,len(b['data']['items']));self.assertNotIn('phone',b['data']['items'][0]);self.assertNotIn('user_id',b['data']['items'][0])
        _,b=self.call('GET','/api/v1/activities/mine',token=student);self.assertEqual(key,b['data']['items'][0]['id'])
        self.assertEqual(200,self.call('DELETE',f'/api/v1/activities/{key}/registration',token=student)[0])
        _,b=self.call('GET',f'/api/v1/admin/activities/{key}/registrations',token=self.teacher)
        self.assertEqual(0,b['data']['activity']['registered_count']);self.assertEqual('CANCELLED',b['data']['items'][0]['registration_status'])
        _,b=self.call('GET',f'/api/v1/admin/activities/{key}/registrations/export',token=self.teacher)
        wb=load_workbook(io.BytesIO(base64.b64decode(b['data']['content_base64'])))
        self.assertEqual(['姓名','学号','学院','报名时间','报名状态'],[c.value for c in wb.active[1]])
        self.assertEqual('已取消',wb.active.cell(2,5).value)
        self.assertEqual(200,self.call('POST',f'/api/v1/activities/{key}/register',{},student)[0])
        with server.connection() as db:
            self.assertEqual(1,db.execute('SELECT count(*) FROM activity_registrations WHERE activity_id=?',(key,)).fetchone()[0])

    def test_capacity_race_and_release(self):
        a,_=self.activity(capacity=1);key=a['id'];students=[self.student() for _ in range(8)]
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results=list(pool.map(lambda t:self.call('POST',f'/api/v1/activities/{key}/register',{},t),students))
        self.assertEqual(1,sum(c==200 for c,_ in results));self.assertEqual(7,sum(c==409 for c,_ in results))
        winner=next(t for t,r in zip(students,results) if r[0]==200)
        self.assertEqual(200,self.call('DELETE',f'/api/v1/activities/{key}/registration',token=winner)[0])
        self.assertEqual(200,self.call('POST',f'/api/v1/activities/{key}/register',{},students[(students.index(winner)+1)%8])[0])

    def test_college_and_module_isolation(self):
        a,data=self.activity();key=a['id']
        for suffix in ('','/registrations','/registrations/export'):
            self.assertEqual(404,self.call('GET',f'/api/v1/admin/activities/{key}'+suffix,token=self.other)[0])
        self.assertEqual(404,self.call('PUT',f'/api/v1/admin/activities/{key}',{**data,'college_id':'其他学院','organizer_name':'其他学院'},self.other)[0])
        self.assertEqual(403,self.call('POST','/api/v1/admin/activities',{**data,'college_id':'其他学院'},self.teacher)[0])
        for path in ('admins','users','config','identity-statistics'):
            self.assertEqual(403,self.call('GET','/api/v1/admin/'+path,token=self.teacher)[0])

    def test_phone_consent_and_server_status(self):
        a,_=self.activity(requires_phone=True);key=a['id'];t=self.student()
        self.assertEqual(422,self.call('POST',f'/api/v1/activities/{key}/register',{},t)[0])
        self.assertEqual(422,self.call('POST',f'/api/v1/activities/{key}/register',{'student_name':'伪造'},t)[0])
        self.assertEqual(200,self.call('POST',f'/api/v1/activities/{key}/register',{'phone':'13800000000','phone_confirmed':True},t)[0])
        _,b=self.call('GET',f'/api/v1/admin/activities/{key}/registrations',token=self.teacher)
        self.assertEqual('13800000000',b['data']['items'][0]['phone'])
        with server.connection() as db:
            db.execute('UPDATE activities SET signup_end_at=? WHERE id=?',(server.now_ms()-1,key))
        self.assertEqual(409,self.call('DELETE',f'/api/v1/activities/{key}/registration',token=t)[0])
        _,b=self.call('GET',f'/api/v1/activities/{key}',token=t)
        self.assertEqual('CLOSED',b['data']['state']);self.assertFalse(b['data']['can_cancel'])

    def test_migration_repeat(self):
        server.initialize_database();server.initialize_database()
        with server.connection() as db:
            self.assertEqual(1,db.execute("SELECT count(*) FROM schema_migrations WHERE id='20261004_campus_activities_v1'").fetchone()[0])

    def test_account_erasure_cleans_activity_identity_and_phone(self):
        a,_=self.activity();t=self.student()
        self.assertEqual(200,self.call('POST',f'/api/v1/activities/{a["id"]}/register',{},t)[0])
        _,me=self.call('GET','/api/v1/auth/me',token=t);uid=me['data']['id']
        with server.connection() as db:
            user=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            server.delete_user_account_data(db,user,reason='Synthetic erasure test')
            self.assertEqual(0,db.execute('SELECT count(*) FROM activity_profiles WHERE user_id=?',(uid,)).fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM activity_registrations WHERE user_id=?',(uid,)).fetchone()[0])
            deleted=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            server.purge_deleted_user(db,deleted)

    def test_binding_withdrawal_blocks_registration(self):
        a,_=self.activity();t=self.student()
        _,me=self.call('GET','/api/v1/auth/me',token=t)
        with server.connection() as db:
            db.execute("UPDATE school_bindings SET status='ENDED' WHERE student_id=(SELECT student_number FROM activity_profiles WHERE user_id=?)",(me['data']['id'],))
        self.assertEqual(409,self.call('POST',f'/api/v1/activities/{a["id"]}/register',{},t)[0])

    def test_edit_capacity_and_collection_are_protected(self):
        a,data=self.activity();key=a['id']
        for _ in range(2): self.assertEqual(200,self.call('POST',f'/api/v1/activities/{key}/register',{},self.student())[0])
        self.assertEqual(409,self.call('PUT',f'/api/v1/admin/activities/{key}',{**data,'capacity':1},self.teacher)[0])
        self.assertEqual(409,self.call('PUT',f'/api/v1/admin/activities/{key}',{**data,'requires_phone':True},self.teacher)[0])
        self.assertEqual(200,self.call('PUT',f'/api/v1/admin/activities/{key}',{**data,'latest_update':'测试地点调整'},self.teacher)[0])

    def test_disabled_teacher_and_cancelled_activity(self):
        a,data=self.activity();key=a['id'];t=self.student()
        self.assertEqual(200,self.call('PUT',f'/api/v1/admin/activities/{key}',{**data,'status':'CANCELLED'},self.teacher)[0])
        self.assertEqual(409,self.call('POST',f'/api/v1/activities/{key}/register',{},t)[0])
        _,me=self.call('GET','/api/v1/auth/me',token=self.teacher)
        self.assertEqual(200,self.call('PATCH','/api/v1/admin/admins/'+me['data']['id'],{'enabled':False},self.admin)[0])
        self.assertEqual(401,self.call('GET','/api/v1/admin/activities',token=self.teacher)[0])


    def test_school_organization_path_matches_college_scope(self):
        token=self.student()
        _,identity=self.call('GET','/api/v1/activities/profile',token=token)
        p=identity['data']
        code,_=self.call('PUT','/api/v1/activities/profile',dict(student_number=p['student_number'],student_name=p['student_name'],college_id='本科生/教学机构（大类）/国际工学院',source='CLIENT_SCHOOL_LOGIN',consent=True),token)
        self.assertEqual(200,code)
        _,identity=self.call('GET','/api/v1/activities/profile',token=token)
        self.assertEqual('国际工学院',identity['data']['college_id'])
        a,_=self.activity(audience='COLLEGE')
        self.assertEqual(200,self.call('GET',f'/api/v1/activities/{a["id"]}',token=token)[0])
        self.assertEqual(200,self.call('POST',f'/api/v1/activities/{a["id"]}/register',{},token)[0])


if __name__=='__main__': unittest.main()
