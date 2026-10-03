"""Synthetic end-to-end school, activity attribution and concurrent snapshot acceptance."""
import secrets
import uuid
import threading
import unittest
from unittest.mock import patch
import test_identity_statistics as existing
server=existing.server
identities=existing.identities

class SchoolActivityTest(existing.IdentityStatisticsTest):
    # Reuse fixtures, not the inherited full suite.
    def school(self,auth,student='001234567',**kw):
        return {**{k:auth[k] for k in ('installationId','installationSecret')},'eventId':str(uuid.uuid4()),
            'action':'BIND','schoolId':'csust','studentId':student,'consentVersion':1,
            'source':'CLIENT_SCHOOL_LOGIN','verifiedClientAt':server.now_ms(),**kw}
    def bind(self,data,token=None):
        return self.call('POST','/api/v1/school-binding',data,token)

    def test_school_proof_consent_leading_zero_and_credential_rejection(self):
        auth,event,guest=self.installation(secrets.token_hex(32));self.report(event,guest)
        _,a=self.account(auth);data=self.school(auth)
        self.assertEqual(403,self.bind(data)[0])
        self.assertEqual(422,self.bind({**data,'password':'synthetic-not-a-real-secret'},a['token'])[0])
        self.assertEqual(422,self.bind({**data,'consentVersion':0},a['token'])[0])
        self.assertEqual(403,self.bind({**data,'installationSecret':str(uuid.uuid4())},a['token'])[0])
        self.assertEqual(200,self.bind(data,a['token'])[0])
        self.assertTrue(self.bind(data,a['token'])[1]['data']['duplicate'])
        row=self.rows('accounts',uuid=a['user']['id'])['items'][0]
        self.assertEqual('001234567',row['bound_student_id'])
        self.assertEqual(1,self.rows('devices',q='001234567')['total'])
        self.assertEqual(1,self.rows('students',q='001234567')['total'])
        self.assertNotIn('password',str(row).lower())

    def test_unbind_retry_does_not_end_a_later_binding(self):
        auth,event,guest=self.installation(secrets.token_hex(32));self.report(event,guest)
        _,a=self.account(auth);self.bind(self.school(auth),a['token'])
        end={k:auth[k] for k in ('installationId','installationSecret')}
        end.update(action='UNBIND',eventId=str(uuid.uuid4()))
        self.assertEqual(200,self.bind(end)[0])
        with server.connection() as db:
            ended=db.execute('SELECT school_sync_ended_at FROM installations WHERE installation_id=?',(auth['installationId'],)).fetchone()[0]
        new=self.school(auth,'009876543',verifiedClientAt=ended+1)
        self.assertEqual(200,self.bind(new,a['token'])[0])
        self.assertTrue(self.bind(end)[1]['data']['duplicate'])
        self.assertEqual('009876543',self.rows('accounts',uuid=a['user']['id'])['items'][0]['bound_student_id'])

    def test_acknowledged_withdrawal_handles_slow_client_clock_without_replay(self):
        auth,e,t=self.installation(secrets.token_hex(32));self.report(e,t)
        end={**e,'kind':'WITHDRAW','eventId':str(uuid.uuid4())}
        self.assertEqual(200,self.report(end,t)[0])
        activation={**e,'protocolVersion':3,'eventId':str(uuid.uuid4()),'clientAt':server.now_ms()-1500}
        self.assertEqual(409,self.report(activation,t)[0])
        activation['afterWithdrawalEventId']=end['eventId']
        self.assertEqual(200,self.report(activation,t)[0])
        self.assertEqual(200,self.report(end,t)[0])
        with server.connection() as db:
            self.assertIsNone(db.execute('SELECT withdrawn_at FROM installations WHERE installation_id=?',(auth['installationId'],)).fetchone()[0])

    def test_conflict_switch_unbind_withdraw_never_merges_or_resurrects(self):
        student='00'+secrets.token_hex(6)
        auth,e,t=self.installation(secrets.token_hex(32));self.report(e,t);_,a=self.account(auth)
        data=self.school(auth,student);self.bind(data,a['token'])
        auth2,e2,t2=self.installation(secrets.token_hex(32));self.report(e2,t2);_,b=self.account(auth2)
        self.bind(self.school(auth2,student),b['token'])
        row=self.rows('students',q=student)['items'][0]
        self.assertEqual(2,len(row['associated_accounts']))
        self.assertTrue(all(x['conflict'] for x in row['school_bindings']))
        self.call('POST','/api/v1/auth/logout',{},a['token'])
        _,c=self.account(auth)
        self.assertEqual(401,self.bind(self.school(auth,student),a['token'])[0])
        # New account has no school association from the previous local binding.
        self.assertIsNone(self.rows('accounts',uuid=c['user']['id'])['items'][0]['bound_student_id'])
        end={k:data[k] for k in ('installationId','installationSecret')};end.update(action='WITHDRAW',eventId=str(uuid.uuid4()))
        self.assertEqual(200,self.bind(end)[0]);self.assertEqual(200,self.bind(end)[0])
        self.assertEqual(409,self.bind({**data,'eventId':str(uuid.uuid4())},c['token'])[0])
        self.assertFalse(self.rows('accounts',uuid=a['user']['id'])['items'][0]['school_bindings'])

    def test_event_actor_retry_day_and_account_activity_are_exact(self):
        auth,e,t=self.installation(secrets.token_hex(32));self.report(e,t);_,a=self.account(auth)
        yesterday=server.now_ms()-identities.DAY
        old={**e,'eventId':str(uuid.uuid4()),'kind':'FOREGROUND','protocolVersion':3,
            'accountIdAtEvent':a['user']['id'],'clientAt':yesterday}
        _,b=self.account(auth)
        self.assertEqual(200,self.report(old,b['token'])[0])
        self.assertEqual(0,self.rows('accounts',uuid=b['user']['id'],activity='today')['total'])
        today={**old,'eventId':str(uuid.uuid4()),'clientAt':server.now_ms(),'accountIdAtEvent':b['user']['id']}
        self.report(today,b['token']);self.report(today,b['token'])
        self.assertEqual(1,self.rows('accounts',uuid=b['user']['id'],activity='today')['total'])
        self.assertEqual(0,self.rows('accounts',uuid=a['user']['id'],activity='today')['total'])
        with server.connection() as db:
            self.assertEqual(1,db.execute('SELECT count(*) FROM installation_events WHERE event_id=?',(today['eventId'],)).fetchone()[0])

    def test_snapshot_during_concurrent_write_cards_trend_pages_export_match(self):
        auth,e,t=self.installation(secrets.token_hex(32));self.report(e,t)
        entered=threading.Event();written=threading.Event();errors=[]
        original=identities.query_rows;once=[False]
        def interleave(db,q):
            result=original(db,q)
            if not once[0]:
                once[0]=True;entered.set();self.assertTrue(written.wait(10))
            return result
        def writer():
            try:
                self.assertTrue(entered.wait(10))
                a,event,token=self.installation(secrets.token_hex(32));self.report(event,token)
            except Exception as ex: errors.append(ex)
            finally: written.set()
        thread=threading.Thread(target=writer);thread.start()
        with patch.object(identities,'query_rows',interleave):
            with server.connection() as db: snap=identities.snapshot(db,'synthetic-snapshot-owner')
        thread.join();self.assertFalse(errors,errors)
        with server.connection() as db:
            for metric in snap['metrics'].values():
                result=identities.listing(db,{**metric['filters'],'_admin':'synthetic-snapshot-owner','export':'true'},server.decrypt_phone)
                self.assertEqual(metric['count'],result['total']);self.assertEqual(result['total'],len(result['items']))
            for day in snap['trend']:
                if day['devices'] is not None:
                    rows,_=identities.query_rows(db,{**day['filters'],'_admin':'synthetic-snapshot-owner'})
                    self.assertEqual(day['devices'],len(rows))

# Only execute this file's additional cases; fixtures are inherited for reuse.
for name in list(existing.IdentityStatisticsTest.__dict__):
    if name.startswith('test_') and name not in SchoolActivityTest.__dict__:
        setattr(SchoolActivityTest,name,None)
