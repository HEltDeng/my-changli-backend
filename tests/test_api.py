import concurrent.futures
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
        server.DB_PATH = Path(cls.temp.name) / "test.db"
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
                       id,username,password_hash,password_salt,role,account_type,nickname,created_at
                   ) VALUES(?,?,?,?, 'ADMIN','ADMIN','测试管理员',?)""",
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
        self.assertEqual("FEATURE_UNAVAILABLE", error["error"]["code"])
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
            {"category": "建议", "content": "本地自动化反馈", "appVersion": "test"},
            user_token,
        )
        self.assertEqual(201, status)
        feedback_id = feedback["data"]["id"]
        self.assertEqual(200, self.call(
            "PATCH",
            f"/api/v1/admin/feedback/{feedback_id}",
            {"status": "RESOLVED"},
            admin,
        )[0])
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

    def test_health_and_bootstrap(self):
        self.assertEqual(200, self.call("GET", "/health")[0])
        status, body = self.call("GET", "/api/v1/public/bootstrap")
        self.assertEqual(200, status)
        self.assertTrue(any(flag["key"] == "sports_reservation" for flag in body["data"]["flags"]))

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
                """INSERT OR REPLACE INTO users(id,username,password_hash,password_salt,role,created_at)
                   VALUES(?,?,?,?, 'ADMIN', ?)""",
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

    def test_admin_web_assets_are_served_locally(self):
        for path, expected in (
            ("/admin/", "本地管理后台"),
            ("/admin/styles.css", "--blue"),
            ("/admin/app.js", "sessionStorage"),
        ):
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(200, response.status)
                self.assertIn(expected, response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
