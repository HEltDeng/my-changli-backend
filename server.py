"""My Changli campus-tools backend MVP.

Standard-library-only HTTP + SQLite service for local development. It deliberately
does not include payment, reservation codes, public deployment, or academic-system
credentials.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from functools import lru_cache
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("MY_CHANGLI_DB_PATH", ROOT / "data" / "my-changli.db"))
UPLOADS = ROOT / "uploads"
ADMIN_WEB = ROOT / "admin"
HOST = os.getenv("MY_CHANGLI_HOST", "127.0.0.1")
PORT = int(os.getenv("MY_CHANGLI_PORT", "8787"))
MAX_BODY = 4 * 1024 * 1024
SESSION_SECONDS = 30 * 24 * 60 * 60
SPORTS = {"BADMINTON", "TABLE_TENNIS"}
LOST_TYPES = {"LOST", "FOUND"}
LOST_STATUSES = {
    "LOST": {"SEARCHING", "FOUND"},
    "FOUND": {"WAITING", "CLAIMED", "RETURNED"},
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def now_ms() -> int:
    return int(time.time() * 1000)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(
        DB_PATH,
        timeout=10,
        isolation_level=None,
        factory=ClosingConnection,
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    return db


def ensure_column(
    db: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {
        row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def initialize_database() -> None:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        ensure_column(db, "venues", "operating_status", "TEXT NOT NULL DEFAULT 'OPEN'")
        ensure_column(db, "venues", "maintenance_note", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "venues", "expected_restore_at", "INTEGER")
        ensure_column(db, "users", "account_type", "TEXT NOT NULL DEFAULT 'DEVICE'")
        ensure_column(db, "users", "nickname", "TEXT NOT NULL DEFAULT '长理同学'")
        ensure_column(db, "users", "phone_hash", "TEXT")
        ensure_column(db, "users", "phone_masked", "TEXT")
        ensure_column(db, "users", "student_id", "TEXT")
        ensure_column(db, "users", "real_name", "TEXT")
        ensure_column(db, "users", "campus_verified", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "users", "campus_verified_at", "INTEGER")
        ensure_column(db, "users", "account_status", "TEXT NOT NULL DEFAULT 'ACTIVE'")
        ensure_column(db, "reservations", "refund_status", "TEXT NOT NULL DEFAULT 'NOT_APPLICABLE'")
        ensure_column(db, "reservations", "reservation_code", "TEXT")
        ensure_column(db, "reservations", "verification_status", "TEXT NOT NULL DEFAULT 'NOT_REQUIRED'")
        ensure_column(db, "reservations", "cancelled_at", "INTEGER")
        ensure_column(db, "reservations", "cancelled_by", "TEXT")
        ensure_column(db, "reservations", "cancel_reason", "TEXT")
        ensure_column(db, "feedback", "workflow_status", "TEXT NOT NULL DEFAULT 'OPEN'")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_phone_hash ON users(phone_hash) WHERE phone_hash IS NOT NULL")
        db.execute(
            """UPDATE users SET account_type=CASE
               WHEN role='ADMIN' THEN 'ADMIN'
               WHEN username LIKE 'device_%' THEN 'DEVICE'
               ELSE account_type END"""
        )
        db.execute(
            "UPDATE feedback SET workflow_status=status WHERE workflow_status IS NULL OR workflow_status=''"
        )
        timestamp = now_ms()
        venues = [
            ("badminton", "BADMINTON", "羽毛球馆", "场", 8, 20),
            ("table-tennis", "TABLE_TENNIS", "乒乓球馆", "台", 10, 10),
        ]
        slots = [
            ("08:00", "09:00"), ("09:00", "10:00"), ("10:00", "11:00"),
            ("14:00", "15:00"), ("15:00", "16:00"), ("16:00", "17:00"),
            ("19:00", "20:00"), ("20:00", "21:00"),
        ]
        for venue_id, sport, name, unit, count, price in venues:
            db.execute(
                """INSERT INTO venues(id,sport,name,unit_label,court_count,price_yuan,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO NOTHING""",
                (venue_id, sport, name, unit, count, price, timestamp),
            )
            for start, end in slots:
                db.execute(
                    """INSERT INTO venue_time_slots(id,venue_id,start_time,end_time)
                       VALUES(?,?,?,?) ON CONFLICT(venue_id,start_time,end_time) DO NOTHING""",
                    (f"{venue_id}-{start}", venue_id, start, end),
                )
        db.execute(
            """INSERT INTO app_versions(platform,latest_version_code,minimum_version_code,
                       latest_version_name,update_message,updated_at)
               VALUES('android',31,24,'2.0.9','',?)
               ON CONFLICT(platform) DO NOTHING""",
            (timestamp,),
        )
        for key, enabled in (
            ("sports_reservation", 1),
            ("lost_found", 1),
            ("campus_channel", 0),
            ("buddy_club", 0),
        ):
            db.execute(
                """INSERT INTO remote_flags(key,enabled,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO NOTHING""",
                (key, enabled, timestamp),
            )
        db.execute(
            """INSERT INTO reservation_rules(
                   id,max_active_per_day,max_user_cancellations_per_day,
                   cancel_cutoff_minutes,restriction_hours_after_excess_cancel,updated_at
               ) VALUES('default',2,2,30,24,?)
               ON CONFLICT(id) DO NOTHING""",
            (timestamp,),
        )
        for feature_key in ("sports_reservation", "lost_found"):
            row = db.execute(
                "SELECT payload_json FROM remote_flags WHERE key=?", (feature_key,)
            ).fetchone()
            payload = json.loads(row["payload_json"] or "{}") if row else {}
            if "status" not in payload:
                payload.update(
                    {
                        "status": "OPEN",
                        "maintenanceNote": "",
                        "expectedRestoreAt": None,
                    }
                )
                db.execute(
                    "UPDATE remote_flags SET payload_json=?,updated_at=? WHERE key=?",
                    (json.dumps(payload, ensure_ascii=False), timestamp, feature_key),
                )
    bootstrap_admin()


def password_record(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )
    return base64.b64encode(digest).decode(), base64.b64encode(salt).decode()


def verify_password(password: str, encoded_hash: str, encoded_salt: str) -> bool:
    expected, _ = password_record(password, base64.b64decode(encoded_salt))
    return hmac.compare_digest(expected, encoded_hash)


def bootstrap_admin() -> None:
    username = os.getenv("MY_CHANGLI_ADMIN_USERNAME", "").strip()
    password = os.getenv("MY_CHANGLI_ADMIN_PASSWORD", "")
    if not username or len(password) < 12:
        return
    digest, salt = password_record(password)
    with connection() as db:
        db.execute(
            """INSERT INTO users(
                   id,username,password_hash,password_salt,role,account_type,nickname,created_at
               ) VALUES(?,?,?,?, 'ADMIN','ADMIN','本地管理员',?)
               ON CONFLICT(username) DO NOTHING""",
            (str(uuid.uuid4()), username, digest, salt, now_ms()),
        )


def issue_token(db: sqlite3.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
        (hashlib.sha256(token.encode()).hexdigest(), user_id, now_ms() + SESSION_SECONDS * 1000, now_ms()),
    )
    return token


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def required_text(data: dict[str, Any], key: str, maximum: int) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ApiError(400, "VALIDATION_ERROR", f"{key} 不能为空")
    if len(value) > maximum:
        raise ApiError(400, "VALIDATION_ERROR", f"{key} 长度不能超过 {maximum}")
    return value


def parse_iso_date(value: str, *, future_allowed: bool = True) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(400, "INVALID_DATE", "日期格式必须为 YYYY-MM-DD") from exc
    if not future_allowed and parsed > date.today():
        raise ApiError(400, "INVALID_DATE", "日期不能晚于今天")
    return parsed.isoformat()


def normalize_phone(value: Any) -> str:
    phone = re.sub(r"\s+", "", str(value or ""))
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise ApiError(400, "INVALID_PHONE", "请输入有效的 11 位手机号")
    return phone


@lru_cache(maxsize=1)
def phone_hash_secret() -> bytes:
    configured = os.getenv("MY_CHANGLI_PHONE_HASH_SECRET", "").encode()
    if len(configured) >= 32:
        return configured
    secret_path = DB_PATH.parent / ".phone-hash-secret"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if secret_path.is_file():
        existing = secret_path.read_bytes()
        if len(existing) >= 32:
            return existing
    generated = secrets.token_bytes(48)
    secret_path.write_bytes(generated)
    return generated


def phone_digest(phone: str) -> str:
    return hmac.new(phone_hash_secret(), phone.encode(), hashlib.sha256).hexdigest()


def phone_mask(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}"


def device_username(installation_id: str) -> str:
    return "device_" + hashlib.sha256(installation_id.encode()).hexdigest()[:32]


def migrate_device_data(
    db: sqlite3.Connection, installation_id: str | None, target_user_id: str
) -> bool:
    if not installation_id:
        return False
    source = db.execute(
        "SELECT * FROM users WHERE username=? AND account_type='DEVICE'",
        (device_username(installation_id),),
    ).fetchone()
    if not source or source["id"] == target_user_id:
        return False
    source_id = source["id"]
    db.execute(
        """UPDATE lost_found_posts SET client_local_id=NULL
           WHERE owner_user_id=? AND client_local_id IN (
               SELECT client_local_id FROM lost_found_posts
               WHERE owner_user_id=? AND client_local_id IS NOT NULL
           )""",
        (source_id, target_user_id),
    )
    db.execute("UPDATE reservations SET user_id=? WHERE user_id=?", (target_user_id, source_id))
    db.execute(
        "UPDATE lost_found_posts SET owner_user_id=? WHERE owner_user_id=?",
        (target_user_id, source_id),
    )
    db.execute("UPDATE feedback SET user_id=? WHERE user_id=?", (target_user_id, source_id))
    for table in ("lost_found_favorites", "lost_found_claims"):
        db.execute(
            f"""INSERT OR IGNORE INTO {table}(user_id,post_id,created_at)
                SELECT ?,post_id,created_at FROM {table} WHERE user_id=?""",
            (target_user_id, source_id),
        )
        db.execute(f"DELETE FROM {table} WHERE user_id=?", (source_id,))
    db.execute(
        """INSERT OR IGNORE INTO lost_found_reports(
               id,user_id,post_id,reason,status,created_at
           ) SELECT id,?,post_id,reason,status,created_at
             FROM lost_found_reports WHERE user_id=?""",
        (target_user_id, source_id),
    )
    db.execute("DELETE FROM lost_found_reports WHERE user_id=?", (source_id,))
    db.execute(
        "UPDATE reservation_events SET user_id=? WHERE user_id=?",
        (target_user_id, source_id),
    )
    db.execute("DELETE FROM sessions WHERE user_id=?", (source_id,))
    db.execute("DELETE FROM users WHERE id=?", (source_id,))
    return True


def serialize_user(user: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": user["id"],
        "role": user["role"],
        "accountType": user["account_type"],
        "nickname": user["nickname"],
        "phoneMasked": user["phone_masked"],
        "studentId": user["student_id"],
        "realName": user["real_name"],
        "campusVerified": bool(user["campus_verified"]),
        "campusVerifiedAt": user["campus_verified_at"],
        "accountStatus": user["account_status"],
    }


def write_audit(
    db: sqlite3.Connection,
    actor_user_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    db.execute(
        """INSERT INTO admin_audit_logs(
               id,actor_user_id,action,entity_type,entity_id,details_json,created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()),
            actor_user_id,
            action,
            entity_type,
            entity_id,
            json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
            now_ms(),
        ),
    )


