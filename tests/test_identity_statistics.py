"""HTTP acceptance with synthetic identities only; no production database or device."""
import concurrent.futures
import json
import secrets
import unittest
import uuid
import urllib.parse
import test_api as legacy_api
server=legacy_api.server
import identity_statistics as identities

class IdentityStatisticsTest(unittest.TestCase):
    setUpClass = classmethod(legacy_api.ApiTest.setUpClass.__func__)
    tearDownClass = classmethod(legacy_api.ApiTest.tearDownClass.__func__)
    call = legacy_api.ApiTest.call
    admin_token = legacy_api.ApiTest.admin_token

    def setUp(self):
        server.RATE_LIMITER=server.RateLimiter()

    def installation(self, scoped=None):
        auth={"installationId":str(uuid.uuid4()),"installationSecret":str(uuid.uuid4()),
              "versionName":"1.0.6","versionCode":38}
        status,result=self.call("POST","/api/v1/auth/device",auth)
        self.assertEqual(200,status,result)
        event={**auth,"eventId":str(uuid.uuid4()),"kind":"ACTIVATE","clientAt":server.now_ms(),
            "protocolVersion":2,"packageName":"com.mychangli.app","consentVersion":1,
            "manufacturer":"Synthetic","brand":"Test brand","modelRaw":"MODEL SAME",
            "androidVersion":"16","sdkInt":36,"appVersionName":"1.0.6","appVersionCode":38,
            "installationOrigin":"NEW","firstInstallAt":server.now_ms(),"deviceScopedId":scoped}
        return auth,event,result["data"]["token"]

    def account(self,auth):
        data={**auth,"phone":"19"+"".join(str(secrets.randbelow(10)) for _ in range(9)),
            "password":secrets.token_urlsafe(18),"nickname":"测试注册账号"}
        status,result=self.call("POST","/api/v1/auth/register",data)
        self.assertEqual(201,status,result)
        return data,result["data"]

    def report(self,event,token=None):
        return self.call("POST","/api/v1/installation-events",event,token)

    def rows(self,scope,**filters):
        status,result=self.call("GET","/api/v1/admin/identity-records?"+urllib.parse.urlencode(
            {"scope":scope,**filters}),token=self.admin_token())
        self.assertEqual(200,status,result)
        return result["data"]

    def test_activation_retries_concurrency_and_foreground_do_not_create_installations(self):
        auth,event,token=self.installation(secrets.token_hex(32))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results=list(executor.map(lambda _:self.report(event,token),range(6)))
        self.assertTrue(all(s==200 for s,r in results),results)
        for n in range(4):
            self.assertEqual(200,self.report({**event,"eventId":str(uuid.uuid4()),"kind":"FOREGROUND"},token)[0])
        with server.connection() as db:
            self.assertEqual(1,db.execute("SELECT count(*) FROM installations WHERE installation_id=?",(auth["installationId"],)).fetchone()[0])
            self.assertEqual(5,db.execute("SELECT count(*) FROM installation_events WHERE installation_id=?",(auth["installationId"],)).fetchone()[0])
        detail=self.rows("guests",uuid=auth["installationId"])["items"][0]
        self.assertEqual("游客 · Test brand MODEL SAME",detail["display_name"])
        self.assertNotIn("identity_secret_hash",json.dumps(detail))

    def test_no_consent_has_no_activation_and_withdrawal_clears_optional_profile(self):
        auth,event,token=self.installation()
        self.assertEqual(0,self.rows("installations",uuid=auth["installationId"])["total"])
        self.assertEqual(422,self.report({**event,"consentVersion":0},token)[0])
        self.assertEqual(200,self.report(event,token)[0])
        withdrawal={k:event[k] for k in ("installationId","installationSecret","protocolVersion","packageName","clientAt")}
        withdrawal.update(kind="WITHDRAW",eventId=str(uuid.uuid4()))
        self.assertEqual(200,self.report(withdrawal,token)[0])
        row=self.rows("installations",uuid=auth["installationId"])["items"][0]
        self.assertIsNone(row["model_raw"]);self.assertIsNone(row["foreground_at"])
        self.assertEqual(409,self.report({**event,"kind":"FOREGROUND","eventId":str(uuid.uuid4())},token)[0])
        self.assertEqual(200,self.call("GET","/api/v1/auth/me",token=token)[0])

    def test_registration_logout_and_two_accounts_retain_one_installation(self):
        auth,event,guest=self.installation(secrets.token_hex(32));self.report(event,guest)
        credentials,a=self.account(auth)
        self.assertEqual(0,self.rows("guests",uuid=auth["installationId"])["total"])
        self.assertEqual(1,self.rows("accounts",uuid=a["user"]["id"])["items"][0]["installation_count"])
        self.call("POST","/api/v1/auth/logout",{},a["token"])
        self.assertEqual(0,self.rows("guests",uuid=auth["installationId"])["total"])
        credentials2,b=self.account(auth)
        for c in (credentials,credentials2,credentials):
            self.assertEqual(200,self.call("POST","/api/v1/auth/login",c)[0])
        self.assertEqual(1,self.rows("installations",uuid=auth["installationId"])["total"])
        self.assertEqual(1,self.rows("accounts",uuid=b["user"]["id"])["items"][0]["installation_count"])
        self.assertNotEqual(a["user"]["id"],b["user"]["id"])

    def test_same_model_distinct_devices_and_reinstall_same_device(self):
        a,e,t=self.installation(secrets.token_hex(32));self.report(e,t)
        b,f,u=self.installation(secrets.token_hex(32));self.report(f,u)
        c,g,v=self.installation(e["deviceScopedId"]);self.report(g,v)
        ia=self.rows("installations",uuid=a["installationId"])["items"][0]
        ib=self.rows("installations",uuid=b["installationId"])["items"][0]
        ic=self.rows("installations",uuid=c["installationId"])["items"][0]
        self.assertNotEqual(ia["device_id"],ib["device_id"])
        self.assertEqual(ia["device_id"],ic["device_id"])
        self.assertNotEqual(t,v)
        # Physical-device match never authenticates the previous anonymous account.
        with server.connection() as db:
            self.assertNotEqual(db.execute("SELECT anonymous_user_id FROM installations WHERE installation_id=?",(a["installationId"],)).fetchone()[0],
                db.execute("SELECT anonymous_user_id FROM installations WHERE installation_id=?",(c["installationId"],)).fetchone()[0])

    def test_one_account_two_installations_and_update_reuses_identity(self):
        a,e,t=self.installation(secrets.token_hex(32));self.report(e,t);creds,user=self.account(a)
        b,f,u=self.installation(secrets.token_hex(32));self.report(f,u)
        status,login=self.call("POST","/api/v1/auth/login",{**creds,**b})
        self.assertEqual(200,status,login)
        self.assertEqual(user["user"]["id"],login["data"]["user"]["id"])
        self.assertEqual(200,self.report({**e,"eventId":str(uuid.uuid4()),"appVersionName":"1.0.7","appVersionCode":39},user["token"])[0])
        self.assertEqual(2,self.rows("accounts",uuid=user["user"]["id"])["items"][0]["installation_count"])

    def test_wrong_installation_proof_and_untrusted_user_id(self):
        a,e,t=self.installation()
        bad={**e,"installationSecret":str(uuid.uuid4())}
        self.assertEqual(403,self.report(bad,t)[0])
        self.assertEqual(403,self.call("POST","/api/v1/auth/device",{"installationId":a["installationId"]})[0])
        self.assertEqual(200,self.report({**e,"user_id":"other-user","userId":"other-user"},t)[0])
        self.assertEqual(1,self.rows("guests",uuid=a["installationId"])["total"])

    def test_profile_order_clock_errors_and_request_reads_do_not_refresh_activity(self):
        a,e,t=self.installation();self.report(e,t)
        before=self.rows("installations",uuid=a["installationId"])["items"][0]["foreground_at"]
        self.call("GET","/api/v1/auth/me",token=t)
        self.report({**e,"eventId":str(uuid.uuid4()),"clientAt":e["clientAt"]-10000,"modelRaw":"STALE"},t)
        self.report({**e,"eventId":str(uuid.uuid4()),"clientAt":e["clientAt"]+10*identities.DAY,"kind":"FOREGROUND"},t)
        after=self.rows("installations",uuid=a["installationId"])["items"][0]
        self.assertEqual(before,after["foreground_at"])
        self.assertEqual("MODEL SAME",after["model_raw"])
        self.assertEqual(409,self.report({**e,"kind":"PROFILE"},t)[0])

    def test_backfill_legacy_compatibility_and_idempotent_migration(self):
        iid="legacy-"+uuid.uuid4().hex
        self.assertEqual(200,self.call("POST","/api/v1/auth/device",{"installationId":iid})[0])
        a={"installationId":iid,"installationSecret":str(uuid.uuid4())}
        self.assertEqual(200,self.call("POST","/api/v1/auth/device",a)[0])
        _,event,_=self.installation()
        event.update(a);event.update(installationOrigin="UPGRADE",firstInstallAt=server.now_ms()-30*identities.DAY)
        self.assertEqual(200,self.report(event)[0])
        r=self.rows("installations",uuid=iid)["items"][0]
        self.assertEqual("LEGACY_REPORT",r["activation_source"])
        self.assertEqual(0,self.rows("installations",uuid=iid,firstActivation="today")["total"])
        with server.connection() as db:
            before=[db.execute("SELECT count(*) FROM "+table).fetchone()[0] for table in ("users","devices","installations","installation_accounts")]
            identities.migrate(db);identities.migrate(db)
            after=[db.execute("SELECT count(*) FROM "+table).fetchone()[0] for table in ("users","devices","installations","installation_accounts")]
            self.assertEqual(before,after)
            self.assertEqual("ok",db.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([],list(db.execute("PRAGMA foreign_key_check")))

    def test_card_pagination_export_and_filtered_counts_agree(self):
        for _ in range(3):
            a,e,t=self.installation();self.report(e,t)
        admin=self.admin_token()
        status,result=self.call("GET","/api/v1/admin/identity-statistics",token=admin)
        self.assertEqual(200,status,result)
        for metric in result["data"]["metrics"].values():
            f=metric["filters"]
            path="/api/v1/admin/identity-records?"+urllib.parse.urlencode({**f,"limit":1})
            _,page=self.call("GET",path,token=admin)
            _,export=self.call("GET",path+"&export=true",token=admin)
            self.assertEqual(metric["count"],page["data"]["total"])
            self.assertEqual(metric["count"],len(export["data"]["items"]))

    def test_admin_business_edit_uniqueness_authorization_and_no_secrets(self):
        a,e,t=self.installation();creds,user=self.account(a);admin=self.admin_token()
        uid=user["user"]["id"];path="/api/v1/admin/users/"+uid+"/business-profile"
        edit={"nickname":"已修改昵称","adminNote":"合成测试备注","reservationName":"测试姓名","reservationStudentId":"TEST001"}
        self.assertEqual(401,self.call("PATCH",path,edit)[0])
        self.assertEqual(403,self.call("PATCH",path,edit,user["token"])[0])
        status,result=self.call("PATCH",path,edit,admin);self.assertEqual(200,status,result)
        status,detail=self.call("GET","/api/v1/admin/users/"+uid+"/details",token=admin)
        self.assertEqual(200,status,detail)
        self.assertEqual(creds["phone"],detail["data"]["user"]["phone"])
        self.assertEqual("测试姓名",detail["data"]["user"]["reservation_name"])
        self.assertIsNone(detail["data"]["user"]["reservation_profile_consented_at"])
        self.assertTrue(detail["data"]["managementEvents"])
        self.assertEqual(422,self.call("PATCH",path,{"installation_count":999},admin)[0])
        for forbidden in ("password_hash","password_salt","identity_secret_hash","access_token","phone_encrypted"):
            self.assertNotIn(forbidden,json.dumps(detail))
        _,_,_=self.installation()
        _,other=self.account({"installationId":str(uuid.uuid4()),"installationSecret":str(uuid.uuid4())})
        with server.connection() as db:
            other_phone=server.decrypt_phone(db.execute("SELECT phone_encrypted FROM users WHERE id=?",(other["user"]["id"],)).fetchone()[0])
        self.assertEqual(409,self.call("PATCH",path,{"phone":other_phone},admin)[0])

    def test_snapshot_remains_stable_and_other_admin_cannot_reuse_it(self):
        admin=self.admin_token()
        _,snap=self.call("GET","/api/v1/admin/identity-statistics",token=admin)
        metric=snap["data"]["metrics"]["activated"]
        a,e,t=self.installation();self.report(e,t)
        path="/api/v1/admin/identity-records?"+urllib.parse.urlencode(metric["filters"])
        _,page=self.call("GET",path,token=admin)
        self.assertEqual(metric["count"],page["data"]["total"])
        self.assertEqual(403,self.call("GET",path,token=self.admin_token())[0])

    def test_model_mapping_preserves_identity_and_requires_a_source(self):
        a,e,t=self.installation(secrets.token_hex(32));self.report(e,t)
        row=self.rows("installations",uuid=a["installationId"])["items"][0];admin=self.admin_token()
        path="/api/v1/admin/identity-devices/"+row["device_id"]
        self.assertEqual(422,self.call("PATCH",path,{"displayModel":"未经证实名称"},admin)[0])
        self.assertEqual(200,self.call("PATCH",path,{"displayModel":"合成型号显示名","mappingSource":"隔离测试字典","adminNote":"测试备注"},admin)[0])
        after=self.rows("installations",uuid=a["installationId"])["items"][0]
        self.assertEqual(row["device_id"],after["device_id"])
        self.assertEqual("MODEL SAME",after["model_raw"])
        self.assertEqual("合成型号显示名",after["model_label"])

    def test_changed_device_key_degrades_and_stale_activation_cannot_undo_withdrawal(self):
        a,e,t=self.installation(secrets.token_hex(32));self.report(e,t)
        changed={**e,"eventId":str(uuid.uuid4()),"deviceScopedId":secrets.token_hex(32)}
        self.assertEqual(200,self.report(changed,t)[0])
        row=self.rows("installations",uuid=a["installationId"])["items"][0]
        self.assertIsNone(row["device_id"])
        self.assertEqual("CHANGED",row["device_identity_state"])
        self.report({**changed,"eventId":str(uuid.uuid4())},t)
        self.assertIsNone(self.rows("installations",uuid=a["installationId"])["items"][0]["device_id"])
        self.assertEqual(200,self.report({**e,"eventId":str(uuid.uuid4()),"kind":"WITHDRAW"},t)[0])
        self.assertEqual(409,self.report({**e,"eventId":str(uuid.uuid4())},t)[0])

    def test_deleted_account_optional_data_is_not_recovered_by_device_key(self):
        a,e,t=self.installation(secrets.token_hex(32));self.report(e,t);creds,user=self.account(a)
        self.assertEqual(200,self.call("DELETE","/api/v1/auth/account",
            {"password":creds["password"]},user["token"])[0])
        row=self.rows("installations",uuid=a["installationId"])["items"][0]
        self.assertIsNone(row["model_raw"]);self.assertIsNone(row["current_user_id"])
        b,f,u=self.installation(e["deviceScopedId"]);self.report(f,u)
        self.assertEqual(1,self.rows("guests",uuid=b["installationId"])["total"])
        self.assertEqual(0,self.rows("accounts",uuid=user["user"]["id"])["total"])
