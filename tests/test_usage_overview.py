"""Usage reconciliation using synthetic accounts, never production records."""
import uuid
from datetime import datetime
from unittest.mock import patch
import test_identity_statistics as existing

server=existing.server
identities=existing.identities

class UsageOverviewTest(existing.IdentityStatisticsTest):
    def snap(self, at=None):
        with server.connection() as db:
            if at is None: return identities.snapshot(db, 'usage-test')
            with patch.object(identities, 'now_ms', return_value=at):
                return identities.snapshot(db, 'usage-test')

    def metric_ids(self, snap, key):
        with server.connection() as db, patch.object(identities,'now_ms',return_value=snap['asOf']):
            result=identities.listing(db,{**snap['metrics'][key]['filters'], '_admin':'usage-test', 'export':'true'},server.decrypt_phone)
        self.assertEqual(snap['metrics'][key]['count'],result['total'])
        return {r['id'] for r in result['items']}

    def test_anonymous_auth_counts_service_only_without_invented_device_or_account(self):
        auth,event,token=self.installation()
        self.call('POST','/api/v1/auth/device',auth)
        snap=self.snap()
        self.assertIn(auth['installationId'],self.metric_ids(snap,'serviceToday'))
        self.assertNotIn(auth['installationId'],self.metric_ids(snap,'activated'))
        self.assertNotIn(auth['installationId'],self.metric_ids(snap,'profileReported'))
        self.assertIn(auth['installationId'],self.metric_ids(snap,'profileMissing'))
        with server.connection() as db:
            rows,_=identities.query_rows(db,{'scope':'installations','includeService':'true','activity':'today','activitySource':'service'})
            self.assertEqual(1,sum(r['id']==auth['installationId'] for r in rows))
        status,data=self.call('GET','/api/v1/admin/user-events?accountType=DEVICE&limit=100',token=self.admin_token())
        self.assertEqual(200,status)
        self.assertTrue(all(r['account_type']=='DEVICE' for r in data['data']['items']))

    def test_registered_login_without_optional_consent_counts_and_deduplicates(self):
        auth,event,token=self.installation(); credentials,account=self.account(auth)
        for _ in range(2): self.assertEqual(200,self.call('POST','/api/v1/auth/login',credentials)[0])
        snap=self.snap();uid=account['user']['id']
        for key in ('registered','registeredToday','registeredWeek','loginToday','accountToday','accountWeek'):
            self.assertIn(uid,self.metric_ids(snap,key))
        with server.connection() as db:
            row=identities.listing(db,{**snap['metrics']['accountToday']['filters'],'_admin':'usage-test','uuid':uid},server.decrypt_phone)['items'][0]
        self.assertIsNone(row['foreground_at'])
        self.assertIsNone(row['model_raw'])
        self.assertIsNotNone(row['last_service_at'])

    def test_business_activity_counts_but_failure_polling_and_admin_actions_do_not(self):
        auth,event,token=self.installation();_,a=self.account(auth)
        now=server.now_ms();uid=a['user']['id']
        with server.connection() as db:
            db.execute('DELETE FROM user_behavior_events WHERE user_id=?',(uid,))
            for event_type,result,source in [('APP_ACTIVE','SUCCESS','APP'),('LOGIN_FAILURE','FAILURE','APP'),('ACCOUNT_STATUS_CHANGED','SUCCESS','ADMIN'),('LOGIN_SUCCESS','FAILURE','APP'),('LOGIN_SUCCESS','SUCCESS','ADMIN')]:
                server.write_user_event(db,uid,'ACCOUNT',event_type,result,client_source=source,installation_id=auth['installationId'])
        self.assertNotIn(uid,self.metric_ids(self.snap(),'accountToday'))
        with server.connection() as db:
            server.write_user_event(db,uid,'RESOURCE_LIBRARY','RESOURCE_VIEWED',installation_id=auth['installationId'])
        snap=self.snap()
        self.assertIn(uid,self.metric_ids(snap,'accountToday'))
        self.assertNotIn(uid,self.metric_ids(snap,'loginToday'))

    def test_utc8_boundaries_and_future_records_use_asof(self):
        auth,event,token=self.installation();_,a=self.account(auth);uid=a['user']['id']
        at=int(datetime(2026,10,13,12,tzinfo=server.BUSINESS_TIMEZONE).timestamp()*1000)
        start,_=server.business_day_bounds_ms(datetime(2026,10,13).date())
        with server.connection() as db:
            db.execute('UPDATE users SET created_at=? WHERE id=?',(start-1,uid))
            db.execute('UPDATE installations SET created_at=? WHERE installation_id=?',(start-identities.DAY,auth['installationId']))
            db.execute('DELETE FROM user_behavior_events WHERE user_id=?',(uid,))
            for when in (start-1,at+1):
                with patch.object(server,'now_ms',return_value=when): server.write_user_event(db,uid,'ACCOUNT','LOGIN_SUCCESS',installation_id=auth['installationId'])
        snap=self.snap(at)
        self.assertNotIn(uid,self.metric_ids(snap,'accountToday'))
        self.assertIn(uid,self.metric_ids(snap,'accountWeek'))
        self.assertNotIn(uid,self.metric_ids(snap,'registeredToday'))
        self.assertIn(uid,self.metric_ids(snap,'registeredWeek'))

    def test_test_installations_and_deleted_accounts_do_not_add_activity(self):
        auth,event,token=self.installation();_,a=self.account(auth);uid=a['user']['id']
        with server.connection() as db: db.execute('UPDATE installations SET is_test=1 WHERE installation_id=?',(auth['installationId'],))
        snap=self.snap()
        self.assertNotIn(uid,self.metric_ids(snap,'accountToday'))
        self.assertNotIn(auth['installationId'],self.metric_ids(snap,'serviceToday'))
        with server.connection() as db:
            db.execute('UPDATE installations SET is_test=0 WHERE installation_id=?',(auth['installationId'],))
            db.execute("UPDATE users SET account_status='DELETED' WHERE id=?",(uid,))
        self.assertNotIn(uid,self.metric_ids(self.snap(),'accountToday'))

    def test_cards_trends_drilldown_export_and_snapshot_match_for_all_sources(self):
        auth,event,token=self.installation();_,a=self.account(auth)
        self.report({**event,'protocolVersion':3,'accountIdAtEvent':a['user']['id']},a['token'])
        snap=self.snap()
        for key in snap['metrics']: self.metric_ids(snap,key)
        with server.connection() as db:
            for day in snap['trend']:
                for key,scope,source in [('serviceInstallations','installations','service'),('accounts','accounts','account'),('devices','devices','foreground')]:
                    if day[key] is None: continue
                    rows,_=identities.query_rows(db,{**day['filters'],'scope':scope,'activitySource':source,'includeService':'true' if source=='service' else 'false','_admin':'usage-test'})
                    self.assertEqual(day[key],len(rows),(day['day'],key))
        old=self.metric_ids(snap,'accountToday')
        auth2,_,_=self.installation();_,a2=self.account(auth2)
        self.assertEqual(old,self.metric_ids(snap,'accountToday'))
        self.assertIn(a2['user']['id'],self.metric_ids(self.snap(),'accountToday'))

for name in list(existing.IdentityStatisticsTest.__dict__):
    if name.startswith('test_') and name not in UsageOverviewTest.__dict__: setattr(UsageOverviewTest,name,None)
