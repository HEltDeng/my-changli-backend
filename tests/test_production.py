import base64
import io
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet
from PIL import Image

import core
import server
from logging_config import JsonLogFormatter, redact
from runtime_config import ConfigurationError, load_runtime_config
from scripts.database_tools import backup_database, restore_database, verify_backup
from wsgi import application


ROOT = Path(__file__).resolve().parents[1]


class ProductionUnitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "database" / "test.db"
        core.configure_test_runtime(self.database)
        server.DB_PATH = self.database
        server.UPLOADS = self.root / "uploads"
        server.phone_hash_secret.cache_clear()
        server.pii_cipher.cache_clear()
        server.RATE_LIMITER.reset()
        server.initialize_database()

    def tearDown(self):
        server.phone_hash_secret.cache_clear()
        server.pii_cipher.cache_clear()
        server.RATE_LIMITER.reset()
        self.temp.cleanup()

    def wsgi_call(
        self,
        method,
        path,
        data=None,
        token=None,
        headers=None,
        raw_body=None,
        content_length=None,
    ):
        body = (
            raw_body
            if raw_body is not None
            else json.dumps(data).encode("utf-8") if data is not None else b""
        )
        parts = path.split("?", 1)
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": parts[0],
            "QUERY_STRING": parts[1] if len(parts) == 2 else "",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "8787",
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_HOST": "localhost:8787",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": (
                str(content_length)
                if content_length is not None
                else str(len(body)) if body else ""
            ),
            "wsgi.input": io.BytesIO(body),
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }
        if token:
            environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        for key, value in (headers or {}).items():
            environ["HTTP_" + key.upper().replace("-", "_")] = value
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = int(status.split()[0])
            captured["headers"] = dict(response_headers)

        payload = b"".join(application(environ, start_response))
        return captured["status"], json.loads(payload), captured["headers"]

    def test_production_config_fails_closed_and_accepts_complete_values(self):
        with self.assertRaises(ConfigurationError):
            load_runtime_config({"MY_CHANGLI_ENV": "production"})
        environment = {
            "MY_CHANGLI_ENV": "production",
            "MY_CHANGLI_DEBUG": "false",
            "MY_CHANGLI_HOST": "127.0.0.1",
            "MY_CHANGLI_PORT": "18787",
            "MY_CHANGLI_DB_PATH": str(self.root / "state" / "production.db"),
            "MY_CHANGLI_UPLOAD_PATH": str(self.root / "state" / "uploads"),
            "MY_CHANGLI_LOG_PATH": str(self.root / "logs" / "backend.jsonl"),
            "MY_CHANGLI_BACKUP_PATH": str(self.root / "backups"),
            "MY_CHANGLI_PHONE_HASH_SECRET": secrets.token_urlsafe(48),
            "MY_CHANGLI_PII_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "MY_CHANGLI_ALLOWED_HOSTS": "api.example.test",
            "MY_CHANGLI_ADMIN_ORIGIN": "https://api.example.test",
            "MY_CHANGLI_TRUSTED_PROXY": "127.0.0.1",
        }
        config = load_runtime_config(environment)
        self.assertTrue(config.is_production)
        self.assertFalse(config.debug)
        self.assertTrue(config.database_path.is_absolute())
        with self.assertRaises(ConfigurationError):
            load_runtime_config(dict(environment, MY_CHANGLI_DEBUG="true"))
        with self.assertRaises(ConfigurationError):
            load_runtime_config(dict(environment, MY_CHANGLI_PHONE_HASH_SECRET="PLACEHOLDER"))
        with self.assertRaises(ConfigurationError):
            load_runtime_config(dict(environment, MY_CHANGLI_TRUSTED_PROXY="10.0.0.2"))
        with self.assertRaises(ConfigurationError):
            load_runtime_config(dict(environment, MY_CHANGLI_ADMIN_ORIGIN="https://admin.example.test"))

    def test_clean_database_wal_integrity_and_default_state(self):
        server.initialize_database()
        with server.connection() as database:
            self.assertEqual("wal", database.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, database.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual("ok", database.execute("PRAGMA integrity_check").fetchone()[0])
            for table in (
                "users",
                "reservations",
                "lost_found_posts",
                "feedback",
                "announcements",
            ):
                self.assertEqual(0, database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            version = database.execute(
                "SELECT status FROM app_versions WHERE platform='android'"
            ).fetchone()
            self.assertEqual("DRAFT", version["status"])
            self.assertEqual(5, database.execute("SELECT COUNT(*) FROM resource_items").fetchone()[0])

    def test_consistent_backup_verification_and_restore(self):
        with server.connection() as database:
            database.execute(
                "INSERT INTO announcements(id,title,content,published_at,updated_at) VALUES('before','before','before',1,1)"
            )
        result = backup_database(self.database, self.root / "backups", retention_days=14)
        verification = verify_backup(result["backup"])
        self.assertTrue(verification["valid"])
        self.assertTrue(verification["manifestMatches"])
        with server.connection() as database:
            database.execute("DELETE FROM announcements")
            database.execute(
                "INSERT INTO announcements(id,title,content,published_at,updated_at) VALUES('after','after','after',2,2)"
            )
        restored = restore_database(result["backup"], self.database, self.root / "safety")
        self.assertTrue(restored["safetyBackup"].is_file())
        with closing(sqlite3.connect(self.database)) as database:
            ids = {row[0] for row in database.execute("SELECT id FROM announcements")}
        self.assertEqual({"before"}, ids)

    def test_wsgi_health_readiness_and_security_headers(self):
        for path, expected in (("/health", "ok"), ("/ready", "ready")):
            status, body, headers = self.wsgi_call("GET", path)
            self.assertEqual(200, status)
            self.assertEqual(expected, body["data"]["status"])
            self.assertNotIn("path", json.dumps(body).lower())
            self.assertEqual("nosniff", headers["X-Content-Type-Options"])
            self.assertTrue(headers["X-Request-ID"])

    def test_request_boundaries_reject_malformed_oversized_and_traversal_input(self):
        status, body, _ = self.wsgi_call(
            "POST", "/api/v1/auth/device", raw_body=b"{"
        )
        self.assertEqual(400, status)
        self.assertEqual("BAD_JSON", body["error"]["code"])

        status, body, _ = self.wsgi_call(
            "POST",
            "/api/v1/auth/device",
            raw_body=b"{}",
            content_length=core.MAX_BODY + 1,
        )
        self.assertEqual(400, status)
        self.assertEqual("BAD_REQUEST", body["error"]["code"])

        status, body, _ = self.wsgi_call("GET", "/uploads/../server.py")
        self.assertEqual(404, status)
        self.assertFalse(body["ok"])

    def test_logging_redacts_credentials_and_pii(self):
        secret = secrets.token_urlsafe(24)
        value = {
            "password": secret,
            "Authorization": f"Bearer {secret}",
            "phone": "13800138000",
            "safe": "retained",
        }
        redacted = json.dumps(redact(value), ensure_ascii=False)
        self.assertNotIn(secret, redacted)
        self.assertNotIn("13800138000", redacted)
        self.assertIn("retained", redacted)
        formatter = JsonLogFormatter()
        import logging

        record = logging.LogRecord("test", logging.INFO, __file__, 1, f"token={secret}", (), None)
        self.assertNotIn(secret, formatter.format(record))

    def test_login_rate_limit_blocks_repeated_brute_force(self):
        for _ in range(8):
            status, _, _ = self.wsgi_call(
                "POST", "/api/v1/auth/login", {"username": "missing-admin", "password": "wrong-password"}
            )
            self.assertEqual(401, status)
        status, body, _ = self.wsgi_call(
            "POST", "/api/v1/auth/login", {"username": "missing-admin", "password": "wrong-password"}
        )
        self.assertEqual(429, status)
        self.assertEqual("RATE_LIMITED", body["error"]["code"])

    def test_upload_requires_authentication_and_normalizes_image(self):
        image = Image.new("RGB", (20, 20), "red")
        encoded = io.BytesIO()
        image.save(encoded, "PNG", pnginfo=None)
        image_base64 = base64.b64encode(encoded.getvalue()).decode()
        payload = {
            "type": "LOST",
            "itemName": "测试图片",
            "category": "OTHER",
            "location": "测试地点",
            "eventDate": "2026-08-11",
            "imageBase64": image_base64,
        }
        status, _, _ = self.wsgi_call("POST", "/api/v1/lost-found", payload)
        self.assertEqual(401, status)
        self.assertEqual([], list(server.UPLOADS.glob("*")))

        _, auth, _ = self.wsgi_call(
            "POST", "/api/v1/auth/device", {"installationId": "TEST_ONLY_UPLOAD"}
        )
        status, created, _ = self.wsgi_call(
            "POST", "/api/v1/lost-found", payload, auth["data"]["token"]
        )
        self.assertEqual(201, status)
        image_name = created["data"]["imageUrl"].rsplit("/", 1)[-1]
        stored = server.UPLOADS / image_name
        self.assertTrue(stored.is_file())
        with Image.open(stored) as normalized:
            self.assertEqual("PNG", normalized.format)
            self.assertEqual((20, 20), normalized.size)

        invalid = dict(payload, imageBase64=base64.b64encode(b"<script>alert(1)</script>").decode())
        status, body, _ = self.wsgi_call(
            "POST", "/api/v1/lost-found", invalid, auth["data"]["token"]
        )
        self.assertEqual(400, status)
        self.assertEqual("INVALID_IMAGE", body["error"]["code"])


class ProductionRuntimeSmokeTest(unittest.TestCase):
    def test_waitress_production_runtime_bootstrap_and_health(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("MY_CHANGLI_")
            }
            bootstrap_password = secrets.token_urlsafe(24)
            phone_secret = secrets.token_urlsafe(48)
            pii_key = Fernet.generate_key().decode()
            environment.update({
                "PYTHONUTF8": "1",
                "MY_CHANGLI_ENV": "production",
                "MY_CHANGLI_DEBUG": "false",
                "MY_CHANGLI_HOST": "127.0.0.1",
                "MY_CHANGLI_PORT": str(port),
                "MY_CHANGLI_DB_PATH": str(temp / "state" / "production.db"),
                "MY_CHANGLI_UPLOAD_PATH": str(temp / "state" / "uploads"),
                "MY_CHANGLI_LOG_PATH": str(temp / "logs" / "backend.jsonl"),
                "MY_CHANGLI_BACKUP_PATH": str(temp / "backups"),
                "MY_CHANGLI_PHONE_HASH_SECRET": phone_secret,
                "MY_CHANGLI_PII_ENCRYPTION_KEY": pii_key,
                "MY_CHANGLI_ALLOWED_HOSTS": "127.0.0.1",
                "MY_CHANGLI_ADMIN_ORIGIN": "https://127.0.0.1",
                "MY_CHANGLI_TRUSTED_PROXY": "127.0.0.1",
                "MY_CHANGLI_SUPER_ADMIN_USERNAME": "TEST_ONLY_SUPER_ADMIN",
                "MY_CHANGLI_SUPER_ADMIN_PASSWORD": bootstrap_password,
            })
            placeholder_environment = dict(environment)
            placeholder_environment["MY_CHANGLI_SUPER_ADMIN_USERNAME"] = (
                "YOUR_ONE_TIME_SUPER_ADMIN_USERNAME"
            )
            placeholder_environment["MY_CHANGLI_SUPER_ADMIN_PASSWORD"] = (
                "YOUR_ONE_TIME_LONG_RANDOM_PASSWORD"
            )
            rejected_bootstrap = subprocess.run(
                [sys.executable, "manage.py", "bootstrap-production"],
                cwd=ROOT,
                env=placeholder_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertNotEqual(0, rejected_bootstrap.returncode)
            bootstrap = subprocess.run(
                [sys.executable, "manage.py", "bootstrap-production"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(0, bootstrap.returncode, bootstrap.stderr)
            environment.pop("MY_CHANGLI_SUPER_ADMIN_PASSWORD")
            process = subprocess.Popen(
                [sys.executable, "production.py"],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                deadline = time.time() + 20
                last_error = None
                while time.time() < deadline:
                    try:
                        request = urllib.request.Request(f"http://127.0.0.1:{port}/ready")
                        with urllib.request.urlopen(request, timeout=1) as response:
                            body = json.loads(response.read())
                            self.assertEqual("ready", body["data"]["status"])
                            break
                    except (OSError, urllib.error.URLError) as exc:
                        last_error = exc
                        time.sleep(0.2)
                else:
                    stdout, stderr = process.communicate(timeout=2)
                    self.fail(f"Production runtime did not become ready: {last_error}\n{stdout}\n{stderr}")

                def json_request(path, *, method="GET", data=None, token=None):
                    encoded = json.dumps(data).encode("utf-8") if data is not None else None
                    headers = {"Content-Type": "application/json"} if encoded else {}
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}",
                        data=encoded,
                        headers=headers,
                        method=method,
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=2) as response:
                            return response.status, json.loads(response.read())
                    except urllib.error.HTTPError as error:
                        return error.code, json.loads(error.read())

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/v1/public/bootstrap", timeout=2
                ) as response:
                    self.assertEqual(200, response.status)
                    public_bootstrap = json.loads(response.read())
                    self.assertTrue(public_bootstrap["ok"])
                    self.assertEqual(5, len(public_bootstrap["data"]["flags"]))

                login_status, login = json_request(
                    "/api/v1/auth/login",
                    method="POST",
                    data={
                        "username": "TEST_ONLY_SUPER_ADMIN",
                        "password": bootstrap_password,
                    },
                )
                self.assertEqual(200, login_status)
                admin_token = login["data"]["token"]
                self.assertEqual(
                    200,
                    json_request("/api/v1/admin/overview", token=admin_token)[0],
                )

                device_status, device = json_request(
                    "/api/v1/auth/device",
                    method="POST",
                    data={"installationId": "TEST_ONLY_PRODUCTION_SMOKE"},
                )
                self.assertEqual(200, device_status)
                user_token = device["data"]["token"]
                self.assertEqual(
                    403,
                    json_request("/api/v1/admin/overview", token=user_token)[0],
                )
                self.assertEqual(
                    200,
                    json_request("/api/v1/lost-found", token=user_token)[0],
                )
                version_status, version = json_request(
                    "/api/v1/public/version/android?currentVersionCode=1"
                )
                self.assertEqual(200, version_status)
                self.assertFalse(version["data"]["updateAvailable"])
                invalid_host = urllib.request.Request(
                    f"http://127.0.0.1:{port}/health",
                    headers={"Host": "evil.example"},
                )
                with self.assertRaises(urllib.error.HTTPError) as host_error:
                    urllib.request.urlopen(invalid_host, timeout=2)
                self.assertEqual(400, host_error.exception.code)

                invalid_origin = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/admin/unknown",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://evil.example",
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as origin_error:
                    urllib.request.urlopen(invalid_origin, timeout=2)
                self.assertEqual(403, origin_error.exception.code)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                process.communicate(timeout=2)
            log_text = (temp / "logs" / "backend.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(bootstrap_password, log_text)
            self.assertNotIn(phone_secret, log_text)
            self.assertNotIn(pii_key, log_text)


if __name__ == "__main__":
    unittest.main()