def write_reservation_event(
    db: sqlite3.Connection,
    reservation_id: str | None,
    user_id: str | None,
    actor_user_id: str | None,
    action: str,
    reason: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    db.execute(
        """INSERT INTO reservation_events(
               id,reservation_id,user_id,actor_user_id,action,reason,details_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()),
            reservation_id,
            user_id,
            actor_user_id,
            action,
            reason[:300],
            json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
            now_ms(),
        ),
    )


def feature_state(db: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM remote_flags WHERE key=?", (key,)).fetchone()
    if not row:
        return {
            "key": key,
            "enabled": True,
            "status": "OPEN",
            "maintenanceNote": "",
            "expectedRestoreAt": None,
        }
    payload = json.loads(row["payload_json"] or "{}")
    status = str(payload.get("status", "OPEN")).upper()
    if status not in {"OPEN", "CLOSED", "MAINTENANCE"}:
        status = "OPEN"
    return {
        "key": key,
        "enabled": bool(row["enabled"]),
        "status": status,
        "maintenanceNote": str(payload.get("maintenanceNote", ""))[:300],
        "expectedRestoreAt": payload.get("expectedRestoreAt"),
    }


def ensure_feature_open(db: sqlite3.Connection, key: str, label: str) -> None:
    state = feature_state(db, key)
    if not state["enabled"] or state["status"] != "OPEN":
        note = state["maintenanceNote"] or f"{label}暂时不可用，请稍后再试"
        raise ApiError(503, "FEATURE_UNAVAILABLE", note)


def reservation_rules(db: sqlite3.Connection) -> sqlite3.Row:
    return db.execute(
        "SELECT * FROM reservation_rules WHERE id='default'"
    ).fetchone()


def active_restriction(
    db: sqlite3.Connection, user_id: str
) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM user_restrictions WHERE user_id=?", (user_id,)
    ).fetchone()


def upsert_restriction(
    db: sqlite3.Connection,
    user_id: str,
    reservation_blocked_until: int | None,
    cancellation_blocked_until: int | None,
    reason: str,
) -> None:
    db.execute(
        """INSERT INTO user_restrictions(
               user_id,reservation_blocked_until,cancellation_blocked_until,
               reason,updated_at
           ) VALUES(?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
           reservation_blocked_until=excluded.reservation_blocked_until,
           cancellation_blocked_until=excluded.cancellation_blocked_until,
           reason=excluded.reason,updated_at=excluded.updated_at""",
        (
            user_id,
            reservation_blocked_until,
            cancellation_blocked_until,
            reason[:300],
            now_ms(),
        ),
    )


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "MyChangliMVP/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never log authorization headers or request bodies.
        print(f"{self.log_date_time_string()} {self.client_address[0]} {fmt % args}")

    def send_json(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def body_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(400, "BAD_REQUEST", "请求长度无效") from exc
        if length <= 0 or length > MAX_BODY:
            raise ApiError(400, "BAD_REQUEST", "请求内容为空或过大")
        try:
            body = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "BAD_JSON", "请求必须是有效 JSON") from exc
        if not isinstance(body, dict):
            raise ApiError(400, "BAD_JSON", "请求 JSON 必须是对象")
        return body

    def current_user(self, db: sqlite3.Connection, role: str | None = None) -> sqlite3.Row:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise ApiError(401, "UNAUTHORIZED", "需要登录")
        token_hash = hashlib.sha256(header[7:].strip().encode()).hexdigest()
        user = db.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.expires_at>?""",
            (token_hash, now_ms()),
        ).fetchone()
        if not user:
            raise ApiError(401, "SESSION_EXPIRED", "登录状态已过期")
        if user["account_status"] != "ACTIVE":
            raise ApiError(403, "ACCOUNT_UNAVAILABLE", "账号当前不可用，请联系管理员")
        if role and user["role"] != role:
            raise ApiError(403, "FORBIDDEN", "权限不足")
        return user

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_PATCH(self) -> None:
        self.dispatch("PATCH")

    def do_PUT(self) -> None:
        self.dispatch("PUT")

    def do_DELETE(self) -> None:
        self.dispatch("DELETE")

    def dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if method == "GET" and (path == "/admin" or path.startswith("/admin/")):
                self.send_admin_asset(path)
                return
            if method == "GET" and path.startswith("/uploads/"):
                self.send_upload(path.removeprefix("/uploads/"))
                return
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            result, status = self.route(method, path, query)
            self.send_json(status, {"ok": True, "data": result})
        except ApiError as error:
            self.send_json(error.status, {"ok": False, "error": {"code": error.code, "message": error.message}})
        except Exception:
            self.send_json(500, {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "服务器内部错误"}})

    def send_admin_asset(self, path: str) -> None:
        relative = "index.html" if path in {"/admin", "/admin/"} else path.removeprefix("/admin/")
        if relative not in {"index.html", "app.js", "styles.css"}:
            raise ApiError(404, "NOT_FOUND", "管理后台资源不存在")
        target = (ADMIN_WEB / relative).resolve()
        if target.parent != ADMIN_WEB.resolve() or not target.is_file():
            raise ApiError(404, "NOT_FOUND", "管理后台资源不存在")
        content = target.read_bytes()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }[target.suffix]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(content)

    def send_upload(self, name: str) -> None:
        if not re.fullmatch(r"[0-9a-f-]+\.(jpg|png|webp)", name):
            raise ApiError(404, "NOT_FOUND", "图片不存在")
        target = (UPLOADS / name).resolve()
        if target.parent != UPLOADS.resolve() or not target.is_file():
            raise ApiError(404, "NOT_FOUND", "图片不存在")
        content = target.read_bytes()
        media_type = {
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }[target.suffix]
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def route(self, method: str, path: str, query: dict[str, str]) -> tuple[Any, int]:
        if method == "GET" and path == "/health":
            return {"status": "ok", "time": now_ms()}, 200
        if method == "POST" and path == "/api/v1/auth/device":
            return self.auth_device(), 200
        if method == "POST" and path == "/api/v1/auth/register":
            return self.auth_register(), 201
        if method == "POST" and path == "/api/v1/auth/login":
            return self.auth_login(), 200
        if method == "POST" and path == "/api/v1/auth/logout":
            return self.auth_logout(), 200
        if method == "GET" and path == "/api/v1/auth/me":
            return self.auth_me(), 200
        if method == "PATCH" and path == "/api/v1/auth/profile":
            return self.auth_profile(), 200
        if method == "DELETE" and path == "/api/v1/auth/account":
            return self.auth_delete_account(), 200
        if path.startswith("/api/v1/sports/"):
            with connection() as db:
                ensure_feature_open(db, "sports_reservation", "体育预约")
        if path.startswith("/api/v1/lost-found"):
            with connection() as db:
                ensure_feature_open(db, "lost_found", "失物招领")
        if method == "GET" and path == "/api/v1/sports/venues":
            return self.sports_venues(), 200
        if method == "GET" and path == "/api/v1/sports/availability":
            return self.sports_availability(query), 200
        if method == "GET" and path == "/api/v1/sports/reservations":
            return self.sports_reservations(), 200
        if method == "POST" and path == "/api/v1/sports/reservations":
            return self.sports_reserve(), 201
        match = re.fullmatch(r"/api/v1/sports/reservations/([^/]+)/cancel", path)
        if method == "POST" and match:
            return self.sports_cancel(match.group(1)), 200
        if method == "GET" and path == "/api/v1/lost-found":
            return self.lost_list(query), 200
        if method == "POST" and path == "/api/v1/lost-found":
            return self.lost_create(), 201
        if method == "GET" and path == "/api/v1/lost-found/favorites":
            return self.lost_favorites(), 200
        match = re.fullmatch(r"/api/v1/lost-found/([^/]+)", path)
        if match and method == "GET":
            return self.lost_get(match.group(1)), 200
        if match and method == "PATCH":
            return self.lost_update(match.group(1)), 200
        if match and method == "DELETE":
            return self.lost_delete(match.group(1)), 200
        match = re.fullmatch(r"/api/v1/lost-found/([^/]+)/favorite", path)
        if match and method == "POST":
            return self.lost_favorite(match.group(1)), 200
        match = re.fullmatch(r"/api/v1/lost-found/([^/]+)/claim", path)
        if match and method == "POST":
            return self.lost_claim(match.group(1)), 200
        match = re.fullmatch(r"/api/v1/lost-found/([^/]+)/report", path)
        if match and method == "POST":
            return self.lost_report(match.group(1)), 201
        if method == "GET" and path == "/api/v1/public/bootstrap":
            return self.public_bootstrap(), 200
        if method == "POST" and path == "/api/v1/feedback":
            return self.feedback_create(), 201
        if path.startswith("/api/v1/admin/"):
            return self.admin_route(method, path, query)
        raise ApiError(404, "NOT_FOUND", "接口不存在")

    def auth_device(self) -> dict[str, Any]:
        data = self.body_json()
        install_id = required_text(data, "installationId", 100)
        username = device_username(install_id)
        with connection() as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not user:
                digest, salt = password_record(secrets.token_urlsafe(32))
                user_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO users(
                           id,username,password_hash,password_salt,role,account_type,
                           nickname,account_status,created_at
                       ) VALUES(?,?,?,?,'USER','DEVICE','测试设备','ACTIVE',?)""",
                    (user_id, username, digest, salt, now_ms()),
                )
                user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return {"token": issue_token(db, user["id"]), "user": serialize_user(user)}

    def auth_register(self) -> dict[str, Any]:
        data = self.body_json()
        phone = normalize_phone(data.get("phone"))
        password = required_text(data, "password", 200)
        if len(password) < 8:
            raise ApiError(400, "WEAK_PASSWORD", "密码至少 8 位")
        nickname = str(data.get("nickname", "长理同学")).strip()[:30] or "长理同学"
        digest_phone = phone_digest(phone)
        username = "phone_" + digest_phone[:32]
        digest, salt = password_record(password)
        with connection() as db:
            user_id = str(uuid.uuid4())
            try:
                db.execute(
                    """INSERT INTO users(
                           id,username,password_hash,password_salt,role,account_type,
                           nickname,phone_hash,phone_masked,account_status,created_at
                       ) VALUES(?,?,?,?,'USER','PHONE',?,?,?,'ACTIVE',?)""",
                    (
                        user_id,
                        username,
                        digest,
                        salt,
                        nickname,
                        digest_phone,
                        phone_mask(phone),
                        now_ms(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "PHONE_EXISTS", "该手机号已经注册") from exc
            migrated = migrate_device_data(
                db, str(data.get("installationId", "")).strip() or None, user_id
            )
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            write_audit(
                db,
                user_id,
                "USER_REGISTER",
                "USER",
                user_id,
                {"migratedDeviceData": migrated},
            )
            return {"token": issue_token(db, user_id), "user": serialize_user(user)}

    def auth_login(self) -> dict[str, Any]:
        data = self.body_json()
        password = required_text(data, "password", 200)
        with connection() as db:
            if str(data.get("phone", "")).strip():
                phone = normalize_phone(data.get("phone"))
                user = db.execute(
                    "SELECT * FROM users WHERE phone_hash=?", (phone_digest(phone),)
                ).fetchone()
            else:
                username = required_text(data, "username", 48)
                user = db.execute(
                    "SELECT * FROM users WHERE username=?", (username,)
                ).fetchone()
            if not user or not verify_password(password, user["password_hash"], user["password_salt"]):
                raise ApiError(401, "INVALID_CREDENTIALS", "账号或密码错误")
            if user["account_status"] != "ACTIVE":
                raise ApiError(403, "ACCOUNT_UNAVAILABLE", "账号当前不可用")
            migrated = migrate_device_data(
                db,
                str(data.get("installationId", "")).strip() or None,
                user["id"],
            )
            user = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            return {
                "token": issue_token(db, user["id"]),
                "user": serialize_user(user),
                "migratedDeviceData": migrated,
            }

    def auth_logout(self) -> dict[str, Any]:
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            token_hash = hashlib.sha256(header[7:].strip().encode()).hexdigest()
            with connection() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        return {"loggedOut": True}

    def auth_me(self) -> dict[str, Any]:
        with connection() as db:
            return serialize_user(self.current_user(db))

    def auth_profile(self) -> dict[str, Any]:
        data = self.body_json()
        with connection() as db:
            user = self.current_user(db)
            nickname = str(data.get("nickname", user["nickname"])).strip()[:30]
            if not nickname:
                raise ApiError(400, "VALIDATION_ERROR", "昵称不能为空")
            db.execute(
                "UPDATE users SET nickname=? WHERE id=?", (nickname, user["id"])
            )
            return serialize_user(
                db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            )

    def auth_delete_account(self) -> dict[str, Any]:
        data = self.body_json()
        password = required_text(data, "password", 200)
        with connection() as db:
            user = self.current_user(db)
            if user["role"] == "ADMIN" or user["account_type"] != "PHONE":
                raise ApiError(400, "ACCOUNT_DELETE_UNAVAILABLE", "该账号不能在 App 内注销")
            if not verify_password(password, user["password_hash"], user["password_salt"]):
                raise ApiError(401, "INVALID_CREDENTIALS", "密码错误")
            timestamp = now_ms()
            active_ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM reservations WHERE user_id=? AND status='ACTIVE'",
                    (user["id"],),
                )
            ]
            db.execute(
                """UPDATE reservations SET status='CANCELLED',cancelled_at=?,
                   cancelled_by='ACCOUNT_DELETION',cancel_reason='账号注销',
                   updated_at=? WHERE user_id=? AND status='ACTIVE'""",
                (timestamp, timestamp, user["id"]),
            )
            for reservation_id in active_ids:
                write_reservation_event(
                    db,
                    reservation_id,
                    user["id"],
                    user["id"],
                    "ACCOUNT_DELETION_CANCEL",
                    "账号注销",
                )
            random_digest, random_salt = password_record(secrets.token_urlsafe(40))
            db.execute(
                """UPDATE users SET account_status='DELETED',nickname='已注销用户',
                   phone_hash=NULL,phone_masked=NULL,student_id=NULL,real_name=NULL,
                   campus_verified=0,campus_verified_at=NULL,
                   password_hash=?,password_salt=? WHERE id=?""",
                (random_digest, random_salt, user["id"]),
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            write_audit(
                db, user["id"], "USER_ACCOUNT_DELETED", "USER", user["id"]
            )
            return {"deleted": True}

    def sports_venues(self) -> list[dict[str, Any]]:
        with connection() as db:
            self.current_user(db)
            rows = db.execute(
                "SELECT * FROM venues WHERE enabled=1 AND operating_status='OPEN' ORDER BY sport"
            ).fetchall()
            result = []
            for venue in rows:
                slots = db.execute(
                    """SELECT start_time || '-' || end_time AS value FROM venue_time_slots
                       WHERE venue_id=? AND enabled=1 ORDER BY start_time""",
                    (venue["id"],),
                ).fetchall()
                result.append({
                    "id": venue["id"], "sport": venue["sport"], "name": venue["name"],
                    "unitLabel": venue["unit_label"], "courtCount": venue["court_count"],
                    "priceYuan": venue["price_yuan"], "timeSlots": [row["value"] for row in slots],
                })
            return result

    def sports_availability(self, query: dict[str, str]) -> dict[str, Any]:
        venue_id = required_text(query, "venueId", 80)
        booking_date = parse_iso_date(required_text(query, "date", 10))
        with connection() as db:
            self.current_user(db)
            venue = db.execute(
                "SELECT * FROM venues WHERE id=? AND enabled=1 AND operating_status='OPEN'",
                (venue_id,),
            ).fetchone()
            if not venue:
                raise ApiError(404, "VENUE_NOT_FOUND", "场馆不存在")
            slots = db.execute(
                """SELECT start_time || '-' || end_time AS value FROM venue_time_slots
                   WHERE venue_id=? AND enabled=1 ORDER BY start_time""",
                (venue_id,),
            ).fetchall()
            occupied = {
                (row["time_slot"], row["court_number"]): "RESERVED"
                for row in db.execute(
                    """SELECT time_slot,court_number FROM reservations
                       WHERE venue_id=? AND booking_date=? AND status='ACTIVE'""",
                    (venue_id, booking_date),
                )
            }
            blocked = {
                (row["time_slot"], row["court_number"]): "UNAVAILABLE"
                for row in db.execute(
                    """SELECT time_slot,court_number FROM blocked_inventory
                       WHERE venue_id=? AND booking_date=?""",
                    (venue_id, booking_date),
                )
            }
            return {
                "venueId": venue_id,
                "date": booking_date,
                "slots": [
                    {
                        "timeSlot": slot["value"],
                        "courts": [
                            {"courtNumber": number, "status": blocked.get((slot["value"], number), occupied.get((slot["value"], number), "AVAILABLE"))}
                            for number in range(1, venue["court_count"] + 1)
                        ],
                    }
                    for slot in slots
                ],
            }

    def sports_reserve(self) -> dict[str, Any]:
        data = self.body_json()
        venue_id = required_text(data, "venueId", 80)
        booking_date = parse_iso_date(required_text(data, "date", 10))
        time_slot = required_text(data, "timeSlot", 20)
        court = int(data.get("courtNumber", 0))
        today = date.today()
        selected = date.fromisoformat(booking_date)
        if selected < today or selected > today + timedelta(days=6):
            raise ApiError(400, "INVALID_DATE", "仅支持今天起未来 7 天")
        with connection() as db:
            user = self.current_user(db)
            reservation_id = str(uuid.uuid4())
            timestamp = now_ms()
            try:
                db.execute("BEGIN IMMEDIATE")
                rules = reservation_rules(db)
                restriction = active_restriction(db, user["id"])
                if (
                    restriction
                    and restriction["reservation_blocked_until"]
                    and restriction["reservation_blocked_until"] > timestamp
                ):
                    raise ApiError(
                        429,
                        "RESERVATION_RESTRICTED",
                        restriction["reason"] or "当前账号暂时无法继续预约",
                    )
                active_count = db.execute(
                    """SELECT COUNT(*) AS count FROM reservations
                       WHERE user_id=? AND booking_date=? AND status='ACTIVE'""",
                    (user["id"], booking_date),
                ).fetchone()["count"]
                if active_count >= rules["max_active_per_day"]:
                    raise ApiError(
                        429,
                        "DAILY_RESERVATION_LIMIT",
                        f"每天最多保留 {rules['max_active_per_day']} 条有效预约",
                    )
                venue = db.execute(
                    """SELECT * FROM venues WHERE id=? AND enabled=1
                       AND operating_status='OPEN'""",
                    (venue_id,),
                ).fetchone()
                if not venue or court not in range(1, venue["court_count"] + 1):
                    raise ApiError(400, "INVALID_INVENTORY", "场地选择无效或场馆维护中")
                slot_exists = db.execute(
                    """SELECT 1 FROM venue_time_slots WHERE venue_id=? AND enabled=1
                       AND start_time || '-' || end_time=?""",
                    (venue_id, time_slot),
                ).fetchone()
                if not slot_exists:
                    raise ApiError(400, "INVALID_TIME_SLOT", "时间段不可预约")
                blocked = db.execute(
                    """SELECT 1 FROM blocked_inventory WHERE venue_id=? AND booking_date=?
                       AND time_slot=? AND court_number=?""",
                    (venue_id, booking_date, time_slot, court),
                ).fetchone()
                if blocked:
                    raise ApiError(409, "INVENTORY_UNAVAILABLE", "该场地当前不可预约")
                db.execute(
                    """INSERT INTO reservations(id,user_id,venue_id,booking_date,time_slot,
                       court_number,price_yuan,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (reservation_id, user["id"], venue_id, booking_date, time_slot, court,
                     venue["price_yuan"], timestamp, timestamp),
                )
                write_reservation_event(
                    db,
                    reservation_id,
                    user["id"],
                    user["id"],
                    "CREATED",
                    details={
                        "bookingDate": booking_date,
                        "timeSlot": time_slot,
                        "courtNumber": court,
                    },
                )
                db.execute("COMMIT")
            except ApiError as exc:
                write_reservation_event(
                    db,
                    None,
                    user["id"],
                    user["id"],
                    "RESERVATION_REJECTED",
                    exc.message,
                    {"code": exc.code, "venueId": venue_id, "date": booking_date},
                )
                db.execute("COMMIT")
                raise
            except sqlite3.IntegrityError as exc:
                db.execute("ROLLBACK")
                with connection() as event_db:
                    write_reservation_event(
                        event_db,
                        None,
                        user["id"],
                        user["id"],
                        "RESERVATION_REJECTED",
                        "库存刚刚被其他用户预约",
                        {"code": "INVENTORY_ALREADY_RESERVED"},
                    )
                raise ApiError(409, "INVENTORY_ALREADY_RESERVED", "该场地刚刚已被预约，请重新选择") from exc
            return self.reservation_by_id(db, reservation_id)

    def reservation_by_id(self, db: sqlite3.Connection, reservation_id: str) -> dict[str, Any]:
        row = db.execute(
            """SELECT r.*,v.sport,v.name,v.unit_label FROM reservations r
               JOIN venues v ON v.id=r.venue_id WHERE r.id=?""",
            (reservation_id,),
        ).fetchone()
        if not row:
            raise ApiError(404, "RESERVATION_NOT_FOUND", "预约不存在")
        return {
            "id": row["id"], "sport": row["sport"], "venueId": row["venue_id"],
            "venueName": row["name"], "unitLabel": row["unit_label"],
            "date": row["booking_date"], "timeSlot": row["time_slot"],
            "courtNumber": row["court_number"], "priceYuan": row["price_yuan"],
            "status": row["status"], "paymentStatus": row["payment_status"],
            "refundStatus": row["refund_status"],
            "verificationStatus": row["verification_status"],
            "cancelledAtMillis": row["cancelled_at"],
            "cancelledBy": row["cancelled_by"],
            "cancelReason": row["cancel_reason"],
            "createdAtMillis": row["created_at"],
        }

    def sports_reservations(self) -> list[dict[str, Any]]:
        with connection() as db:
            user = self.current_user(db)
            ids = db.execute(
                "SELECT id FROM reservations WHERE user_id=? ORDER BY created_at DESC",
                (user["id"],),
            ).fetchall()
            return [self.reservation_by_id(db, row["id"]) for row in ids]

    def sports_cancel(self, reservation_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            timestamp = now_ms()
            db.execute("BEGIN IMMEDIATE")
            reservation = db.execute(
                """SELECT * FROM reservations
                   WHERE id=? AND user_id=? AND status='ACTIVE'""",
                (reservation_id, user["id"]),
            ).fetchone()
            if not reservation:
                db.execute("ROLLBACK")
                raise ApiError(409, "CANNOT_CANCEL", "预约不存在或已取消")
            rules = reservation_rules(db)
            restriction = active_restriction(db, user["id"])
            if (
                restriction
                and restriction["cancellation_blocked_until"]
                and restriction["cancellation_blocked_until"] > timestamp
            ):
                write_reservation_event(
                    db,
                    reservation_id,
                    user["id"],
                    user["id"],
                    "CANCEL_REJECTED",
                    restriction["reason"] or "当前账号暂时无法继续取消预约",
                    {"code": "CANCELLATION_RESTRICTED"},
                )
                db.execute("COMMIT")
                raise ApiError(
                    429,
                    "CANCELLATION_RESTRICTED",
                    restriction["reason"] or "当前账号暂时无法继续取消预约",
                )
            start_text = reservation["time_slot"].split("-", 1)[0]
            start_at = datetime.strptime(
                f"{reservation['booking_date']} {start_text}", "%Y-%m-%d %H:%M"
            )
            if datetime.now() + timedelta(minutes=rules["cancel_cutoff_minutes"]) >= start_at:
                message = f"开场前 {rules['cancel_cutoff_minutes']} 分钟内不可自行取消"
                write_reservation_event(
                    db,
                    reservation_id,
                    user["id"],
                    user["id"],
                    "CANCEL_REJECTED",
                    message,
                    {"code": "CANCEL_CUTOFF"},
                )
                db.execute("COMMIT")
                raise ApiError(409, "CANCEL_CUTOFF", message)
            day_start = int(
                datetime.combine(date.today(), datetime.min.time()).timestamp() * 1000
            )
            cancellation_count = db.execute(
                """SELECT COUNT(*) AS count FROM reservation_events
                   WHERE user_id=? AND action='USER_CANCELLED' AND created_at>=?""",
                (user["id"], day_start),
            ).fetchone()["count"]
            if cancellation_count >= rules["max_user_cancellations_per_day"]:
                blocked_until = timestamp + (
                    rules["restriction_hours_after_excess_cancel"] * 60 * 60 * 1000
                )
                reason = (
                    f"今日主动取消已达 {rules['max_user_cancellations_per_day']} 次，"
                    f"请在限制结束后再试"
                )
                upsert_restriction(
                    db, user["id"], blocked_until, blocked_until, reason
                )
                write_reservation_event(
                    db,
                    reservation_id,
                    user["id"],
                    user["id"],
                    "CANCEL_REJECTED",
                    reason,
                    {"code": "DAILY_CANCELLATION_LIMIT"},
                )
                db.execute("COMMIT")
                raise ApiError(429, "DAILY_CANCELLATION_LIMIT", reason)
            db.execute(
                """UPDATE reservations SET status='CANCELLED',cancelled_at=?,
                   cancelled_by='USER',cancel_reason='用户主动取消',updated_at=?
                   WHERE id=?""",
                (timestamp, timestamp, reservation_id),
            )
            write_reservation_event(
                db,
                reservation_id,
                user["id"],
                user["id"],
                "USER_CANCELLED",
                "用户主动取消",
            )
            if cancellation_count + 1 >= rules["max_user_cancellations_per_day"]:
                blocked_until = timestamp + (
                    rules["restriction_hours_after_excess_cancel"] * 60 * 60 * 1000
                )
                reason = (
                    f"今日主动取消已达 {rules['max_user_cancellations_per_day']} 次，"
                    f"已暂时限制继续预约和取消"
                )
                upsert_restriction(
                    db, user["id"], blocked_until, blocked_until, reason
                )
            db.execute("COMMIT")
            return self.reservation_by_id(db, reservation_id)

    def serialize_lost(self, db: sqlite3.Connection, row: sqlite3.Row, user_id: str) -> dict[str, Any]:
        favorite = db.execute(
            "SELECT 1 FROM lost_found_favorites WHERE user_id=? AND post_id=?",
            (user_id, row["id"]),
        ).fetchone() is not None
        claimed = db.execute(
            "SELECT 1 FROM lost_found_claims WHERE user_id=? AND post_id=?",
            (user_id, row["id"]),
        ).fetchone() is not None
        return {
            "id": row["id"], "clientLocalId": row["client_local_id"], "type": row["type"],
            "itemName": row["item_name"], "category": row["category"],
            "location": row["location"], "eventDate": row["event_date"],
            "approximateTime": row["approximate_time"], "description": row["description"],
            "claimNote": row["claim_note"], "imageUrl": f"/uploads/{row['image_name']}" if row["image_name"] else None,
            "status": row["status"], "createdAtMillis": row["created_at"],
            "updatedAtMillis": row["updated_at"], "isMine": row["owner_user_id"] == user_id,
            "isFavorite": favorite, "hasClaimRequest": claimed,
        }

    def lost_list(self, query: dict[str, str]) -> list[dict[str, Any]]:
        with connection() as db:
            user = self.current_user(db)
            clauses, values = [], []
            lost_type = query.get("type", "").upper()
            if lost_type in LOST_TYPES:
                clauses.append("type=?")
                values.append(lost_type)
            if query.get("mine") == "true":
                clauses.append("owner_user_id=?")
                values.append(user["id"])
            search = query.get("q", "").strip()
            if search:
                clauses.append("(item_name LIKE ? OR description LIKE ? OR location LIKE ? OR category LIKE ?)")
                token = f"%{search[:80]}%"
                values.extend([token] * 4)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = db.execute(
                "SELECT * FROM lost_found_posts" + where + " ORDER BY updated_at DESC LIMIT 200",
                values,
            ).fetchall()
            return [self.serialize_lost(db, row, user["id"]) for row in rows]

    def lost_get(self, post_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            row = db.execute("SELECT * FROM lost_found_posts WHERE id=?", (post_id,)).fetchone()
            if not row:
                raise ApiError(404, "POST_NOT_FOUND", "失物信息不存在")
            return self.serialize_lost(db, row, user["id"])

    def validated_lost(self, data: dict[str, Any], existing: sqlite3.Row | None = None) -> dict[str, Any]:
        lost_type = str(data.get("type", existing["type"] if existing else "")).upper()
        if lost_type not in LOST_TYPES:
            raise ApiError(400, "VALIDATION_ERROR", "type 必须为 LOST 或 FOUND")
        status = str(data.get("status", existing["status"] if existing else ("SEARCHING" if lost_type == "LOST" else "WAITING"))).upper()
        if status not in LOST_STATUSES[lost_type]:
            raise ApiError(400, "VALIDATION_ERROR", "状态与失物类型不匹配")
        return {
            "type": lost_type,
            "item_name": required_text(data, "itemName", 80) if "itemName" in data or not existing else existing["item_name"],
            "category": required_text(data, "category", 40) if "category" in data or not existing else existing["category"],
            "location": required_text(data, "location", 120) if "location" in data or not existing else existing["location"],
            "event_date": parse_iso_date(str(data.get("eventDate", existing["event_date"] if existing else "")), future_allowed=False),
            "approximate_time": str(data.get("approximateTime", existing["approximate_time"] if existing else "") or "")[:40] or None,
            "description": str(data.get("description", existing["description"] if existing else "") or "")[:1000],
            "claim_note": str(data.get("claimNote", existing["claim_note"] if existing else "") or "")[:500],
            "status": status,
        }

    def save_image(self, encoded: Any, post_id: str) -> str | None:
        if not encoded:
            return None
        try:
            raw = base64.b64decode(str(encoded), validate=True)
        except Exception as exc:
            raise ApiError(400, "INVALID_IMAGE", "图片数据无效") from exc
        if len(raw) > 3 * 1024 * 1024:
            raise ApiError(413, "IMAGE_TOO_LARGE", "图片不能超过 3MB")
        if raw.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            raise ApiError(400, "INVALID_IMAGE", "仅支持 JPEG、PNG 或 WebP")
        name = post_id + suffix
        (UPLOADS / name).write_bytes(raw)
        return name

    def lost_create(self) -> dict[str, Any]:
        data = self.body_json()
        values = self.validated_lost(data)
        post_id = str(uuid.uuid4())
        image_name = self.save_image(data.get("imageBase64"), post_id)
        timestamp = now_ms()
        with connection() as db:
            user = self.current_user(db)
            try:
                db.execute(
                    """INSERT INTO lost_found_posts(id,owner_user_id,client_local_id,type,item_name,
                       category,location,event_date,approximate_time,description,claim_note,image_name,
                       status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (post_id, user["id"], str(data.get("clientLocalId") or "")[:100] or None,
                     values["type"], values["item_name"], values["category"], values["location"],
                     values["event_date"], values["approximate_time"], values["description"],
                     values["claim_note"], image_name, values["status"], timestamp, timestamp),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT * FROM lost_found_posts WHERE owner_user_id=? AND client_local_id=?",
                    (user["id"], data.get("clientLocalId")),
                ).fetchone()
                if existing:
                    return self.serialize_lost(db, existing, user["id"])
                raise
            row = db.execute("SELECT * FROM lost_found_posts WHERE id=?", (post_id,)).fetchone()
            return self.serialize_lost(db, row, user["id"])

    def lost_update(self, post_id: str) -> dict[str, Any]:
        data = self.body_json()
        with connection() as db:
            user = self.current_user(db)
            existing = db.execute("SELECT * FROM lost_found_posts WHERE id=?", (post_id,)).fetchone()
            if not existing:
                raise ApiError(404, "POST_NOT_FOUND", "失物信息不存在")
            if existing["owner_user_id"] != user["id"] and user["role"] != "ADMIN":
                raise ApiError(403, "FORBIDDEN", "只能修改自己的发布")
            values = self.validated_lost(data, existing)
            image_name = existing["image_name"]
            if "imageBase64" in data:
                image_name = self.save_image(data.get("imageBase64"), post_id)
            db.execute(
                """UPDATE lost_found_posts SET type=?,item_name=?,category=?,location=?,event_date=?,
                   approximate_time=?,description=?,claim_note=?,image_name=?,status=?,updated_at=?
                   WHERE id=?""",
                (values["type"], values["item_name"], values["category"], values["location"],
                 values["event_date"], values["approximate_time"], values["description"],
                 values["claim_note"], image_name, values["status"], now_ms(), post_id),
            )
            updated = db.execute("SELECT * FROM lost_found_posts WHERE id=?", (post_id,)).fetchone()
            return self.serialize_lost(db, updated, user["id"])

    def lost_delete(self, post_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            post = db.execute("SELECT * FROM lost_found_posts WHERE id=?", (post_id,)).fetchone()
            if not post:
                raise ApiError(404, "POST_NOT_FOUND", "失物信息不存在")
            if post["owner_user_id"] != user["id"] and user["role"] != "ADMIN":
                raise ApiError(403, "FORBIDDEN", "只能删除自己的发布")
            db.execute("DELETE FROM lost_found_posts WHERE id=?", (post_id,))
            if post["image_name"]:
                run_safely_delete_upload(post["image_name"])
            return {"deleted": True, "id": post_id}

    def lost_favorite(self, post_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            if not db.execute("SELECT 1 FROM lost_found_posts WHERE id=?", (post_id,)).fetchone():
                raise ApiError(404, "POST_NOT_FOUND", "失物信息不存在")
            existing = db.execute(
                "SELECT 1 FROM lost_found_favorites WHERE user_id=? AND post_id=?",
                (user["id"], post_id),
            ).fetchone()
            if existing:
                db.execute("DELETE FROM lost_found_favorites WHERE user_id=? AND post_id=?", (user["id"], post_id))
            else:
                db.execute("INSERT INTO lost_found_favorites VALUES(?,?,?)", (user["id"], post_id, now_ms()))
            return {"postId": post_id, "isFavorite": not bool(existing)}

    def lost_favorites(self) -> list[dict[str, Any]]:
        with connection() as db:
            user = self.current_user(db)
            rows = db.execute(
                """SELECT p.* FROM lost_found_favorites f JOIN lost_found_posts p ON p.id=f.post_id
                   WHERE f.user_id=? ORDER BY f.created_at DESC""",
                (user["id"],),
            ).fetchall()
            return [self.serialize_lost(db, row, user["id"]) for row in rows]

    def lost_claim(self, post_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            post = db.execute("SELECT * FROM lost_found_posts WHERE id=?", (post_id,)).fetchone()
            if not post or post["type"] != "FOUND" or post["status"] == "RETURNED":
                raise ApiError(409, "CLAIM_UNAVAILABLE", "当前不能申请认领")
            if post["owner_user_id"] == user["id"]:
                raise ApiError(409, "CLAIM_OWN_POST", "不能认领自己发布的信息")
            db.execute(
                "INSERT OR IGNORE INTO lost_found_claims VALUES(?,?,?)",
                (user["id"], post_id, now_ms()),
            )
            return {"postId": post_id, "submitted": True, "message": "认领申请已记录；本阶段不提供站内联系"}

    def lost_report(self, post_id: str) -> dict[str, Any]:
        data = self.body_json()
        with connection() as db:
            user = self.current_user(db)
            if not db.execute("SELECT 1 FROM lost_found_posts WHERE id=?", (post_id,)).fetchone():
                raise ApiError(404, "POST_NOT_FOUND", "失物信息不存在")
            report_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO lost_found_reports(id,user_id,post_id,reason,created_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(user_id,post_id)
                   DO UPDATE SET reason=excluded.reason,status='OPEN',created_at=excluded.created_at""",
                (report_id, user["id"], post_id, required_text(data, "reason", 200), now_ms()),
            )
            return {"postId": post_id, "recorded": True}

    def public_bootstrap(self) -> dict[str, Any]:
        with connection() as db:
            flags = [
                {"key": row["key"], "enabled": bool(row["enabled"]), "payload": json.loads(row["payload_json"])}
                for row in db.execute("SELECT * FROM remote_flags ORDER BY key")
            ]
            announcements = [
                row_dict(row) for row in db.execute(
                    "SELECT id,title,content,published_at AS publishedAt FROM announcements WHERE enabled=1 ORDER BY published_at DESC LIMIT 10"
                )
            ]
            version = row_dict(db.execute("SELECT * FROM app_versions WHERE platform='android'").fetchone())
            return {"flags": flags, "announcements": announcements, "version": version}

    def feedback_create(self) -> dict[str, Any]:
        data = self.body_json()
        with connection() as db:
            user = self.current_user(db)
            feedback_id = str(uuid.uuid4())
            timestamp = now_ms()
            db.execute(
                """INSERT INTO feedback(id,user_id,category,content,app_version,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (feedback_id, user["id"], required_text(data, "category", 40),
                 required_text(data, "content", 2000), str(data.get("appVersion", ""))[:40],
                 timestamp, timestamp),
            )
            return {"id": feedback_id, "status": "OPEN"}

    def admin_route(
        self, method: str, path: str, query: dict[str, str]
    ) -> tuple[Any, int]:
        with connection() as db:
            admin = self.current_user(db, "ADMIN")

            if method == "GET" and path == "/api/v1/admin/users":
                clauses = ["u.role='USER'"]
                values: list[Any] = []
                if query.get("q"):
                    token = f"%{query['q'][:80]}%"
                    clauses.append(
                        """(u.nickname LIKE ? OR u.phone_masked LIKE ? OR
                            u.student_id LIKE ? OR u.real_name LIKE ? OR u.id LIKE ?)"""
                    )
                    values.extend([token, token, token, token, token])
                if query.get("status", "").upper() in {"ACTIVE", "SUSPENDED", "DELETED"}:
                    clauses.append("u.account_status=?")
                    values.append(query["status"].upper())
                if query.get("accountType", "").upper() in {"PHONE", "DEVICE"}:
                    clauses.append("u.account_type=?")
                    values.append(query["accountType"].upper())
                return [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT u.id,u.account_type,u.nickname,u.phone_masked,u.student_id,
                                  u.real_name,u.campus_verified,u.campus_verified_at,
                                  u.account_status,u.created_at,
                                  (SELECT COUNT(*) FROM reservations r
                                   WHERE r.user_id=u.id) AS reservation_count,
                                  (SELECT COUNT(*) FROM reservation_events e
                                   WHERE e.user_id=u.id AND e.action='USER_CANCELLED') AS cancellation_count,
                                  ur.reservation_blocked_until,ur.cancellation_blocked_until,
                                  ur.reason AS restriction_reason
                           FROM users u
                           LEFT JOIN user_restrictions ur ON ur.user_id=u.id
                           WHERE """
                        + " AND ".join(clauses)
                        + " ORDER BY u.created_at DESC LIMIT 500",
                        values,
                    )
                ], 200

            match = re.fullmatch(r"/api/v1/admin/users/([^/]+)", path)
            if method == "PATCH" and match:
                data = self.body_json()
                user = db.execute(
                    "SELECT * FROM users WHERE id=? AND role='USER'", (match.group(1),)
                ).fetchone()
                if not user:
                    raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
                account_status = str(
                    data.get("accountStatus", user["account_status"])
                ).upper()
                if account_status not in {"ACTIVE", "SUSPENDED"}:
                    raise ApiError(400, "VALIDATION_ERROR", "账号状态无效")
                nickname = str(data.get("nickname", user["nickname"] or "长理同学")).strip()[:40]
                student_id = str(data.get("studentId", user["student_id"] or "")).strip()[:32] or None
                real_name = str(data.get("realName", user["real_name"] or "")).strip()[:40] or None
                verified = 1 if bool(data.get("campusVerified", user["campus_verified"])) else 0
                verified_at = user["campus_verified_at"]
                if verified and not verified_at:
                    verified_at = now_ms()
                if not verified:
                    verified_at = None
                db.execute(
                    """UPDATE users SET account_status=?,nickname=?,student_id=?,real_name=?,
                       campus_verified=?,campus_verified_at=? WHERE id=?""",
                    (
                        account_status,
                        nickname or "长理同学",
                        student_id,
                        real_name,
                        verified,
                        verified_at,
                        user["id"],
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "USER_UPDATED",
                    "USER",
                    user["id"],
                    {
                        "accountStatus": account_status,
                        "campusVerified": bool(verified),
                    },
                )
                return serialize_user(
                    db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
                ), 200

            match = re.fullmatch(r"/api/v1/admin/users/([^/]+)/clear-restrictions", path)
            if method == "POST" and match:
                user = db.execute(
                    "SELECT id FROM users WHERE id=? AND role='USER'", (match.group(1),)
                ).fetchone()
                if not user:
                    raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
                db.execute("DELETE FROM user_restrictions WHERE user_id=?", (user["id"],))
                write_audit(
                    db, admin["id"], "USER_RESTRICTION_CLEARED", "USER", user["id"]
                )
                return {"id": user["id"], "restricted": False}, 200

            if method == "GET" and path == "/api/v1/admin/reservation-rules":
                return row_dict(reservation_rules(db)), 200

            if method == "PUT" and path == "/api/v1/admin/reservation-rules":
                data = self.body_json()
                values = {
                    "max_active_per_day": int(data.get("maxActivePerDay", 0)),
                    "max_user_cancellations_per_day": int(
                        data.get("maxUserCancellationsPerDay", 0)
                    ),
                    "cancel_cutoff_minutes": int(data.get("cancelCutoffMinutes", -1)),
                    "restriction_hours_after_excess_cancel": int(
                        data.get("restrictionHoursAfterExcessCancel", 0)
                    ),
                }
                if (
                    values["max_active_per_day"] not in range(1, 21)
                    or values["max_user_cancellations_per_day"] not in range(1, 21)
                    or values["cancel_cutoff_minutes"] not in range(0, 1441)
                    or values["restriction_hours_after_excess_cancel"] not in range(1, 721)
                ):
                    raise ApiError(400, "VALIDATION_ERROR", "预约规则参数无效")
                db.execute(
                    """UPDATE reservation_rules SET max_active_per_day=?,
                       max_user_cancellations_per_day=?,cancel_cutoff_minutes=?,
                       restriction_hours_after_excess_cancel=?,updated_at=?
                       WHERE id='default'""",
                    (
                        values["max_active_per_day"],
                        values["max_user_cancellations_per_day"],
                        values["cancel_cutoff_minutes"],
                        values["restriction_hours_after_excess_cancel"],
                        now_ms(),
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "RESERVATION_RULES_UPDATED",
                    "RESERVATION_RULES",
                    "default",
                    values,
                )
                return row_dict(reservation_rules(db)), 200

            if method == "GET" and path == "/api/v1/admin/audit-logs":
                return [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT a.*,u.nickname AS actor_name
                           FROM admin_audit_logs a
                           LEFT JOIN users u ON u.id=a.actor_user_id
                           ORDER BY a.created_at DESC LIMIT 300"""
                    )
                ], 200

            if method == "GET" and path == "/api/v1/admin/overview":
                today = date.today().isoformat()
                return {
                    "activeReservations": db.execute(
                        "SELECT COUNT(*) AS count FROM reservations WHERE status='ACTIVE'"
                    ).fetchone()["count"],
                    "todayReservations": db.execute(
                        "SELECT COUNT(*) AS count FROM reservations WHERE booking_date=? AND status='ACTIVE'",
                        (today,),
                    ).fetchone()["count"],
                    "lostFoundOpen": db.execute(
                        """SELECT COUNT(*) AS count FROM lost_found_posts
                           WHERE status IN ('SEARCHING','WAITING','CLAIMED')"""
                    ).fetchone()["count"],
                    "openReports": db.execute(
                        "SELECT COUNT(*) AS count FROM lost_found_reports WHERE status='OPEN'"
                    ).fetchone()["count"],
                    "openFeedback": db.execute(
                        "SELECT COUNT(*) AS count FROM feedback WHERE status!='CLOSED'"
                    ).fetchone()["count"],
                    "enabledAnnouncements": db.execute(
                        "SELECT COUNT(*) AS count FROM announcements WHERE enabled=1"
                    ).fetchone()["count"],
                    "users": db.execute(
                        "SELECT COUNT(*) AS count FROM users WHERE role='USER' AND account_status='ACTIVE'"
                    ).fetchone()["count"],
                }, 200

            if method == "GET" and path == "/api/v1/admin/venues":
                venues = []
                for venue in db.execute("SELECT * FROM venues ORDER BY sport"):
                    item = row_dict(venue)
                    item["enabled"] = bool(item["enabled"])
                    item["timeSlots"] = [
                        {
                            "id": slot["id"],
                            "startTime": slot["start_time"],
                            "endTime": slot["end_time"],
                            "timeSlot": f"{slot['start_time']}-{slot['end_time']}",
                            "enabled": bool(slot["enabled"]),
                        }
                        for slot in db.execute(
                            "SELECT * FROM venue_time_slots WHERE venue_id=? ORDER BY start_time",
                            (venue["id"],),
                        )
                    ]
                    venues.append(item)
                return venues, 200

            if method == "GET" and path == "/api/v1/admin/inventory":
                venue_id = required_text(query, "venueId", 80)
                booking_date = parse_iso_date(required_text(query, "date", 10))
                return [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT * FROM blocked_inventory
                           WHERE venue_id=? AND booking_date=?
                           ORDER BY time_slot,court_number""",
                        (venue_id, booking_date),
                    )
                ], 200

            if method == "POST" and path == "/api/v1/admin/inventory/block":
                data = self.body_json()
                venue_id = required_text(data, "venueId", 80)
                booking_date = parse_iso_date(required_text(data, "date", 10))
                time_slot = required_text(data, "timeSlot", 20)
                court_number = int(data.get("courtNumber", 0))
                venue = db.execute("SELECT * FROM venues WHERE id=?", (venue_id,)).fetchone()
                if not venue or court_number not in range(1, venue["court_count"] + 1):
                    raise ApiError(400, "INVALID_INVENTORY", "场地编号无效")
                slot = db.execute(
                    """SELECT 1 FROM venue_time_slots WHERE venue_id=?
                       AND start_time || '-' || end_time=?""",
                    (venue_id, time_slot),
                ).fetchone()
                if not slot:
                    raise ApiError(400, "INVALID_TIME_SLOT", "时间段无效")
                block_id = str(uuid.uuid4())
                db.execute(
                    """INSERT INTO blocked_inventory(
                           id,venue_id,booking_date,time_slot,court_number,reason
                       ) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(venue_id,booking_date,time_slot,court_number)
                       DO UPDATE SET reason=excluded.reason""",
                    (
                        block_id,
                        venue_id,
                        booking_date,
                        time_slot,
                        court_number,
                        str(data.get("reason", "场馆暂不可用")).strip()[:100]
                        or "场馆暂不可用",
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "INVENTORY_BLOCKED",
                    "INVENTORY",
                    block_id,
                    {
                        "venueId": venue_id,
                        "date": booking_date,
                        "timeSlot": time_slot,
                        "courtNumber": court_number,
                    },
                )
                return {"blocked": True}, 200

            match = re.fullmatch(r"/api/v1/admin/inventory/block/([^/]+)", path)
            if method == "DELETE" and match:
                changed = db.execute(
                    "DELETE FROM blocked_inventory WHERE id=?", (match.group(1),)
                ).rowcount
                if not changed:
                    raise ApiError(404, "BLOCK_NOT_FOUND", "关闭记录不存在")
                write_audit(
                    db, admin["id"], "INVENTORY_UNBLOCKED", "INVENTORY", match.group(1)
                )
                return {"blocked": False}, 200

            match = re.fullmatch(r"/api/v1/admin/venues/([^/]+)", path)
            if method == "PATCH" and match:
                data = self.body_json()
                venue = db.execute(
                    "SELECT * FROM venues WHERE id=?", (match.group(1),)
                ).fetchone()
                if not venue:
                    raise ApiError(404, "VENUE_NOT_FOUND", "场馆不存在")
                name = str(data.get("name", venue["name"])).strip()[:80]
                count = int(data.get("courtCount", venue["court_count"]))
                price = int(data.get("priceYuan", venue["price_yuan"]))
                enabled = 1 if bool(data.get("enabled", bool(venue["enabled"]))) else 0
                operating_status = str(
                    data.get("operatingStatus", venue["operating_status"])
                ).upper()
                maintenance_note = str(
                    data.get("maintenanceNote", venue["maintenance_note"])
                ).strip()[:200]
                expected_restore_at = data.get(
                    "expectedRestoreAt", venue["expected_restore_at"]
                )
                if expected_restore_at in {"", 0, "0"}:
                    expected_restore_at = None
                elif expected_restore_at is not None:
                    expected_restore_at = int(expected_restore_at)
                if (
                    not name
                    or count < 1
                    or count > 100
                    or price < 0
                    or operating_status not in {"OPEN", "CLOSED", "MAINTENANCE"}
                ):
                    raise ApiError(400, "VALIDATION_ERROR", "场馆配置无效")
                db.execute(
                    """UPDATE venues SET name=?,court_count=?,price_yuan=?,enabled=?,
                       operating_status=?,maintenance_note=?,expected_restore_at=?,
                       updated_at=? WHERE id=?""",
                    (
                        name,
                        count,
                        price,
                        enabled,
                        operating_status,
                        maintenance_note,
                        expected_restore_at,
                        now_ms(),
                        venue["id"],
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "VENUE_UPDATED",
                    "VENUE",
                    venue["id"],
                    {
                        "enabled": bool(enabled),
                        "operatingStatus": operating_status,
                    },
                )
                return row_dict(
                    db.execute("SELECT * FROM venues WHERE id=?", (venue["id"],)).fetchone()
                ), 200

            match = re.fullmatch(r"/api/v1/admin/venues/([^/]+)/time-slots", path)
            if method == "POST" and match:
                data = self.body_json()
                start = required_text(data, "startTime", 5)
                end = required_text(data, "endTime", 5)
                try:
                    datetime.strptime(start, "%H:%M")
                    datetime.strptime(end, "%H:%M")
                except ValueError as exc:
                    raise ApiError(400, "VALIDATION_ERROR", "时间格式必须为 HH:MM") from exc
                if start >= end:
                    raise ApiError(400, "VALIDATION_ERROR", "结束时间必须晚于开始时间")
                venue_id = match.group(1)
                if not db.execute("SELECT 1 FROM venues WHERE id=?", (venue_id,)).fetchone():
                    raise ApiError(404, "VENUE_NOT_FOUND", "场馆不存在")
                slot_id = f"{venue_id}-{start}"
                db.execute(
                    """INSERT INTO venue_time_slots(id,venue_id,start_time,end_time,enabled)
                       VALUES(?,?,?,?,1) ON CONFLICT(venue_id,start_time,end_time)
                       DO UPDATE SET enabled=1""",
                    (slot_id, venue_id, start, end),
                )
                write_audit(
                    db,
                    admin["id"],
                    "TIME_SLOT_CREATED",
                    "TIME_SLOT",
                    slot_id,
                    {"venueId": venue_id, "startTime": start, "endTime": end},
                )
                return {
                    "id": slot_id,
                    "venueId": venue_id,
                    "timeSlot": f"{start}-{end}",
                    "enabled": True,
                }, 201

            match = re.fullmatch(r"/api/v1/admin/time-slots/([^/]+)", path)
            if method == "PATCH" and match:
                data = self.body_json()
                changed = db.execute(
                    "UPDATE venue_time_slots SET enabled=? WHERE id=?",
                    (1 if bool(data.get("enabled")) else 0, match.group(1)),
                ).rowcount
                if not changed:
                    raise ApiError(404, "TIME_SLOT_NOT_FOUND", "时间段不存在")
                write_audit(
                    db,
                    admin["id"],
                    "TIME_SLOT_UPDATED",
                    "TIME_SLOT",
                    match.group(1),
                    {"enabled": bool(data.get("enabled"))},
                )
                return {"id": match.group(1), "enabled": bool(data.get("enabled"))}, 200

            if method == "GET" and path == "/api/v1/admin/reservations":
                clauses, values = [], []
                if query.get("venueId"):
                    clauses.append("r.venue_id=?")
                    values.append(query["venueId"])
                if query.get("date"):
                    clauses.append("r.booking_date=?")
                    values.append(parse_iso_date(query["date"]))
                if query.get("status", "").upper() in {"ACTIVE", "CANCELLED"}:
                    clauses.append("r.status=?")
                    values.append(query["status"].upper())
                if query.get("user"):
                    clauses.append(
                        """(u.nickname LIKE ? OR u.phone_masked LIKE ? OR
                            u.student_id LIKE ? OR u.real_name LIKE ? OR u.id LIKE ?)"""
                    )
                    token = f"%{query['user'][:80]}%"
                    values.extend([token, token, token, token, token])
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                return [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT r.*,v.name AS venue_name,v.unit_label,u.nickname,
                                  u.phone_masked,u.student_id,u.real_name,u.campus_verified,
                                  u.account_type
                           FROM reservations r
                           JOIN venues v ON v.id=r.venue_id
                           JOIN users u ON u.id=r.user_id"""
                        + where
                        + " ORDER BY r.created_at DESC LIMIT 500",
                        values,
                    )
                ], 200

            match = re.fullmatch(r"/api/v1/admin/reservations/([^/]+)/cancel", path)
            if method == "POST" and match:
                data = self.body_json()
                reservation = db.execute(
                    "SELECT * FROM reservations WHERE id=? AND status='ACTIVE'",
                    (match.group(1),),
                ).fetchone()
                if not reservation:
                    raise ApiError(409, "CANNOT_CANCEL", "预约不存在或已取消")
                timestamp = now_ms()
                changed = db.execute(
                    """UPDATE reservations SET status='CANCELLED',cancelled_at=?,
                       cancelled_by='ADMIN',cancel_reason=?,updated_at=?
                       WHERE id=? AND status='ACTIVE'""",
                    (
                        timestamp,
                        str(data.get("reason", "管理员取消")).strip()[:200] or "管理员取消",
                        timestamp,
                        match.group(1),
                    ),
                ).rowcount
                if not changed:
                    raise ApiError(409, "CANNOT_CANCEL", "预约不存在或已取消")
                write_reservation_event(
                    db,
                    reservation["id"],
                    reservation["user_id"],
                    admin["id"],
                    "ADMIN_CANCELLED",
                    str(data.get("reason", "管理员取消")).strip()[:200],
                )
                write_audit(
                    db,
                    admin["id"],
                    "RESERVATION_CANCELLED",
                    "RESERVATION",
                    reservation["id"],
                    {"userId": reservation["user_id"]},
                )
                return {"id": match.group(1), "status": "CANCELLED"}, 200

            if method == "GET" and path == "/api/v1/admin/lost-found":
                clauses, values = [], []
                if query.get("type", "").upper() in LOST_TYPES:
                    clauses.append("p.type=?")
                    values.append(query["type"].upper())
                if query.get("status"):
                    clauses.append("p.status=?")
                    values.append(query["status"].upper())
                if query.get("q"):
                    clauses.append(
                        "(p.item_name LIKE ? OR p.location LIKE ? OR p.description LIKE ?)"
                    )
                    token = f"%{query['q'][:80]}%"
                    values.extend([token, token, token])
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                return [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT p.*,u.nickname,u.phone_masked,u.student_id,u.real_name,
                           (SELECT COUNT(*) FROM lost_found_reports rr
                            WHERE rr.post_id=p.id AND rr.status='OPEN') AS open_reports
                           FROM lost_found_posts p
                           JOIN users u ON u.id=p.owner_user_id"""
                        + where
                        + " ORDER BY p.updated_at DESC LIMIT 500",
                        values,
                    )
                ], 200

            match = re.fullmatch(r"/api/v1/admin/lost-found/([^/]+)", path)
            if method == "PATCH" and match:
                data = self.body_json()
                post = db.execute(
                    "SELECT * FROM lost_found_posts WHERE id=?", (match.group(1),)
                ).fetchone()
                if not post:
                    raise ApiError(404, "POST_NOT_FOUND", "失物信息不存在")
                status = str(data.get("status", post["status"])).upper()
                if status not in LOST_STATUSES[post["type"]]:
                    raise ApiError(400, "VALIDATION_ERROR", "失物状态无效")
                db.execute(
                    "UPDATE lost_found_posts SET status=?,updated_at=? WHERE id=?",
                    (status, now_ms(), post["id"]),
                )
                write_audit(
                    db,
                    admin["id"],
                    "LOST_FOUND_STATUS_UPDATED",
                    "LOST_FOUND",
                    post["id"],
                    {"status": status},
                )
                return {"id": post["id"], "status": status}, 200

            if method == "DELETE" and match:
                post = db.execute(
                    "SELECT image_name FROM lost_found_posts WHERE id=?", (match.group(1),)
                ).fetchone()
                if not post:
                    raise ApiError(404, "POST_NOT_FOUND", "失物信息不存在")
                db.execute("DELETE FROM lost_found_posts WHERE id=?", (match.group(1),))
                if post["image_name"]:
                    run_safely_delete_upload(post["image_name"])
                write_audit(
                    db, admin["id"], "LOST_FOUND_DELETED", "LOST_FOUND", match.group(1)
                )
                return {"id": match.group(1), "deleted": True}, 200

            if method == "GET" and path == "/api/v1/admin/lost-found-reports":
                return [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT r.*,p.item_name,p.type,u.nickname,u.phone_masked
                           FROM lost_found_reports r
                           JOIN lost_found_posts p ON p.id=r.post_id
                           JOIN users u ON u.id=r.user_id
                           ORDER BY r.created_at DESC LIMIT 500"""
                    )
                ], 200

            match = re.fullmatch(r"/api/v1/admin/lost-found-reports/([^/]+)", path)
            if method == "PATCH" and match:
                data = self.body_json()
                status = str(data.get("status", "")).upper()
                if status not in {"OPEN", "REVIEWED", "CLOSED"}:
                    raise ApiError(400, "VALIDATION_ERROR", "举报状态无效")
                changed = db.execute(
                    "UPDATE lost_found_reports SET status=? WHERE id=?",
                    (status, match.group(1)),
                ).rowcount
                if not changed:
                    raise ApiError(404, "REPORT_NOT_FOUND", "举报不存在")
                write_audit(
                    db,
                    admin["id"],
                    "LOST_FOUND_REPORT_UPDATED",
                    "LOST_FOUND_REPORT",
                    match.group(1),
                    {"status": status},
                )
                return {"id": match.group(1), "status": status}, 200

            if method == "GET" and path == "/api/v1/admin/announcements":
                return [
                    {**row_dict(row), "enabled": bool(row["enabled"])}
                    for row in db.execute(
                        "SELECT * FROM announcements ORDER BY published_at DESC"
                    )
                ], 200

            if method == "POST" and path == "/api/v1/admin/announcements":
                data = self.body_json()
                announcement_id = str(uuid.uuid4())
                timestamp = now_ms()
                db.execute(
                    "INSERT INTO announcements VALUES(?,?,?,?,?,?)",
                    (
                        announcement_id,
                        required_text(data, "title", 100),
                        required_text(data, "content", 4000),
                        1 if bool(data.get("enabled", True)) else 0,
                        timestamp,
                        timestamp,
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "ANNOUNCEMENT_CREATED",
                    "ANNOUNCEMENT",
                    announcement_id,
                    {"enabled": bool(data.get("enabled", True))},
                )
                return {"id": announcement_id}, 201

            match = re.fullmatch(r"/api/v1/admin/announcements/([^/]+)", path)
            if method == "PATCH" and match:
                data = self.body_json()
                current = db.execute(
                    "SELECT * FROM announcements WHERE id=?", (match.group(1),)
                ).fetchone()
                if not current:
                    raise ApiError(404, "ANNOUNCEMENT_NOT_FOUND", "公告不存在")
                title = str(data.get("title", current["title"])).strip()[:100]
                content = str(data.get("content", current["content"])).strip()[:4000]
                enabled = 1 if bool(data.get("enabled", bool(current["enabled"]))) else 0
                if not title or not content:
                    raise ApiError(400, "VALIDATION_ERROR", "公告标题和内容不能为空")
                db.execute(
                    """UPDATE announcements SET title=?,content=?,enabled=?,updated_at=?
                       WHERE id=?""",
                    (title, content, enabled, now_ms(), current["id"]),
                )
                write_audit(
                    db,
                    admin["id"],
                    "ANNOUNCEMENT_UPDATED",
                    "ANNOUNCEMENT",
                    current["id"],
                    {"enabled": bool(enabled)},
                )
                return {"id": current["id"], "enabled": bool(enabled)}, 200

            if method == "DELETE" and match:
                changed = db.execute(
                    "DELETE FROM announcements WHERE id=?", (match.group(1),)
                ).rowcount
                if not changed:
                    raise ApiError(404, "ANNOUNCEMENT_NOT_FOUND", "公告不存在")
                write_audit(
                    db,
                    admin["id"],
                    "ANNOUNCEMENT_DELETED",
                    "ANNOUNCEMENT",
                    match.group(1),
                )
                return {"id": match.group(1), "deleted": True}, 200

            if method == "GET" and path == "/api/v1/admin/config":
                flags = []
                for row in db.execute("SELECT * FROM remote_flags ORDER BY key"):
                    flags.append(
                        {
                            "key": row["key"],
                            "enabled": bool(row["enabled"]),
                            "payload": json.loads(row["payload_json"]),
                            "updatedAt": row["updated_at"],
                        }
                    )
                return {
                    "flags": flags,
                    "version": row_dict(
                        db.execute(
                            "SELECT * FROM app_versions WHERE platform='android'"
                        ).fetchone()
                    ),
                }, 200

            match = re.fullmatch(r"/api/v1/admin/flags/([^/]+)", path)
            if method == "PUT" and match:
                data = self.body_json()
                payload = data.get("payload", {})
                if not isinstance(payload, dict):
                    raise ApiError(400, "VALIDATION_ERROR", "功能开关配置必须是对象")
                status = str(payload.get("status", "OPEN")).upper()
                if status not in {"OPEN", "CLOSED", "MAINTENANCE"}:
                    raise ApiError(400, "VALIDATION_ERROR", "功能运行状态无效")
                payload = {
                    **payload,
                    "status": status,
                    "maintenanceNote": str(payload.get("maintenanceNote", "")).strip()[:300],
                    "expectedRestoreAt": payload.get("expectedRestoreAt") or None,
                }
                db.execute(
                    """INSERT INTO remote_flags(key,enabled,payload_json,updated_at)
                       VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                       enabled=excluded.enabled,payload_json=excluded.payload_json,
                       updated_at=excluded.updated_at""",
                    (
                        match.group(1),
                        1 if bool(data.get("enabled")) else 0,
                        json.dumps(payload, ensure_ascii=False),
                        now_ms(),
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "FEATURE_FLAG_UPDATED",
                    "REMOTE_FLAG",
                    match.group(1),
                    {
                        "enabled": bool(data.get("enabled")),
                        "status": str(payload.get("status", "")),
                    },
                )
                return {
                    "key": match.group(1),
                    "enabled": bool(data.get("enabled")),
                    "payload": payload,
                }, 200

            if method == "PUT" and path == "/api/v1/admin/version/android":
                data = self.body_json()
                latest = int(data.get("latestVersionCode", 0))
                minimum = int(data.get("minimumVersionCode", 0))
                if latest < 1 or minimum < 1 or minimum > latest:
                    raise ApiError(400, "VALIDATION_ERROR", "版本号配置无效")
                db.execute(
                    """INSERT INTO app_versions(
                           platform,latest_version_code,minimum_version_code,
                           latest_version_name,update_message,download_url,updated_at
                       ) VALUES('android',?,?,?,?,?,?)
                       ON CONFLICT(platform) DO UPDATE SET
                       latest_version_code=excluded.latest_version_code,
                       minimum_version_code=excluded.minimum_version_code,
                       latest_version_name=excluded.latest_version_name,
                       update_message=excluded.update_message,
                       download_url=excluded.download_url,
                       updated_at=excluded.updated_at""",
                    (
                        latest,
                        minimum,
                        required_text(data, "latestVersionName", 40),
                        str(data.get("updateMessage", ""))[:1000],
                        str(data.get("downloadUrl", ""))[:500] or None,
                        now_ms(),
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "APP_VERSION_UPDATED",
                    "APP_VERSION",
                    "android",
                    {"latestVersionCode": latest, "minimumVersionCode": minimum},
                )
                return {
                    "platform": "android",
                    "latestVersionCode": latest,
                    "minimumVersionCode": minimum,
                }, 200

            if method == "GET" and path == "/api/v1/admin/feedback":
                clauses, values = [], []
                if query.get("status", "").upper() in {
                    "OPEN",
                    "PROCESSING",
                    "RESOLVED",
                    "CLOSED",
                }:
                    clauses.append("f.workflow_status=?")
                    values.append(query["status"].upper())
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                return [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT f.*,u.nickname,u.phone_masked,u.student_id,u.real_name
                           FROM feedback f
                           LEFT JOIN users u ON u.id=f.user_id"""
                        + where
                        + " ORDER BY f.created_at DESC LIMIT 500",
                        values,
                    )
                ], 200

            match = re.fullmatch(r"/api/v1/admin/feedback/([^/]+)", path)
            if method == "PATCH" and match:
                data = self.body_json()
                status = str(data.get("status", "")).upper()
                if status not in {"OPEN", "PROCESSING", "RESOLVED", "CLOSED"}:
                    raise ApiError(400, "VALIDATION_ERROR", "反馈状态无效")
                changed = db.execute(
                    """UPDATE feedback SET status=?,workflow_status=?,updated_at=?
                       WHERE id=?""",
                    ("CLOSED" if status == "RESOLVED" else status, status, now_ms(), match.group(1)),
                ).rowcount
                if not changed:
                    raise ApiError(404, "FEEDBACK_NOT_FOUND", "反馈不存在")
                write_audit(
                    db,
                    admin["id"],
                    "FEEDBACK_STATUS_UPDATED",
                    "FEEDBACK",
                    match.group(1),
                    {"status": status},
                )
                return {"id": match.group(1), "status": status}, 200

        raise ApiError(404, "NOT_FOUND", "管理员接口不存在")

    def admin_route_legacy(self, method: str, path: str) -> tuple[Any, int]:
        with connection() as db:
            self.current_user(db, "ADMIN")
            if method == "GET" and path == "/api/v1/admin/reservations":
                return [row_dict(row) for row in db.execute("SELECT * FROM reservations ORDER BY created_at DESC LIMIT 500")], 200
            if method == "GET" and path == "/api/v1/admin/lost-found":
                return [row_dict(row) for row in db.execute("SELECT * FROM lost_found_posts ORDER BY updated_at DESC LIMIT 500")], 200
            if method == "GET" and path == "/api/v1/admin/venues":
                return [row_dict(row) for row in db.execute("SELECT * FROM venues ORDER BY sport")], 200
            if method == "GET" and path == "/api/v1/admin/feedback":
                return [row_dict(row) for row in db.execute("SELECT * FROM feedback ORDER BY created_at DESC LIMIT 500")], 200
            if method == "GET" and path == "/api/v1/admin/lost-found-reports":
                return [row_dict(row) for row in db.execute("SELECT * FROM lost_found_reports ORDER BY created_at DESC LIMIT 500")], 200
            if method == "POST" and path == "/api/v1/admin/announcements":
                data = self.body_json()
                announcement_id = str(uuid.uuid4())
                timestamp = now_ms()
                db.execute(
                    "INSERT INTO announcements VALUES(?,?,?,?,?,?)",
                    (announcement_id, required_text(data, "title", 100),
                     required_text(data, "content", 4000), 1, timestamp, timestamp),
                )
                return {"id": announcement_id}, 201
            if method == "POST" and path == "/api/v1/admin/inventory/block":
                data = self.body_json()
                db.execute(
                    """INSERT INTO blocked_inventory(id,venue_id,booking_date,time_slot,court_number,reason)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(venue_id,booking_date,time_slot,court_number)
                       DO UPDATE SET reason=excluded.reason""",
                    (str(uuid.uuid4()), required_text(data, "venueId", 80),
                     parse_iso_date(required_text(data, "date", 10)),
                     required_text(data, "timeSlot", 20), int(data.get("courtNumber", 0)),
                     str(data.get("reason", "场馆暂不可用"))[:100]),
                )
                return {"blocked": True}, 200
            match = re.fullmatch(r"/api/v1/admin/venues/([^/]+)", path)
            if method == "PATCH" and match:
                data = self.body_json()
                venue = db.execute("SELECT * FROM venues WHERE id=?", (match.group(1),)).fetchone()
                if not venue:
                    raise ApiError(404, "VENUE_NOT_FOUND", "场馆不存在")
                name = str(data.get("name", venue["name"])).strip()[:80]
                count = int(data.get("courtCount", venue["court_count"]))
                price = int(data.get("priceYuan", venue["price_yuan"]))
                enabled = 1 if bool(data.get("enabled", bool(venue["enabled"]))) else 0
                if not name or count < 1 or count > 100 or price < 0:
                    raise ApiError(400, "VALIDATION_ERROR", "场馆配置无效")
                db.execute(
                    """UPDATE venues SET name=?,court_count=?,price_yuan=?,enabled=?,updated_at=?
                       WHERE id=?""",
                    (name, count, price, enabled, now_ms(), venue["id"]),
                )
                return row_dict(db.execute("SELECT * FROM venues WHERE id=?", (venue["id"],)).fetchone()), 200
            match = re.fullmatch(r"/api/v1/admin/venues/([^/]+)/time-slots", path)
            if method == "POST" and match:
                data = self.body_json()
                start = required_text(data, "startTime", 5)
                end = required_text(data, "endTime", 5)
                try:
                    datetime.strptime(start, "%H:%M")
                    datetime.strptime(end, "%H:%M")
                except ValueError as exc:
                    raise ApiError(400, "VALIDATION_ERROR", "时间格式必须为 HH:MM") from exc
                if start >= end:
                    raise ApiError(400, "VALIDATION_ERROR", "结束时间必须晚于开始时间")
                venue_id = match.group(1)
                if not db.execute("SELECT 1 FROM venues WHERE id=?", (venue_id,)).fetchone():
                    raise ApiError(404, "VENUE_NOT_FOUND", "场馆不存在")
                slot_id = f"{venue_id}-{start}"
                db.execute(
                    """INSERT INTO venue_time_slots(id,venue_id,start_time,end_time,enabled)
                       VALUES(?,?,?,?,1) ON CONFLICT(venue_id,start_time,end_time)
                       DO UPDATE SET enabled=1""",
                    (slot_id, venue_id, start, end),
                )
                return {"id": slot_id, "venueId": venue_id, "timeSlot": f"{start}-{end}", "enabled": True}, 201
            match = re.fullmatch(r"/api/v1/admin/reservations/([^/]+)/cancel", path)
            if method == "POST" and match:
                changed = db.execute(
                    "UPDATE reservations SET status='CANCELLED',updated_at=? WHERE id=? AND status='ACTIVE'",
                    (now_ms(), match.group(1)),
                ).rowcount
                if not changed:
                    raise ApiError(409, "CANNOT_CANCEL", "预约不存在或已取消")
                return {"id": match.group(1), "status": "CANCELLED"}, 200
            match = re.fullmatch(r"/api/v1/admin/flags/([^/]+)", path)
            if method == "PUT" and match:
                data = self.body_json()
                payload = data.get("payload", {})
                db.execute(
                    """INSERT INTO remote_flags(key,enabled,payload_json,updated_at)
                       VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                       enabled=excluded.enabled,payload_json=excluded.payload_json,
                       updated_at=excluded.updated_at""",
                    (match.group(1), 1 if bool(data.get("enabled")) else 0,
                     json.dumps(payload, ensure_ascii=False), now_ms()),
                )
                return {"key": match.group(1), "enabled": bool(data.get("enabled")), "payload": payload}, 200
            if method == "PUT" and path == "/api/v1/admin/version/android":
                data = self.body_json()
                latest = int(data.get("latestVersionCode", 0))
                minimum = int(data.get("minimumVersionCode", 0))
                if latest < 1 or minimum < 1 or minimum > latest:
                    raise ApiError(400, "VALIDATION_ERROR", "版本号配置无效")
                db.execute(
                    """INSERT INTO app_versions(platform,latest_version_code,minimum_version_code,
                       latest_version_name,update_message,download_url,updated_at)
                       VALUES('android',?,?,?,?,?,?) ON CONFLICT(platform) DO UPDATE SET
                       latest_version_code=excluded.latest_version_code,
                       minimum_version_code=excluded.minimum_version_code,
                       latest_version_name=excluded.latest_version_name,
                       update_message=excluded.update_message,
                       download_url=excluded.download_url,updated_at=excluded.updated_at""",
                    (latest, minimum, required_text(data, "latestVersionName", 40),
                     str(data.get("updateMessage", ""))[:1000],
                     str(data.get("downloadUrl", ""))[:500] or None, now_ms()),
                )
                return {"platform": "android", "latestVersionCode": latest, "minimumVersionCode": minimum}, 200
            match = re.fullmatch(r"/api/v1/admin/feedback/([^/]+)", path)
            if method == "PATCH" and match:
                data = self.body_json()
                status = str(data.get("status", "")).upper()
                if status not in {"OPEN", "PROCESSING", "CLOSED"}:
                    raise ApiError(400, "VALIDATION_ERROR", "反馈状态无效")
                changed = db.execute(
                    "UPDATE feedback SET status=?,updated_at=? WHERE id=?",
                    (status, now_ms(), match.group(1)),
                ).rowcount
                if not changed:
                    raise ApiError(404, "FEEDBACK_NOT_FOUND", "反馈不存在")
                return {"id": match.group(1), "status": status}, 200
        raise ApiError(404, "NOT_FOUND", "管理员接口不存在")


def run_safely_delete_upload(name: str) -> None:
    target = (UPLOADS / name).resolve()
    if target.parent == UPLOADS.resolve() and target.is_file():
        target.unlink()


def main() -> None:
    initialize_database()
    print(f"My Changli backend listening on http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    ThreadingHTTPServer((HOST, PORT), ApiHandler).serve_forever()


if __name__ == "__main__":
    main()
