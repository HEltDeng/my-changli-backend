import concurrent.futures
import core
import importlib.util
import json
import secrets
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("my_changli_server", ROOT / "server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        test_database = Path(cls.temp.name) / "test.db"
        core.configure_test_runtime(test_database)
        server.DB_PATH = test_database
        server.UPLOADS = Path(cls.temp.name) / "uploads"
        server.initialize_database()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.ApiHandler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        with server.connection() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cls.temp.cleanup()

    def call(self, method, path, data=None, token=None):
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        parts = urllib.parse.urlsplit(path)
        safe_path = urllib.parse.quote(parts.path)
        safe_query = urllib.parse.quote(parts.query, safe="=&")
        target = safe_path + (f"?{safe_query}" if safe_query else "")
        request = urllib.request.Request(self.base + target, body, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def device_token(self, identity):
        status, response = self.call("POST", "/api/v1/auth/device", {"installationId": identity})
        self.assertEqual(200, status)
        return response["data"]["token"]

    def phone_account(self, installation_id=None):
        phone = "19" + "".join(str(secrets.randbelow(10)) for _ in range(9))
        password = secrets.token_urlsafe(18)
        payload = {
            "phone": phone,
            "password": password,
            "nickname": "自动化测试用户",
        }
        if installation_id:
            payload["installationId"] = installation_id
        status, response = self.call("POST", "/api/v1/auth/register", payload)
        self.assertEqual(201, status)
        return phone, password, response["data"]["token"], response["data"]["user"]

    def admin_token(self):
        generated_secret = secrets.token_urlsafe(24)
        digest, salt = server.password_record(generated_secret)
        admin_id = "admin-" + secrets.token_hex(8)
        username = "admin-" + secrets.token_hex(8)
        with server.connection() as db:
            db.execute(
                """INSERT INTO users(
                      id,username,password_hash,password_salt,role,account_type,nickname,
                      admin_permissions_json,admin_role,admin_enabled,account_status,created_at
                  ) VALUES(?,?,?,?, 'ADMIN','ADMIN','测试管理员','["*"]','SUPER_ADMIN',1,'ACTIVE',?)""",
                (admin_id, username, digest, salt, server.now_ms()),
            )
        status, login = self.call(
            "POST",
            "/api/v1/auth/login",
            {"username": username, "password": generated_secret},
        )
        self.assertEqual(200, status)
        return login["data"]["token"]

    def test_phone_account_gate_and_device_data_migration(self):
        installation_id = "migration-" + secrets.token_hex(8)
        device = self.device_token(installation_id)
        payload = {
            "clientLocalId": "migrated-" + secrets.token_hex(8),
            "type": "LOST",
            "itemName": "测试物品",
            "category": "OTHER",
            "location": "本地测试地点",
            "eventDate": date.today().isoformat(),
        }
        self.assertEqual(201, self.call("POST", "/api/v1/lost-found", payload, device)[0])
        phone, password, token, user = self.phone_account(installation_id)
        self.assertEqual("PHONE", user["accountType"])
        self.assertNotIn(phone, json.dumps(user, ensure_ascii=False))
        self.assertEqual(401, self.call("GET", "/api/v1/auth/me", token=device)[0])
        self.assertEqual(200, self.call("GET", "/api/v1/auth/me", token=token)[0])
        status, posts = self.call("GET", "/api/v1/lost-found?mine=true", token=token)
        self.assertEqual(200, status)
        self.assertTrue(any(row["itemName"] == "测试物品" for row in posts["data"]))
        self.assertEqual(409, self.call(
            "POST",
            "/api/v1/auth/register",
            {"phone": phone, "password": password, "nickname": "重复账号"},
        )[0])
        self.assertEqual(200, self.call(
            "POST",
            "/api/v1/auth/login",
            {"phone": phone, "password": password},
        )[0])

    def test_reservation_rules_maintenance_and_admin_override(self):
        _, _, user_token, user = self.phone_account()
        admin = self.admin_token()
        self.assertEqual(200, self.call(
            "PUT",
            "/api/v1/admin/reservation-rules",
            {
                "maxActivePerDay": 1,
                "maxUserCancellationsPerDay": 1,
                "cancelCutoffMinutes": 0,
                "restrictionHoursAfterExcessCancel": 1,
            },
            admin,
        )[0])
        booking_date = (date.today() + timedelta(days=1)).isoformat()
        request = {
            "venueId": "badminton",
            "date": booking_date,
            "timeSlot": "09:00-10:00",
            "courtNumber": 6,
        }
        status, created = self.call(
            "POST", "/api/v1/sports/reservations", request, user_token
        )
        self.assertEqual(201, status)
        request["courtNumber"] = 7
        self.assertEqual(429, self.call(
            "POST", "/api/v1/sports/reservations", request, user_token
        )[0])
        reservation_id = created["data"]["id"]
        self.assertEqual(200, self.call(
            "POST", f"/api/v1/sports/reservations/{reservation_id}/cancel", {}, user_token
        )[0])
        self.assertEqual(429, self.call(
            "POST", "/api/v1/sports/reservations", request, user_token
        )[0])
        self.assertEqual(200, self.call(
            "POST",
            f"/api/v1/admin/users/{user['id']}/clear-restrictions",
            {},
            admin,
        )[0])
        self.assertEqual(201, self.call(
            "POST", "/api/v1/sports/reservations", request, user_token
        )[0])
        self.assertEqual(200, self.call(
            "PUT",
            "/api/v1/admin/flags/sports_reservation",
            {
                "enabled": False,
                "payload": {
                    "status": "MAINTENANCE",
                    "maintenanceNote": "自动化维护测试",
                },
            },
            admin,
        )[0])
        status, error = self.call("GET", "/api/v1/sports/venues", token=user_token)
        self.assertEqual(503, status)
        self.assertEqual("FEATURE_MAINTENANCE", error["error"]["code"])
        self.assertEqual(200, self.call(
            "PUT",
            "/api/v1/admin/flags/sports_reservation",
            {"enabled": True, "payload": {"status": "OPEN"}},
            admin,
        )[0])

    def test_feedback_workflow_announcement_delete_and_audit(self):
        _, _, user_token, _ = self.phone_account()
        admin = self.admin_token()
        status, feedback = self.call(
            "POST",
            "/api/v1/feedback",
            {
                "category": "FEATURE_SUGGESTION",
                "title": "反馈闭环自动化",
                "content": "本地自动化反馈",
                "appVersion": "test",
            },
            user_token,
        )
        self.assertEqual(201, status)
        feedback_id = feedback["data"]["id"]
        self.assertEqual(200, self.call(
            "PATCH",
            f"/api/v1/admin/feedback/{feedback_id}",
            {
                "status": "RESOLVED",
                "publicReply": "问题已处理，请更新后重试。",
                "internalNote": "仅管理员可见的回归记录",
            },
            admin,
        )[0])
        status, mine = self.call("GET", "/api/v1/feedback", token=user_token)
        self.assertEqual(200, status)
        self.assertEqual("RESOLVED", mine["data"][0]["status"])
        self.assertEqual("问题已处理，请更新后重试。", mine["data"][0]["publicReply"])
        self.assertNotIn("internal", json.dumps(mine["data"], ensure_ascii=False).lower())
        self.assertNotIn("仅管理员", json.dumps(mine["data"], ensure_ascii=False))
        status, detail = self.call(
            "GET", f"/api/v1/admin/feedback/{feedback_id}", token=admin
        )
        self.assertEqual(200, status)
        self.assertEqual(
            "仅管理员可见的回归记录",
            detail["data"]["feedback"]["internal_note"],
        )
        status, announcement = self.call(
            "POST",
            "/api/v1/admin/announcements",
            {"title": "待删除公告", "content": "自动化测试", "enabled": True},
            admin,
        )
        self.assertEqual(201, status)
        announcement_id = announcement["data"]["id"]
        self.assertEqual(200, self.call(
            "DELETE", f"/api/v1/admin/announcements/{announcement_id}", token=admin
        )[0])
        status, bootstrap = self.call("GET", "/api/v1/public/bootstrap")
        self.assertEqual(200, status)
        self.assertFalse(any(
            row["id"] == announcement_id for row in bootstrap["data"]["announcements"]
        ))
        status, audits = self.call("GET", "/api/v1/admin/audit-logs", token=admin)
        self.assertEqual(200, status)
        self.assertTrue(any(
            row["action"] == "ANNOUNCEMENT_DELETED" for row in audits["data"]
        ))

    def test_reservation_profile_rules_snapshot_and_transparency(self):
        _, _, token, user = self.phone_account()
        admin = self.admin_token()
        status, context = self.call(
            "GET", "/api/v1/sports/context", token=token
        )
        self.assertEqual(200, status)
        self.assertFalse(context["data"]["rules"]["requireReservationStudentId"])
        self.assertEqual("OPTIONAL", context["data"]["rules"]["reservationProfileMode"])
        self.assertIsNone(context["data"]["profile"]["studentId"])
        self.assertEqual(200, self.call(
            "PUT",
            "/api/v1/admin/reservation-rules",
            {
                "maxActivePerDay": 2,
                "maxUserCancellationsPerDay": 2,
                "cancelCutoffMinutes": 30,
                "restrictionHoursAfterExcessCancel": 24,
                "reservationProfileMode": "REQUIRED",
                "reason": "验证预约资料必填流程",
            },
            admin,
        )[0])
        request = {
            "venueId": "table-tennis",
            "date": (date.today() + timedelta(days=2)).isoformat(),
            "timeSlot": "14:00-15:00",
            "courtNumber": 8,
        }
        self.assertEqual(409, self.call(
            "POST", "/api/v1/sports/reservations", request, token
        )[0])
        status, profile = self.call(
            "PUT",
            "/api/v1/auth/reservation-profile",
            {
                "studentId": "TEST" + secrets.token_hex(5),
                "name": "预约测试同学",
                "consented": True,
            },
            token,
        )
        self.assertEqual(200, status)
        saved_id = profile["data"]["profile"]["studentId"]
        status, created = self.call(
            "POST", "/api/v1/sports/reservations", request, token
        )
        self.assertEqual(201, status)
        self.assertEqual(
            saved_id, created["data"]["reservationStudentIdSnapshot"]
        )
        self.assertIn("canCancel", created["data"])
        status, users = self.call(
            "GET", f"/api/v1/admin/users?q={saved_id}", token=admin
        )
        self.assertEqual(200, status)
        self.assertEqual(user["id"], users["data"][0]["id"])
        self.assertEqual(saved_id, users["data"][0]["reservation_student_id"])
        self.assertEqual("预约测试同学", users["data"][0]["reservation_name"])
        status, by_name = self.call(
            "GET", "/api/v1/admin/users?q=预约测试同学", token=admin
        )
        self.assertEqual(200, status)
        self.assertEqual(user["id"], by_name["data"][0]["id"])
        status, revealed = self.call(
            "POST",
            f"/api/v1/admin/users/{user['id']}/reservation-profile",
            {"reason": "场馆人工核验预约资料"},
            admin,
        )
        self.assertEqual(200, status)
        self.assertEqual(saved_id, revealed["data"]["studentId"])
        self.assertEqual("预约测试同学", revealed["data"]["name"])
        status, snapshot = self.call(
            "POST",
            f"/api/v1/admin/reservations/{created['data']['id']}/profile",
            {"reason": "核验历史预约资料快照"},
            admin,
        )
        self.assertEqual(200, status)
        self.assertEqual(saved_id, snapshot["data"]["studentId"])
        self.assertEqual(200, self.call(
            "DELETE", "/api/v1/auth/reservation-profile", token=token
        )[0])
        status, reservations = self.call(
            "GET", "/api/v1/sports/reservations", token=token
        )
        self.assertEqual(200, status)
        self.assertEqual(
            saved_id, reservations["data"][0]["reservationStudentIdSnapshot"]
        )
        self.assertEqual(
            "预约测试同学", reservations["data"][0]["reservationNameSnapshot"]
        )
        status, audits = self.call("GET", "/api/v1/admin/audit-logs", token=admin)
        rule_audit = next(
            row for row in audits["data"]
            if row["action"] == "RESERVATION_RULES_UPDATED"
        )
        self.assertIn("old", json.loads(rule_audit["details_json"]))
        self.assertNotIn(saved_id, rule_audit["details_json"])
        audit_json = json.dumps(audits["data"], ensure_ascii=False)
        self.assertIn("RESERVATION_PROFILE_REVEALED", audit_json)
        self.assertNotIn("预约测试同学", audit_json)
        self.assertNotIn(saved_id, audit_json)

    def test_feedback_validation_filters_and_account_isolation(self):
        phone_a, _, token_a, user_a = self.phone_account()
        _, _, token_b, _ = self.phone_account()
        admin = self.admin_token()
        status, created = self.call(
            "POST",
            "/api/v1/feedback",
            {
                "category": "ACCOUNT_SECURITY",
                "title": "账号登录问题",
                "content": "<script>alert('x')</script>\n第二行说明",
                "appVersion": "test",
            },
            token_a,
        )
        self.assertEqual(201, status)
        feedback_id = created["data"]["id"]
        self.assertEqual([], self.call("GET", "/api/v1/feedback", token=token_b)[1]["data"])
        self.assertEqual(
            404,
            self.call("GET", f"/api/v1/feedback/{feedback_id}", token=token_b)[0],
        )
        self.assertEqual(
            400,
            self.call(
                "POST",
                "/api/v1/feedback",
                {"category": "INVALID", "title": "无效", "content": "内容"},
                token_a,
            )[0],
        )
        self.assertEqual(
            400,
            self.call(
                "PATCH",
                f"/api/v1/admin/feedback/{feedback_id}",
                {"status": "RESOLVED", "publicReply": ""},
                admin,
            )[0],
        )
        status, rows = self.call(
            "GET",
            f"/api/v1/admin/feedback?status=OPEN&category=ACCOUNT_SECURITY&user={phone_a}&q=登录",
            token=admin,
        )
        self.assertEqual(200, status)
        self.assertEqual(feedback_id, rows["data"][0]["id"])
        self.assertNotIn(phone_a, json.dumps(rows["data"], ensure_ascii=False))
        self.assertEqual(user_a["id"], rows["data"][0]["user_id"])

    def test_exact_phone_search_controlled_reveal_and_user_details(self):
        phone, _, token, user = self.phone_account()
        self.assertEqual(201, self.call(
            "POST",
            "/api/v1/feedback",
            {
                "category": "OTHER",
                "title": "关联查询",
                "content": "关联查询测试",
                "appVersion": "test",
            },
            token,
        )[0])
        admin = self.admin_token()
        status, users = self.call(
            "GET", f"/api/v1/admin/users?q={phone}", token=admin
        )
        self.assertEqual(200, status)
        self.assertEqual(user["id"], users["data"][0]["id"])
        self.assertEqual(phone, users["data"][0]["phone"])
        status, revealed = self.call(
            "POST",
            f"/api/v1/admin/users/{user['id']}/phone",
            {"reason": "用户主动报号后定位问题"},
            admin,
        )
        self.assertEqual(200, status)
        self.assertEqual(phone, revealed["data"]["phone"])
        status, details = self.call(
            "GET", f"/api/v1/admin/users/{user['id']}/details", token=admin
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(details["data"]["feedback"]))
        status, audits = self.call("GET", "/api/v1/admin/audit-logs", token=admin)
        audit_json = json.dumps(audits["data"], ensure_ascii=False)
        self.assertIn("USER_PHONE_REVEALED", audit_json)
        self.assertNotIn(phone, audit_json)

    def test_registration_login_logout_and_account_status_enforcement(self):
        phone, password, token, user = self.phone_account()
        self.assertEqual(200, self.call("GET", "/api/v1/auth/me", token=token)[0])
        self.assertEqual(409, self.call(
            "POST", "/api/v1/auth/register",
            {"phone": phone, "password": password, "nickname": "重复用户"},
        )[0])
        self.assertEqual(401, self.call(
            "POST", "/api/v1/auth/login",
            {"phone": phone, "password": "definitely-wrong-password"},
        )[0])
        self.assertEqual(200, self.call(
            "POST", "/api/v1/auth/login", {"phone": phone, "password": password},
        )[0])
        self.assertEqual(200, self.call("POST", "/api/v1/auth/logout", {}, token)[0])
        self.assertEqual(401, self.call("GET", "/api/v1/auth/me", token=token)[0])
        self.assertEqual(401, self.call("GET", "/api/v1/auth/me", token="invalid-token")[0])

        admin = self.admin_token()
        self.assertEqual(200, self.call(
            "PATCH", f"/api/v1/admin/users/{user['id']}",
            {"accountStatus": "DISABLED", "reason": "自动化权限测试"}, admin,
        )[0])
        disabled_status, disabled = self.call(
            "POST", "/api/v1/auth/login", {"phone": phone, "password": password},
        )
        self.assertEqual(403, disabled_status)
        self.assertNotIn(password, json.dumps(disabled, ensure_ascii=False))
        self.assertEqual(200, self.call(
            "PATCH", f"/api/v1/admin/users/{user['id']}",
            {"accountStatus": "DELETED", "reason": "自动化注销测试"}, admin,
        )[0])
        self.assertEqual(403, self.call(
            "POST", "/api/v1/auth/login", {"phone": phone, "password": password},
        )[0])

    def test_admin_role_workspaces_and_server_side_module_isolation(self):
        super_token = self.admin_token()
        cases = [
            ("SPORTS_OPERATOR", "/api/v1/admin/overview/sports", "/api/v1/admin/lost-found"),
            ("LOST_FOUND_OPERATOR", "/api/v1/admin/overview/lost-found", "/api/v1/admin/venues"),
            ("LIBRARY_OPERATOR", "/api/v1/admin/overview/library", "/api/v1/admin/reservations"),
            ("CONTENT_OPERATOR", "/api/v1/admin/overview/content", "/api/v1/admin/users"),
        ]
        for role, allowed, forbidden in cases:
            username = f"operator-{role.lower()}-{secrets.token_hex(3)}"
            password = "Safe-local-" + secrets.token_urlsafe(16)
            status, created = self.call(
                "POST", "/api/v1/admin/admins",
                {"username": username, "nickname": role, "password": password, "adminRole": role},
                super_token,
            )
            self.assertEqual(201, status)
            login_status, login = self.call(
                "POST", "/api/v1/auth/login", {"username": username, "password": password},
            )
            self.assertEqual(200, login_status)
            operator_token = login["data"]["token"]
            self.assertEqual(role, login["data"]["user"]["adminRole"])
            self.assertEqual(200, self.call("GET", allowed, token=operator_token)[0])
            self.assertEqual(403, self.call("GET", forbidden, token=operator_token)[0])
            self.assertEqual(403, self.call("GET", "/api/v1/admin/admins", token=operator_token)[0])
            self.assertEqual(200, self.call(
                "PATCH", f"/api/v1/admin/admins/{created['data']['id']}",
                {"adminRole": role, "enabled": False}, super_token,
            )[0])
            self.assertEqual(401, self.call("GET", allowed, token=operator_token)[0])
            self.assertEqual(403, self.call(
                "POST", "/api/v1/auth/login",
                {"username": username, "password": password},
            )[0])

        status, audits = self.call("GET", "/api/v1/admin/audit-logs", token=super_token)
        self.assertEqual(200, status)
        serialized = json.dumps(audits["data"], ensure_ascii=False)
        self.assertIn("ADMIN_ACCESS_DENIED", serialized)
        self.assertNotIn("Safe-local-", serialized)

    def test_sports_operator_receives_complete_reservation_profile_only_in_sports(self):
        phone, _, user_token, user = self.phone_account()
        student_id = "SPORT" + secrets.token_hex(5)
        name = "体育核验同学"
        self.assertEqual(200, self.call(
            "PUT", "/api/v1/auth/reservation-profile",
            {"name": name, "studentId": student_id, "consented": True}, user_token,
        )[0])
        booking_date = (date.today() + timedelta(days=2)).isoformat()
        status, created = self.call(
            "POST", "/api/v1/sports/reservations",
            {"venueId": "badminton", "date": booking_date,
             "timeSlot": "10:00-11:00", "courtNumber": 8}, user_token,
        )
        self.assertEqual(201, status)
        reservation_id = created["data"]["id"]

        super_token = self.admin_token()
        operator_password = secrets.token_urlsafe(18)
        operator_username = "sports-op-" + secrets.token_hex(4)
        status, operator = self.call(
            "POST", "/api/v1/admin/admins",
            {"username": operator_username, "nickname": "体育运营测试",
             "password": operator_password, "adminRole": "SPORTS_OPERATOR"},
            super_token,
        )
        self.assertEqual(201, status)
        status, login = self.call(
            "POST", "/api/v1/auth/login",
            {"username": operator_username, "password": operator_password},
        )
        self.assertEqual(200, status)
        token = login["data"]["token"]

        status, listing = self.call(
            "GET", f"/api/v1/admin/reservations?paged=true&user={phone}", token=token,
        )
        self.assertEqual(200, status)
        self.assertEqual(1, listing["data"]["total"])
        row = listing["data"]["items"][0]
        self.assertEqual(phone, row["phone"])
        self.assertEqual(name, row["reservation_name_snapshot"])
        self.assertEqual(student_id, row["reservation_student_id_snapshot"])
        self.assertEqual(user["id"], row["user_id"])
        self.assertEqual(reservation_id, row["id"])

        status, details = self.call(
            "GET", f"/api/v1/admin/reservations/{reservation_id}", token=token,
        )
        self.assertEqual(200, status)
        self.assertEqual(phone, details["data"]["reservation"]["phone"])
        self.assertTrue(details["data"]["sportsHistory"])
        self.assertNotIn("lostFound", details["data"])
        self.assertNotIn("feedback", details["data"])
        self.assertEqual(200, self.call(
            "POST", f"/api/v1/admin/reservations/{reservation_id}/profile", {}, token,
        )[0])
        self.assertEqual(200, self.call(
            "POST", f"/api/v1/admin/reservations/{reservation_id}/verify",
            {"status": "VERIFIED"}, token,
        )[0])
        self.assertEqual(200, self.call(
            "POST", f"/api/v1/admin/reservations/{reservation_id}/cancel", {}, token,
        )[0])
        for forbidden in (
            f"/api/v1/admin/users/{user['id']}/details",
            "/api/v1/admin/lost-found",
            "/api/v1/admin/resources",
            "/api/v1/admin/audit-logs",
        ):
            self.assertEqual(403, self.call("GET", forbidden, token=token)[0])

        status, audits = self.call("GET", "/api/v1/admin/audit-logs", token=super_token)
        self.assertEqual(200, status)
        text = json.dumps(audits["data"], ensure_ascii=False)
        self.assertIn("SPORTS_RESERVATIONS_VIEWED", text)
        self.assertIn("RESERVATION_PROFILE_VERIFIED", text)
        self.assertNotIn(phone, text)
        self.assertNotIn(student_id, text)

    def test_feature_states_persist_publish_and_block_user_apis(self):
        _, _, token, _ = self.phone_account()
        admin = self.admin_token()
        booking_status, booking = self.call(
            "POST", "/api/v1/sports/reservations",
            {"venueId": "table-tennis", "date": (date.today() + timedelta(days=3)).isoformat(),
             "timeSlot": "14:00-15:00", "courtNumber": 10}, token,
        )
        self.assertEqual(201, booking_status)
        cases = [
            ("sports_reservation", "/api/v1/sports/venues"),
            ("lost_found", "/api/v1/lost-found"),
            ("resource_library", "/api/v1/library"),
        ]
        for feature_key, user_path in cases:
            for feature_status, expected_code in (
                ("MAINTENANCE", "FEATURE_MAINTENANCE"),
                ("CLOSED", "FEATURE_CLOSED"),
            ):
                update_status, update = self.call(
                    "PUT", f"/api/v1/admin/flags/{feature_key}",
                    {"payload": {"status": feature_status,
                                 "maintenanceNote": f"{feature_key} local test",
                                 "expectedRestoreAt": server.now_ms() + 60000},
                     "changeNote": "automated state transition"}, admin,
                )
                self.assertEqual(200, update_status)
                self.assertEqual(feature_status, update["data"]["payload"]["status"])
                bootstrap_status, bootstrap = self.call("GET", "/api/v1/public/bootstrap")
                self.assertEqual(200, bootstrap_status)
                flag = next(item for item in bootstrap["data"]["flags"] if item["key"] == feature_key)
                self.assertEqual(feature_status, flag["payload"]["status"])
                self.assertIsNotNone(flag["updatedAt"])
                blocked_status, blocked = self.call("GET", user_path, token=token)
                self.assertEqual(503, blocked_status)
                self.assertEqual(expected_code, blocked["error"]["code"])
                if feature_key == "sports_reservation" and feature_status == "CLOSED":
                    self.assertEqual(200, self.call(
                        "GET", "/api/v1/sports/reservations", token=token,
                    )[0])
                    self.assertEqual(200, self.call(
                        "POST", f"/api/v1/sports/reservations/{booking['data']['id']}/cancel",
                        {}, token,
                    )[0])
            self.assertEqual(200, self.call(
                "PUT", f"/api/v1/admin/flags/{feature_key}",
                {"payload": {"status": "OPEN"}}, admin,
            )[0])
            self.assertEqual(200, self.call("GET", user_path, token=token)[0])

        operator_password = secrets.token_urlsafe(18)
        operator_username = "sports-flag-denied-" + secrets.token_hex(3)
        self.assertEqual(201, self.call(
            "POST", "/api/v1/admin/admins",
            {"username": operator_username, "nickname": "体育只读开关",
             "password": operator_password, "adminRole": "SPORTS_OPERATOR"}, admin,
        )[0])
        _, login = self.call("POST", "/api/v1/auth/login",
                             {"username": operator_username, "password": operator_password})
        self.assertEqual(403, self.call(
            "PUT", "/api/v1/admin/flags/sports_reservation",
            {"payload": {"status": "CLOSED"}}, login["data"]["token"],
        )[0])

    def test_published_version_check_is_local_and_drafts_are_hidden(self):
        admin = self.admin_token()
        payload = {
            "latestVersionCode": 42,
            "minimumVersionCode": 20,
            "latestVersionName": "4.2-local",
            "updateMessage": "本地版本演示",
            "downloadUrl": "https://updates.invalid/my-changli.apk",
            "apkSha256": "a" * 64,
            "apkSize": 123456,
            "forceUpdate": False,
            "status": "DRAFT",
        }
        self.assertEqual(200, self.call("PUT", "/api/v1/admin/version/android", payload, admin)[0])
        hidden = self.call("GET", "/api/v1/public/version/android?currentVersionCode=1")[1]
        self.assertFalse(hidden["data"]["updateAvailable"])
        payload["status"] = "PUBLISHED"
        self.assertEqual(200, self.call("PUT", "/api/v1/admin/version/android", payload, admin)[0])
        status, newer = self.call("GET", "/api/v1/public/version/android?currentVersionCode=21")
        self.assertEqual(200, status)
        self.assertTrue(newer["data"]["updateAvailable"])
        self.assertFalse(newer["data"]["minimumVersionViolated"])
        self.assertFalse(newer["data"]["mandatoryUpdate"])
        below_minimum = self.call(
            "GET", "/api/v1/public/version/android?currentVersionCode=1"
        )[1]
        self.assertTrue(below_minimum["data"]["minimumVersionViolated"])
        self.assertTrue(below_minimum["data"]["mandatoryUpdate"])
        self.assertEqual(42, newer["data"]["latestVersion"]["latest_version_code"])
        current = self.call("GET", "/api/v1/public/version/android?currentVersionCode=42")[1]
        self.assertFalse(current["data"]["updateAvailable"])
        unsafe = dict(payload, downloadUrl="javascript:alert(1)")
        self.assertEqual(400, self.call("PUT", "/api/v1/admin/version/android", unsafe, admin)[0])

    def test_resource_library_admin_publish_user_activity_and_complaint(self):
        _, _, token_a, _ = self.phone_account()
        _, _, token_b, _ = self.phone_account()
        admin = self.admin_token()
        payload = {
            "title": "自动化测试资料",
            "summary": "仅用于本地接口测试",
            "category": "学习资料",
            "tags": ["测试", "公开链接"],
            "audienceScope": "测试用户",
            "sourceLabel": "自动化测试",
            "usageNote": "请遵守版权与使用规则",
            "externalUrl": "https://example.org/resource",
            "publishStatus": "PUBLISHED",
            "visibility": "MEMBERS",
            "isFree": True,
            "sortOrder": 99,
        }
        status, created = self.call(
            "POST", "/api/v1/admin/resources", payload, admin
        )
        self.assertEqual(201, status)
        resource_id = created["data"]["id"]
        self.assertEqual(401, self.call("GET", "/api/v1/library?q=自动化")[0])
        public_payload = {
            **payload,
            "title": "公开浏览测试资料",
            "visibility": "PUBLIC",
        }
        status, public_created = self.call(
            "POST", "/api/v1/admin/resources", public_payload, admin
        )
        self.assertEqual(201, status)
        public_id = public_created["data"]["id"]
        self.assertEqual(200, self.call(
            "PUT",
            "/api/v1/admin/flags/resource_library",
            {
                "enabled": True,
                "payload": {
                    "status": "OPEN",
                    "allowGuestBrowsing": True,
                },
            },
            admin,
        )[0])
        status, guest_resources = self.call(
            "GET", "/api/v1/library?q=公开浏览"
        )
        self.assertEqual(200, status)
        self.assertEqual([public_id], [item["id"] for item in guest_resources["data"]])
        self.assertEqual(401, self.call(
            "POST", f"/api/v1/library/{public_id}/favorite", {}
        )[0])
        status, resources = self.call(
            "GET", "/api/v1/library?q=自动化", token=token_a
        )
        self.assertEqual(200, status)
        self.assertEqual(resource_id, resources["data"][0]["id"])
        self.assertEqual(200, self.call(
            "POST", f"/api/v1/library/{resource_id}/view", {}, token_a
        )[0])
        self.assertEqual(200, self.call(
            "POST", f"/api/v1/library/{resource_id}/favorite", {}, token_a
        )[0])
        status, favorites = self.call(
            "GET", "/api/v1/library?favorites=true", token=token_a
        )
        self.assertEqual(resource_id, favorites["data"][0]["id"])
        status, other_favorites = self.call(
            "GET", "/api/v1/library?favorites=true", token=token_b
        )
        self.assertEqual([], other_favorites["data"])
        status, complaint = self.call(
            "POST",
            f"/api/v1/library/{resource_id}/complaints",
            {"reason": "版权问题", "detail": "本地自动化投诉"},
            token_a,
        )
        self.assertEqual(201, status)
        complaint_id = complaint["data"]["id"]
        self.assertEqual(200, self.call(
            "PATCH",
            f"/api/v1/admin/resource-complaints/{complaint_id}",
            {"status": "RESOLVED", "resolutionNote": "已核查"},
            admin,
        )[0])
        self.assertEqual(200, self.call(
            "PATCH",
            f"/api/v1/admin/resources/{resource_id}",
            {"publishStatus": "UNLISTED"},
            admin,
        )[0])
        self.assertEqual(404, self.call(
            "GET", f"/api/v1/library/{resource_id}", token=token_a
        )[0])
        self.assertEqual(200, self.call(
            "DELETE", f"/api/v1/admin/resources/{resource_id}", token=admin
        )[0])
        self.assertEqual(200, self.call(
            "DELETE", f"/api/v1/admin/resources/{public_id}", token=admin
        )[0])

    def test_health_and_bootstrap(self):
        self.assertEqual(200, self.call("GET", "/health")[0])
        status, body = self.call("GET", "/api/v1/public/bootstrap")
        self.assertEqual(200, status)
        self.assertTrue(any(flag["key"] == "sports_reservation" for flag in body["data"]["flags"]))
        if body["data"].get("version"):
            self.assertIsInstance(body["data"]["version"]["force_update"], bool)

    def test_service_updates_aggregation_and_visibility(self):
        admin = self.admin_token()
        for feature_key in ("sports_reservation", "lost_found", "resource_library"):
            self.assertEqual(
                200,
                self.call(
                    "PUT",
                    f"/api/v1/admin/flags/{feature_key}",
                    {"payload": {"status": "OPEN", "maintenanceNote": ""}},
                    admin,
                )[0],
            )

        version_payload = {
            "latestVersionCode": 42,
            "minimumVersionCode": 20,
            "latestVersionName": "4.2-local",
            "updateMessage": "服务动态聚合测试版本",
            "downloadUrl": "https://updates.invalid/my-changli.apk",
            "apkSha256": "a" * 64,
            "apkSize": 123456,
            "forceUpdate": False,
            "status": "DRAFT",
        }
        self.assertEqual(
            200,
            self.call("PUT", "/api/v1/admin/version/android", version_payload, admin)[0],
        )
        empty_status, empty = self.call("GET", "/api/v1/public/service-updates")
        self.assertEqual(200, empty_status)
        self.assertEqual([], empty["data"])
        version_payload["status"] = "PUBLISHED"
        self.assertEqual(
            200,
            self.call("PUT", "/api/v1/admin/version/android", version_payload, admin)[0],
        )

        visible = self.call(
            "POST",
            "/api/v1/admin/announcements",
            {"title": "服务页可见公告", "content": "公告正文", "serviceVisible": True},
            admin,
        )
        hidden = self.call(
            "POST",
            "/api/v1/admin/announcements",
            {"title": "服务页隐藏公告", "content": "不应出现", "serviceVisible": False},
            admin,
        )
        disabled = self.call(
            "POST",
            "/api/v1/admin/announcements",
            {"title": "撤回公告", "content": "不应出现", "serviceVisible": True, "enabled": False},
            admin,
        )
        self.assertEqual(201, visible[0])
        self.assertEqual(201, hidden[0])
        self.assertEqual(201, disabled[0])

        status, updates = self.call("GET", "/api/v1/public/service-updates")
        self.assertEqual(200, status)
        items = updates["data"]
        self.assertEqual(["UPDATE"], [item["type"] for item in items if item["type"] == "UPDATE"])
        update_item = next(item for item in items if item["type"] == "UPDATE")
        self.assertEqual("ABOUT_UPDATE", update_item["targetType"])
        self.assertEqual(42, update_item["versionCode"])
        self.assertIn("announcement-", items[-1]["id"])
        self.assertEqual("服务页可见公告", items[-1]["title"])
        self.assertEqual("ANNOUNCEMENT", items[-1]["targetType"])
        self.assertIsNotNone(items[-1]["targetId"])
        self.assertTrue(all("maintenanceNote" not in item for item in items))
        self.assertTrue(all("changeNote" not in item for item in items))
        self.assertFalse(any(item["title"] == "服务页隐藏公告" for item in items))
        self.assertFalse(any(item["title"] == "撤回公告" for item in items))

        self.assertEqual(
            200,
            self.call(
                "PUT",
                "/api/v1/admin/flags/sports_reservation",
                {"payload": {"status": "MAINTENANCE", "maintenanceNote": "场馆维护"}},
                admin,
            )[0],
        )
        _, updates = self.call("GET", "/api/v1/public/service-updates")
        maintenance = [item for item in updates["data"] if item["type"] == "MAINTENANCE"]
        self.assertEqual(1, len(maintenance))
        self.assertEqual("sports_reservation", maintenance[0]["moduleKey"])
        self.assertEqual("体育预约维护中", maintenance[0]["title"])
        self.assertEqual("FEATURE", maintenance[0]["targetType"])
        self.assertEqual("场馆维护", maintenance[0]["summary"])

        self.assertEqual(
            200,
            self.call(
                "PUT",
                "/api/v1/admin/flags/sports_reservation",
                {"payload": {"status": "CLOSED", "maintenanceNote": "暂不开放"}},
                admin,
            )[0],
        )
        _, updates = self.call("GET", "/api/v1/public/service-updates")
        closed = [item for item in updates["data"] if item["type"] == "CLOSED"]
        self.assertEqual(1, len(closed))
        self.assertEqual("sports_reservation", closed[0]["moduleKey"])
        self.assertEqual(90, closed[0]["priority"])
        self.assertEqual("FEATURE", closed[0]["targetType"])
        self.assertEqual("暂不开放", closed[0]["summary"])

        self.assertEqual(
            200,
            self.call(
                "PUT",
                "/api/v1/admin/flags/sports_reservation",
                {"payload": {"status": "OPEN"}},
                admin,
            )[0],
        )
        _, updates = self.call("GET", "/api/v1/public/service-updates")
        self.assertFalse(any(item["type"] == "MAINTENANCE" for item in updates["data"]))
        self.assertFalse(any(item["type"] == "CLOSED" for item in updates["data"]))

    def test_core_pagination_and_error_model(self):
        self.assertEqual((50, 0), core.parse_pagination({}))
        self.assertEqual((25, 10), core.parse_pagination({"limit": "25", "offset": "10"}))
        self.assertEqual((100, 10000), core.parse_pagination({"limit": "999", "offset": "99999"}))
        with self.assertRaises(core.ApiError) as ctx:
            core.parse_pagination({"limit": "abc"})
        self.assertEqual(422, ctx.exception.status)
        self.assertEqual("VALIDATION_ERROR", ctx.exception.code)
        response = core.paginated_response(["a"], 1, 50, 0)
        self.assertEqual({"items": ["a"], "total": 1, "limit": 50, "offset": 0}, response)

    def test_core_test_runtime_refuses_default_database(self):
        default_path = core.ROOT / "data" / "my-changli.db"
        with self.assertRaises(RuntimeError):
            core.configure_test_runtime(default_path)

    def test_reservation_is_atomic_and_cancel_restores_inventory(self):
        token_a = self.device_token("reservation-device-a")
        token_b = self.device_token("reservation-device-b")
        booking_date = (date.today() + timedelta(days=1)).isoformat()
        request = {
            "venueId": "badminton",
            "date": booking_date,
            "timeSlot": "09:00-10:00",
            "courtNumber": 3,
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda token: self.call("POST", "/api/v1/sports/reservations", request, token),
                [token_a, token_b],
            ))
        self.assertEqual([201, 409], sorted(result[0] for result in results))
        winner = next(result for result in results if result[0] == 201)
        winner_token = token_a if results[0][0] == 201 else token_b
        reservation_id = winner[1]["data"]["id"]
        self.assertEqual(200, self.call(
            "POST", f"/api/v1/sports/reservations/{reservation_id}/cancel", {}, winner_token
        )[0])
        loser_token = token_b if winner_token == token_a else token_a
        self.assertEqual(201, self.call(
            "POST", "/api/v1/sports/reservations", request, loser_token
        )[0])

    def test_lost_found_crud_search_favorite_and_ownership(self):
        owner = self.device_token("lost-owner")
        viewer = self.device_token("lost-viewer")
        payload = {
            "clientLocalId": "legacy-local-id",
            "type": "FOUND",
            "itemName": "蓝色雨伞",
            "category": "UMBRELLA",
            "location": "图书馆",
            "eventDate": date.today().isoformat(),
            "description": "伞柄有白色贴纸",
            "claimNote": "请说明特征",
        }
        status, created = self.call("POST", "/api/v1/lost-found", payload, owner)
        self.assertEqual(201, status)
        post_id = created["data"]["id"]
        status, search = self.call("GET", "/api/v1/lost-found?q=雨伞", token=viewer)
        self.assertEqual(200, status)
        self.assertEqual(post_id, search["data"][0]["id"])
        self.assertEqual(200, self.call("POST", f"/api/v1/lost-found/{post_id}/favorite", {}, viewer)[0])
        self.assertEqual(200, self.call("POST", f"/api/v1/lost-found/{post_id}/claim", {}, viewer)[0])
        self.assertEqual(201, self.call(
            "POST",
            f"/api/v1/lost-found/{post_id}/report",
            {"reason": "测试无效信息"},
            viewer,
        )[0])
        self.assertEqual(403, self.call("DELETE", f"/api/v1/lost-found/{post_id}", token=viewer)[0])
        self.assertEqual(200, self.call(
            "PATCH", f"/api/v1/lost-found/{post_id}", {"status": "RETURNED"}, owner
        )[0])
        self.assertEqual(200, self.call("DELETE", f"/api/v1/lost-found/{post_id}", token=owner)[0])
        self.assertEqual(404, self.call("GET", f"/api/v1/lost-found/{post_id}", token=owner)[0])

    def test_client_local_id_is_idempotent(self):
        token = self.device_token("migration-device")
        payload = {
            "clientLocalId": "same-local-id",
            "type": "LOST",
            "itemName": "校园卡",
            "category": "CARD",
            "location": "教学楼",
            "eventDate": date.today().isoformat(),
        }
        first = self.call("POST", "/api/v1/lost-found", payload, token)
        second = self.call("POST", "/api/v1/lost-found", payload, token)
        self.assertEqual(first[1]["data"]["id"], second[1]["data"]["id"])

    def test_admin_foundation_is_protected_and_configurable(self):
        ordinary = self.device_token("ordinary-admin-check")
        self.assertEqual(403, self.call("GET", "/api/v1/admin/venues", token=ordinary)[0])
        generated_secret = secrets.token_urlsafe(24)
        digest, salt = server.password_record(generated_secret)
        admin_id = "test-admin-id"
        with server.connection() as db:
            db.execute(
                """INSERT OR REPLACE INTO users(
                       id,username,password_hash,password_salt,role,account_type,nickname,
                       admin_permissions_json,admin_role,admin_enabled,account_status,created_at
                   ) VALUES(?,?,?,?, 'ADMIN','ADMIN','测试管理员','["*"]',
                            'SUPER_ADMIN',1,'ACTIVE',?)""",
                (admin_id, "test-admin", digest, salt, server.now_ms()),
            )
        status, login = self.call(
            "POST",
            "/api/v1/auth/login",
            {"username": "test-admin", "password": generated_secret},
        )
        self.assertEqual(200, status)
        token = login["data"]["token"]
        self.assertEqual(200, self.call(
            "PATCH",
            "/api/v1/admin/venues/badminton",
            {"priceYuan": 22, "courtCount": 8, "enabled": True},
            token,
        )[0])
        self.assertEqual(200, self.call(
            "PUT",
            "/api/v1/admin/flags/lost_found",
            {"enabled": True, "payload": {"maintenance": False}},
            token,
        )[0])

        self.assertEqual(200, self.call(
            "PATCH",
            "/api/v1/admin/venues/badminton",
            {
                "name": "羽毛球馆",
                "priceYuan": 22,
                "courtCount": 8,
                "enabled": True,
                "operatingStatus": "MAINTENANCE",
                "maintenanceNote": "测试维护",
            },
            token,
        )[0])
        status, venues = self.call("GET", "/api/v1/admin/venues", token=token)
        self.assertEqual(200, status)
        badminton = next(item for item in venues["data"] if item["id"] == "badminton")
        self.assertEqual("MAINTENANCE", badminton["operating_status"])
        self.assertTrue(badminton["timeSlots"])

        booking_date = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(200, self.call(
            "POST",
            "/api/v1/admin/inventory/block",
            {
                "venueId": "badminton",
                "date": booking_date,
                "timeSlot": "09:00-10:00",
                "courtNumber": 1,
                "reason": "单元测试",
            },
            token,
        )[0])
        status, inventory = self.call(
            "GET",
            f"/api/v1/admin/inventory?venueId=badminton&date={booking_date}",
            token=token,
        )
        self.assertEqual(200, status)
        self.assertEqual(1, len(inventory["data"]))
        block_id = inventory["data"][0]["id"]
        self.assertEqual(200, self.call(
            "DELETE", f"/api/v1/admin/inventory/block/{block_id}", token=token
        )[0])

        status, announcement = self.call(
            "POST",
            "/api/v1/admin/announcements",
            {"title": "测试公告", "content": "本地后台测试", "enabled": True},
            token,
        )
        self.assertEqual(201, status)
        announcement_id = announcement["data"]["id"]
        self.assertEqual(200, self.call(
            "PATCH",
            f"/api/v1/admin/announcements/{announcement_id}",
            {"enabled": False},
            token,
        )[0])
        status, config = self.call("GET", "/api/v1/admin/config", token=token)
        self.assertEqual(200, status)
        self.assertIn("flags", config["data"])
        self.assertIn("version", config["data"])
        self.assertEqual(200, self.call(
            "PATCH",
            "/api/v1/admin/venues/badminton",
            {"operatingStatus": "OPEN", "maintenanceNote": ""},
            token,
        )[0])

        lost_payload = {
            "type": "FOUND",
            "itemName": "后台测试物品",
            "category": "OTHER",
            "location": "测试地点",
            "eventDate": date.today().isoformat(),
            "status": "WAITING",
        }
        status, created = self.call(
            "POST", "/api/v1/lost-found", lost_payload, ordinary
        )
        self.assertEqual(201, status)
        lost_id = created["data"]["id"]
        self.assertEqual(200, self.call(
            "PATCH",
            f"/api/v1/admin/lost-found/{lost_id}",
            {"status": "RETURNED"},
            token,
        )[0])
        self.assertEqual(200, self.call(
            "DELETE", f"/api/v1/admin/lost-found/{lost_id}", token=token
        )[0])
        self.assertEqual(404, self.call(
            "GET", f"/api/v1/lost-found/{lost_id}", token=ordinary
        )[0])

        self.assertEqual(200, self.call("POST", "/api/v1/auth/logout", {}, token)[0])
        self.assertEqual(401, self.call("GET", "/api/v1/admin/overview", token=token)[0])

    def test_admin_edit_permissions_and_last_super_admin_protection(self):
        admin_token = self.admin_token()
        with server.connection() as db:
            admin_id = db.execute(
                "SELECT id FROM users WHERE username LIKE 'admin-%' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()["id"]

        second_id = "super-two-" + secrets.token_hex(8)
        second_username = "super-two-" + secrets.token_hex(6)
        second_digest, second_salt = server.password_record(secrets.token_urlsafe(24))
        with server.connection() as db:
            db.execute(
                """INSERT INTO users(
                       id,username,password_hash,password_salt,role,account_type,nickname,
                       admin_permissions_json,admin_role,admin_enabled,account_status,created_at
                   ) VALUES(?,?,?,?, 'ADMIN','ADMIN','第二超级管理员','["*"]','SUPER_ADMIN',1,'ACTIVE',?)""",
                (second_id, second_username, second_digest, second_salt, server.now_ms()),
            )

        self.assertEqual(
            200,
            self.call(
                "PATCH",
                f"/api/v1/admin/admins/{second_id}",
                {"nickname": "第二超级管理员-已编辑", "adminRole": "SUPER_ADMIN", "enabled": True},
                admin_token,
            )[0],
        )
        self.assertEqual(
            409,
            self.call(
                "PATCH",
                f"/api/v1/admin/admins/{admin_id}",
                {"adminRole": "SPORTS_OPERATOR", "enabled": True},
                admin_token,
            )[0],
        )
        self.assertEqual(
            409,
            self.call(
                "PATCH",
                f"/api/v1/admin/admins/{admin_id}",
                {"adminRole": "SUPER_ADMIN", "enabled": False},
                admin_token,
            )[0],
        )
        self.assertEqual(
            409,
            self.call(
                "PATCH",
                f"/api/v1/admin/admins/{admin_id}",
                {"nickname": "不应成功", "adminRole": "SUPER_ADMIN", "enabled": True},
                admin_token,
            )[0],
        )
        self.assertEqual(
            200,
            self.call(
                "PATCH",
                f"/api/v1/admin/admins/{second_id}",
                {"adminRole": "SUPER_ADMIN", "enabled": False},
                admin_token,
            )[0],
        )
        self.assertEqual(
            200,
            self.call(
                "PATCH",
                f"/api/v1/admin/admins/{second_id}",
                {"adminRole": "SUPER_ADMIN", "enabled": True},
                admin_token,
            )[0],
        )

        operator_password = secrets.token_urlsafe(18)
        operator_username = "operator-edit-" + secrets.token_hex(3)
        self.assertEqual(
            201,
            self.call(
                "POST",
                "/api/v1/admin/admins",
                {
                    "username": operator_username,
                    "nickname": "体育运营编辑测试",
                    "password": operator_password,
                    "adminRole": "SPORTS_OPERATOR",
                },
                admin_token,
            )[0],
        )
        _, operator_login = self.call(
            "POST",
            "/api/v1/auth/login",
            {"username": operator_username, "password": operator_password},
        )
        self.assertEqual(
            403,
            self.call("GET", "/api/v1/admin/admins", token=operator_login["data"]["token"])[0],
        )
        self.assertEqual(
            403,
            self.call(
                "PATCH",
                f"/api/v1/admin/admins/{second_id}",
                {"adminRole": "SUPER_ADMIN", "enabled": True},
                operator_login["data"]["token"],
            )[0],
        )

    def test_admin_web_assets_are_served_locally(self):
        for path, expected in (
            ("/admin/", "本地管理后台"),
            ("/admin/styles.css", "--blue"),
            ("/admin/app.js", "sessionStorage"),
        ):
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(200, response.status)
                self.assertIn(expected, response.read().decode("utf-8"))

    def test_platform_metrics_user_events_status_and_risk_are_separated(self):
        phone, password, user_token, user = self.phone_account()
        admin = self.admin_token()
        self.assertEqual(200, self.call("GET", "/api/v1/auth/me", token=user_token)[0])
        for _ in range(5):
            self.assertEqual(401, self.call(
                "POST", "/api/v1/auth/login", {"phone": phone, "password": "wrong-password"}
            )[0])

        status, overview = self.call("GET", "/api/v1/admin/overview", token=admin)
        self.assertEqual(200, status)
        self.assertGreaterEqual(overview["data"]["registeredAccounts"], 1)
        self.assertGreaterEqual(overview["data"]["dau"], 1)
        self.assertGreaterEqual(overview["data"]["loginFailures24h"], 5)
        self.assertGreaterEqual(overview["data"]["riskCandidates"], 1)

        status, events = self.call(
            "GET", f"/api/v1/admin/user-events?user={user['id']}&eventType=LOGIN_FAILURE",
            token=admin,
        )
        self.assertEqual(200, status)
        self.assertGreaterEqual(events["data"]["total"], 5)
        serialized = json.dumps(events["data"], ensure_ascii=False)
        self.assertNotIn(password, serialized)
        self.assertNotIn("wrong-password", serialized)

        status, candidates = self.call("GET", "/api/v1/admin/risk-candidates?status=OPEN", token=admin)
        self.assertEqual(200, status)
        candidate = next(row for row in candidates["data"] if row["user_id"] == user["id"])
        self.assertEqual(200, self.call(
            "PATCH", f"/api/v1/admin/risk-candidates/{candidate['id']}",
            {"status": "WATCHING", "reason": "本地自动化复核"}, admin,
        )[0])

        self.assertEqual(200, self.call(
            "PATCH", f"/api/v1/admin/users/{user['id']}",
            {"accountStatus": "LIMITED", "reason": "本地测试限制"}, admin,
        )[0])
        status, rows = self.call("GET", "/api/v1/admin/users?status=LIMITED", token=admin)
        self.assertEqual(200, status)
        self.assertTrue(any(row["id"] == user["id"] for row in rows["data"]))
        self.assertEqual(200, self.call("GET", "/api/v1/auth/me", token=user_token)[0])
        self.assertEqual(403, self.call(
            "POST", "/api/v1/feedback",
            {"title": "受限验证", "category": "OTHER", "content": "该请求应被服务端拒绝"},
            user_token,
        )[0])

        status, audits = self.call("GET", "/api/v1/admin/audit-logs", token=admin)
        self.assertEqual(200, status)
        self.assertTrue(any(row["action"] == "RISK_CANDIDATE_REVIEWED" for row in audits["data"]))
        self.assertTrue(any(row["action"] == "USER_STATUS_UPDATED" for row in audits["data"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
