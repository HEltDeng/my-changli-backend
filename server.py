"""SICAU campus-tools backend MVP.

Small HTTP + SQLite service for local development. It deliberately
does not include payment, reservation codes, public deployment, or academic-system
credentials.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
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
from cryptography.fernet import Fernet, InvalidToken
from core import (
    ApiError,
    ClosingConnection,
    DB_PATH,
    MAX_BODY,
    SESSION_SECONDS,
    connection,
    json_response,
    now_ms,
    paginated_response,
    parse_pagination,
    required_text,
    row_dict,
)

ROOT = Path(__file__).resolve().parent
UPLOADS = ROOT / "uploads"
ADMIN_WEB = ROOT / "admin"
HOST = os.getenv("MY_CHANGLI_HOST", "127.0.0.1")
PORT = int(os.getenv("MY_CHANGLI_PORT", "8787"))
SCHOOL_NAME = os.getenv("MY_CHANGLI_SCHOOL_NAME", "四川农业大学")
SCHOOL_SHORT_NAME = os.getenv("MY_CHANGLI_SCHOOL_SHORT_NAME", "川农")
APP_DISPLAY_NAME = os.getenv("MY_CHANGLI_APP_DISPLAY_NAME", "川农")
SPORTS = {"BADMINTON", "TABLE_TENNIS"}
LOST_TYPES = {"LOST", "FOUND"}
LOST_STATUSES = {
    "LOST": {"SEARCHING", "FOUND"},
    "FOUND": {"WAITING", "CLAIMED", "RETURNED"},
}
FEEDBACK_CATEGORIES = {
    "FEATURE_SUGGESTION",
    "RESERVATION",
    "LOST_FOUND",
    "RESOURCE_LIBRARY",
    "ACCOUNT_SECURITY",
    "CONTENT_REPORT",
    "OTHER",
}
FEEDBACK_STATUSES = {"OPEN", "PROCESSING", "RESOLVED", "CLOSED"}
RESERVATION_PROFILE_MODES = {"OFF", "OPTIONAL", "REQUIRED"}
RESERVATION_PROFILE_VERSION = 1
ACCOUNT_STATUSES = {"ACTIVE", "LIMITED", "DISABLED", "PENDING_DELETION", "DELETED"}
USER_EVENT_RESULTS = {"SUCCESS", "FAILURE", "DENIED"}
USER_EVENT_RETENTION_DAYS = 180
ADMIN_ROLES = {
    "SUPER_ADMIN",
    "SPORTS_OPERATOR",
    "LOST_FOUND_OPERATOR",
    "LIBRARY_OPERATOR",
    "CONTENT_OPERATOR",
}
ROLE_PERMISSIONS = {
    "SUPER_ADMIN": {"*"},
    "SPORTS_OPERATOR": {"SPORTS_VIEW", "SPORTS_MANAGE", "SPORTS_PII_VIEW"},
    "LOST_FOUND_OPERATOR": {"LOST_VIEW", "LOST_MANAGE", "LOST_PII_VIEW"},
    "LIBRARY_OPERATOR": {"LIBRARY_VIEW", "LIBRARY_MANAGE", "LIBRARY_PII_VIEW"},
    "CONTENT_OPERATOR": {"CONTENT_VIEW", "CONTENT_MANAGE"},
}
CONTROLLED_FEATURES = {"sports_reservation", "lost_found", "resource_library"}


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
        ensure_column(db, "users", "nickname", f"TEXT NOT NULL DEFAULT '{SCHOOL_SHORT_NAME}同学'")
        ensure_column(db, "users", "phone_hash", "TEXT")
        ensure_column(db, "users", "phone_masked", "TEXT")
        ensure_column(db, "users", "phone_encrypted", "TEXT")
        ensure_column(db, "users", "student_id", "TEXT")
        ensure_column(db, "users", "real_name", "TEXT")
        ensure_column(db, "users", "campus_verified", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "users", "campus_verified_at", "INTEGER")
        ensure_column(db, "users", "reservation_student_id", "TEXT")
        ensure_column(db, "users", "reservation_name", "TEXT")
        ensure_column(db, "users", "reservation_profile_consented_at", "INTEGER")
        ensure_column(db, "users", "reservation_student_id_hash", "TEXT")
        ensure_column(db, "users", "reservation_name_hash", "TEXT")
        ensure_column(
            db, "users", "reservation_profile_version", "INTEGER NOT NULL DEFAULT 0"
        )
        ensure_column(db, "users", "reservation_profile_updated_at", "INTEGER")
        ensure_column(db, "users", "admin_permissions_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(db, "users", "admin_role", "TEXT")
        ensure_column(db, "users", "admin_enabled", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(db, "users", "account_status", "TEXT NOT NULL DEFAULT 'ACTIVE'")
        ensure_column(db, "users", "account_status_reason", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "users", "account_status_changed_at", "INTEGER")
        ensure_column(db, "users", "account_status_expires_at", "INTEGER")
        ensure_column(db, "users", "last_active_at", "INTEGER")
        ensure_column(db, "users", "first_login_at", "INTEGER")
        ensure_column(db, "app_versions", "apk_sha256", "TEXT")
        ensure_column(db, "app_versions", "apk_size", "INTEGER")
        ensure_column(db, "app_versions", "force_update", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "app_versions", "status", "TEXT NOT NULL DEFAULT 'DRAFT'")
        ensure_column(db, "app_versions", "published_at", "INTEGER")
        ensure_column(db, "app_versions", "created_at", "INTEGER")
        ensure_column(db, "app_versions", "created_by", "TEXT")
        ensure_column(db, "app_versions", "updated_by", "TEXT")
        ensure_column(db, "remote_flags", "updated_by", "TEXT")
        ensure_column(db, "remote_flags", "change_note", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "announcements", "service_visible", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(db, "reservations", "refund_status", "TEXT NOT NULL DEFAULT 'NOT_APPLICABLE'")
        ensure_column(db, "reservations", "reservation_code", "TEXT")
        ensure_column(db, "reservations", "verification_status", "TEXT NOT NULL DEFAULT 'NOT_REQUIRED'")
        ensure_column(db, "reservations", "cancelled_at", "INTEGER")
        ensure_column(db, "reservations", "cancelled_by", "TEXT")
        ensure_column(db, "reservations", "cancel_reason", "TEXT")
        ensure_column(db, "reservations", "reservation_student_id_snapshot", "TEXT")
        ensure_column(db, "reservations", "reservation_name_snapshot", "TEXT")
        ensure_column(db, "feedback", "workflow_status", "TEXT NOT NULL DEFAULT 'OPEN'")
        ensure_column(db, "feedback", "title", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "feedback", "public_reply", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "feedback", "internal_note", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "feedback", "handled_by", "TEXT")
        ensure_column(db, "feedback", "resolved_at", "INTEGER")
        ensure_column(
            db,
            "reservation_rules",
            "require_reservation_student_id",
            "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(
            db,
            "reservation_rules",
            "reservation_profile_mode",
            "TEXT NOT NULL DEFAULT 'OPTIONAL'",
        )
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_phone_hash ON users(phone_hash) WHERE phone_hash IS NOT NULL")
        db.execute(
            """UPDATE users SET account_type=CASE
               WHEN role='ADMIN' THEN 'ADMIN'
               WHEN username LIKE 'device_%' THEN 'DEVICE'
               ELSE account_type END"""
        )
        db.execute(
            """UPDATE users SET admin_permissions_json='["*"]'
               WHERE role='ADMIN' AND
               (admin_permissions_json IS NULL OR admin_permissions_json='[]'
                OR admin_permissions_json='["PII_VIEW"]')"""
        )
        db.execute(
            """UPDATE users SET admin_role='SUPER_ADMIN',admin_enabled=1
               WHERE role='ADMIN' AND (admin_role IS NULL OR admin_role='')"""
        )
        db.execute(
            """UPDATE app_versions SET status='PUBLISHED',created_at=updated_at,
               published_at=COALESCE(published_at,updated_at)
               WHERE created_at IS NULL"""
        )
        db.execute("CREATE INDEX IF NOT EXISTS users_admin_role ON users(role,admin_role,admin_enabled)")
        db.execute("CREATE INDEX IF NOT EXISTS users_last_active ON users(last_active_at)")
        db.execute("CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id,expires_at)")
        db.execute("CREATE INDEX IF NOT EXISTS reservations_booking ON reservations(booking_date,status)")
        db.execute(
            "UPDATE feedback SET workflow_status=status WHERE workflow_status IS NULL OR workflow_status=''"
        )
        db.execute(
            """UPDATE reservation_rules
               SET reservation_profile_mode=CASE
                   WHEN require_reservation_student_id=1 THEN 'REQUIRED'
                   ELSE COALESCE(NULLIF(reservation_profile_mode,''),'OPTIONAL')
               END
               WHERE id='default'"""
        )
        db.execute(
            """UPDATE users SET reservation_profile_version=?
               WHERE reservation_profile_consented_at IS NOT NULL
                 AND reservation_profile_version=0""",
            (RESERVATION_PROFILE_VERSION,),
        )
        db.execute(
            """UPDATE users SET reservation_profile_updated_at=reservation_profile_consented_at
               WHERE reservation_profile_consented_at IS NOT NULL
                 AND reservation_profile_updated_at IS NULL"""
        )
        for profile in db.execute(
            """SELECT id,reservation_student_id,reservation_name
               FROM users
               WHERE reservation_student_id IS NOT NULL OR reservation_name IS NOT NULL"""
        ).fetchall():
            db.execute(
                """UPDATE users SET reservation_student_id_hash=?,
                   reservation_name_hash=? WHERE id=?""",
                (
                    reservation_pii_digest("student-id", profile["reservation_student_id"]),
                    reservation_pii_digest("real-name", profile["reservation_name"]),
                    profile["id"],
                ),
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
            ("resource_library", 1),
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
        for feature_key in ("sports_reservation", "lost_found", "resource_library"):
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
            if feature_key == "resource_library" and "allowGuestBrowsing" not in payload:
                payload["allowGuestBrowsing"] = False
                db.execute(
                    "UPDATE remote_flags SET payload_json=?,updated_at=? WHERE key=?",
                    (json.dumps(payload, ensure_ascii=False), timestamp, feature_key),
                )
        seed_resources(db, timestamp)
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
                   id,username,password_hash,password_salt,role,account_type,nickname,
                   admin_permissions_json,created_at
               ) VALUES(?,?,?,?, 'ADMIN','ADMIN','本地管理员','["*"]',?)
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


def reservation_pii_digest(kind: str, value: Any) -> str | None:
    normalized = re.sub(r"\s+", "", str(value or "")).casefold()
    if not normalized:
        return None
    payload = f"reservation-profile:{kind}:{normalized}".encode()
    return hmac.new(phone_hash_secret(), payload, hashlib.sha256).hexdigest()


def phone_mask(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}"


@lru_cache(maxsize=1)
def pii_cipher() -> Fernet:
    configured = os.getenv("MY_CHANGLI_PII_ENCRYPTION_KEY", "").strip().encode()
    key_path = DB_PATH.parent / ".pii-encryption-key"
    if configured:
        key = configured
    elif key_path.is_file():
        key = key_path.read_bytes().strip()
    else:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_path.write_bytes(key)
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("MY_CHANGLI_PII_ENCRYPTION_KEY 配置无效") from exc


def encrypt_phone(phone: str) -> str:
    return pii_cipher().encrypt(phone.encode()).decode()


def decrypt_phone(value: str | None) -> str | None:
    if not value:
        return None
    try:
        phone = pii_cipher().decrypt(value.encode()).decode()
        return phone if re.fullmatch(r"1[3-9]\d{9}", phone) else None
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return None


def normalize_reservation_student_id(value: Any, *, required: bool = False) -> str | None:
    student_id = re.sub(r"\s+", "", str(value or ""))[:32]
    if not student_id:
        if required:
            raise ApiError(400, "RESERVATION_PROFILE_REQUIRED", "请先填写预约识别学号")
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", student_id):
        raise ApiError(400, "INVALID_RESERVATION_STUDENT_ID", "预约识别学号格式不正确")
    return student_id


def normalize_reservation_name(value: Any, *, required: bool = False) -> str | None:
    name = re.sub(r"\s+", " ", str(value or "").strip())
    if not name:
        if required:
            raise ApiError(400, "RESERVATION_PROFILE_REQUIRED", "请填写真实姓名")
        return None
    if not 2 <= len(name) <= 40 or re.search(r"[<>]", name):
        raise ApiError(400, "INVALID_RESERVATION_NAME", "真实姓名格式不正确")
    return name


def mask_reservation_student_id(value: Any) -> str | None:
    text = str(value or "")
    if not text:
        return None
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * max(2, len(text) - 4)}{text[-2:]}"


def mask_reservation_name(value: Any) -> str | None:
    text = str(value or "")
    if not text:
        return None
    if len(text) == 1:
        return "*"
    if len(text) == 2:
        return f"{text[0]}*"
    return f"{text[0]}{'*' * (len(text) - 2)}{text[-1]}"


def mask_admin_user_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data.pop("phone_encrypted", None)
    if "reservation_student_id" in data:
        data["reservation_student_id"] = mask_reservation_student_id(
            data["reservation_student_id"]
        )
    if "reservation_name" in data:
        data["reservation_name"] = mask_reservation_name(data["reservation_name"])
    return data


def full_admin_user_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """SUPER_ADMIN view: complete business profile, never credentials."""
    data = dict(row)
    encrypted = data.pop("phone_encrypted", None)
    data["phone"] = decrypt_phone(encrypted) if encrypted else None
    return data


def mask_admin_reservation_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = mask_admin_user_row(row)
    if "reservation_student_id_snapshot" in data:
        data["reservation_student_id_snapshot"] = mask_reservation_student_id(
            data["reservation_student_id_snapshot"]
        )
    if "reservation_name_snapshot" in data:
        data["reservation_name_snapshot"] = mask_reservation_name(
            data["reservation_name_snapshot"]
        )
    return data


def full_sports_reservation_row(
    row: sqlite3.Row | dict[str, Any],
) -> dict[str, Any]:
    """Complete reservation contact/profile data for the sports workspace only."""
    data = dict(row)
    encrypted = data.pop("phone_encrypted", None)
    data["phone"] = decrypt_phone(encrypted) if encrypted else None
    # Queries feeding this serializer select only reservation and the minimum
    # sports-account fields; credentials and cross-module data are never selected.
    return data


def normalize_feedback_category(value: Any) -> str:
    raw = str(value or "").strip().upper()
    legacy = {
        "问题反馈": "OTHER",
        "功能建议": "FEATURE_SUGGESTION",
        "体验改进": "FEATURE_SUGGESTION",
        "其他": "OTHER",
    }
    category = legacy.get(str(value or "").strip(), raw)
    if category not in FEEDBACK_CATEGORIES:
        raise ApiError(400, "INVALID_FEEDBACK_CATEGORY", "反馈类型无效")
    return category


def admin_permissions(user: sqlite3.Row) -> set[str]:
    role = admin_role(user)
    mapped = ROLE_PERMISSIONS.get(role, set())
    if mapped:
        return set(mapped)
    try:
        values = json.loads(user["admin_permissions_json"] or "[]")
    except (json.JSONDecodeError, IndexError):
        values = []
    return {str(value) for value in values if isinstance(value, str)}


def admin_role(user: sqlite3.Row | dict[str, Any]) -> str:
    data = dict(user)
    value = str(data.get("admin_role") or "").upper()
    if value in ADMIN_ROLES:
        return value
    return "SUPER_ADMIN" if data.get("role") == "ADMIN" else ""


def is_super_admin(user: sqlite3.Row | dict[str, Any]) -> bool:
    return admin_role(user) == "SUPER_ADMIN"


def require_admin_permission(user: sqlite3.Row, permission: str) -> None:
    permissions = admin_permissions(user)
    if "*" not in permissions and permission not in permissions:
        raise ApiError(403, "ADMIN_PERMISSION_REQUIRED", "当前管理员没有此敏感操作权限")


def require_admin_path_access(user: sqlite3.Row, method: str, path: str) -> None:
    """Enforce module isolation before any admin route reads data."""
    if is_super_admin(user):
        return
    role = admin_role(user)
    if not bool(user["admin_enabled"]):
        raise ApiError(403, "ADMIN_DISABLED", "管理员账号已停用")
    allowed = False
    if role == "SPORTS_OPERATOR":
        allowed = any(token in path for token in ("/venues", "/inventory", "/reservations", "/reservation-rules", "/overview/sports"))
    elif role == "LOST_FOUND_OPERATOR":
        allowed = "/lost-found" in path or "/overview/lost-found" in path
    elif role == "LIBRARY_OPERATOR":
        allowed = any(token in path for token in ("/resources", "/resource-complaints", "/resource-activity", "/overview/library"))
    elif role == "CONTENT_OPERATOR":
        allowed = any(
            token in path
            for token in ("/announcements", "/website-announcements", "/overview/content")
        )
    if not allowed:
        raise ApiError(403, "ADMIN_MODULE_FORBIDDEN", "当前管理员无权访问该业务模块")


def require_account_operation(user: sqlite3.Row) -> None:
    if user["account_status"] in {"LIMITED", "SUSPENDED"}:
        reason = user["account_status_reason"] or "账号当前限制敏感操作，请联系管理员"
        raise ApiError(403, "ACCOUNT_OPERATION_RESTRICTED", reason)


def append_user_search(
    clauses: list[str],
    values: list[Any],
    raw_term: str | None,
    alias: str = "u",
) -> None:
    term = str(raw_term or "").strip()[:100]
    if not term:
        return
    compact = re.sub(r"\s+", "", term)
    if re.fullmatch(r"1[3-9]\d{9}", compact):
        clauses.append(f"{alias}.phone_hash=?")
        values.append(phone_digest(compact))
        return
    if re.fullmatch(r"[A-Za-z0-9_-]{6,32}", compact):
        clauses.append(
            f"({alias}.reservation_student_id_hash=? OR {alias}.id=?)"
        )
        values.extend(
            [reservation_pii_digest("student-id", compact), compact]
        )
        return
    if 2 <= len(term) <= 40 and re.fullmatch(r"[\u3400-\u9fff·.\- ]+", term):
        clauses.append(f"{alias}.reservation_name_hash=?")
        values.append(reservation_pii_digest("real-name", term))
        return
    token = f"%{term}%"
    clauses.append(
        f"""({alias}.nickname LIKE ? OR {alias}.phone_masked LIKE ?
            OR {alias}.id LIKE ?)"""
    )
    values.extend([token, token, token])


def validate_external_url(value: Any, *, required: bool = True) -> str | None:
    url = str(value or "").strip()[:1000]
    if not url:
        if required:
            raise ApiError(400, "INVALID_RESOURCE_URL", "资料链接不能为空")
        return None
    parsed = urlparse(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ApiError(400, "INVALID_RESOURCE_URL", "仅允许安全的 HTTP/HTTPS 资料链接")
    host = parsed.hostname.lower()
    if host in {"0.0.0.0"} or host.endswith(".local"):
        raise ApiError(400, "INVALID_RESOURCE_URL", "资料链接目标不可用")
    return url


def seed_resources(db: sqlite3.Connection, timestamp: int) -> None:
    resources = (
        (
            "computer_exam_archive",
            "四川农业大学官网",
            "学校新闻、公共服务、校历和各校区官方入口。",
            "学生服务",
            ["官网", "通知", "校历", "雅安", "成都", "都江堰"],
            "全校学生",
            SCHOOL_NAME,
            "https://www.sicau.edu.cn/",
        ),
        (
            "university_library",
            "教务处通知公告",
            "查看选课、考试、学籍、竞赛与教学运行相关通知。",
            "教务信息",
            ["教务", "选课", "考试", "学籍", "通知"],
            "全校学生",
            f"{SCHOOL_NAME}教务处",
            "https://jiaowu.sicau.edu.cn/",
        ),
        (
            "library_catalog_search",
            "教务管理系统入口",
            "进入川农教务管理系统；登录参数与课表解析仍以学生账号实测为准。",
            "教务信息",
            ["学号", "教务系统", "课表", "成绩", "考试"],
            "全校学生",
            f"{SCHOOL_NAME}教务管理系统",
            "https://jiaowu.sicau.edu.cn/web/web/web/index.asp",
        ),
        (
            "academic_affairs_notices",
            f"{SCHOOL_NAME}校历",
            "打开学校官方校历页面查看学期、教学周和假期安排。",
            "教务信息",
            ["校历", "学期", "教学周", "假期"],
            "全校学生",
            SCHOOL_NAME,
            "https://www.sicau.edu.cn/ggfw/rlxl.htm",
        ),
        (
            "current_student_services",
            "一网通办",
            "进入四川农业大学校内服务入口；需要统一身份认证时将由学校页面处理。",
            "学生服务",
            ["学生", "服务", "办事", "统一认证"],
            "全校学生",
            SCHOOL_NAME,
            "https://ywtb.sicau.edu.cn/",
        ),
    )
    for index, item in enumerate(resources):
        resource_id, title, summary, category, tags, audience, source, url = item
        db.execute(
            """INSERT INTO resource_items(
                   id,title,summary,category,tags_json,audience_scope,publisher_label,
                   source_label,usage_note,resource_type,external_url,publish_status,
                   visibility,is_free,sort_order,published_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,'请遵守资料来源页面的版权与使用规则。',
                        'EXTERNAL_LINK',?,'PUBLISHED','MEMBERS',1,?,?,?,?)
               ON CONFLICT(id) DO NOTHING""",
            (
                resource_id,
                title,
                summary,
                category,
                json.dumps(tags, ensure_ascii=False),
                audience,
                f"{APP_DISPLAY_NAME}资料库",
                source,
                url,
                100 - index,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        # Existing local databases may already contain the old school seed rows.
        # Migrate only those known seed IDs whose URL still points at the legacy
        # school; user-created resources are left untouched.
        db.execute(
            """UPDATE resource_items
               SET title=?,summary=?,category=?,tags_json=?,audience_scope=?,
                   publisher_label=?,source_label=?,external_url=?,updated_at=?
               WHERE id=? AND (external_url LIKE '%csust%' OR
                               external_url LIKE '%MedivhTirisfal%')""",
            (
                title,
                summary,
                category,
                json.dumps(tags, ensure_ascii=False),
                audience,
                f"{APP_DISPLAY_NAME}资料库",
                source,
                url,
                timestamp,
                resource_id,
            ),
        )


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
    for table, key in (
        ("resource_favorites", "resource_id"),
        ("resource_unlocks", "resource_id"),
    ):
        db.execute(
            f"""INSERT OR IGNORE INTO {table}
                SELECT ?,{key},"""
            + ("created_at" if table == "resource_favorites" else "unlock_status,price_fen,created_at")
            + f" FROM {table} WHERE user_id=?",
            (target_user_id, source_id),
        )
        db.execute(f"DELETE FROM {table} WHERE user_id=?", (source_id,))
    db.execute(
        """INSERT INTO resource_views(user_id,resource_id,view_count,last_viewed_at)
           SELECT ?,resource_id,view_count,last_viewed_at FROM resource_views
           WHERE user_id=? ON CONFLICT(user_id,resource_id) DO UPDATE SET
           view_count=resource_views.view_count+excluded.view_count,
           last_viewed_at=MAX(resource_views.last_viewed_at,excluded.last_viewed_at)""",
        (target_user_id, source_id),
    )
    db.execute("DELETE FROM resource_views WHERE user_id=?", (source_id,))
    db.execute(
        "UPDATE resource_complaints SET user_id=? WHERE user_id=?",
        (target_user_id, source_id),
    )
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
        # Academic-system identity is deliberately not part of the My Changli account API.
        "reservationStudentId": user["reservation_student_id"],
        "reservationName": user["reservation_name"],
        "reservationProfileConsentedAt": user["reservation_profile_consented_at"],
        "reservationProfileVersion": user["reservation_profile_version"],
        "reservationProfileUpdatedAt": user["reservation_profile_updated_at"],
        "adminPermissions": sorted(admin_permissions(user)) if user["role"] == "ADMIN" else [],
        "adminRole": admin_role(user) if user["role"] == "ADMIN" else None,
        "accountStatus": user["account_status"],
        "accountStatusReason": user["account_status_reason"],
        "accountStatusChangedAt": user["account_status_changed_at"],
        "accountStatusExpiresAt": user["account_status_expires_at"],
        "lastActiveAt": user["last_active_at"],
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


def write_user_event(
    db: sqlite3.Connection,
    user_id: str | None,
    module: str,
    event_type: str,
    result: str = "SUCCESS",
    target_type: str | None = None,
    target_id: str | None = None,
    client_source: str = "APP",
    client_version: str | None = None,
    risk_flag: str | None = None,
) -> None:
    """Store only a minimal operational event; never accepts free-form user content."""
    normalized_result = result.upper()
    if normalized_result not in USER_EVENT_RESULTS:
        normalized_result = "FAILURE"
    timestamp = now_ms()
    db.execute(
        """INSERT INTO user_behavior_events(
               id,user_id,module,event_type,result,target_type,target_id,
               client_source,client_version,risk_flag,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), user_id, module[:40], event_type[:60],
            normalized_result, (target_type or "")[:40] or None,
            (target_id or "")[:100] or None, client_source[:20],
            (client_version or "")[:40] or None, (risk_flag or "")[:60] or None,
            timestamp,
        ),
    )
    if not user_id:
        return
    rule = None
    threshold = 0
    window_ms = 10 * 60 * 1000
    if event_type == "LOGIN_FAILURE":
        rule, threshold = ("LOGIN_FAILURE_BURST", "短时间连续登录失败"), 5
    elif event_type == "FEEDBACK_SUBMITTED":
        rule, threshold = ("FEEDBACK_BURST", "短时间高频提交反馈"), 5
    elif event_type in {"LOST_FOUND_PUBLISHED", "LOST_FOUND_REPORTED"}:
        rule, threshold = ("LOST_FOUND_BURST", "短时间高频发布或举报失物"), 8
    elif event_type == "RESERVATION_CANCELLED":
        rule, threshold, window_ms = ("RESERVATION_CANCEL_BURST", "短时间异常取消预约"), 5, 24 * 60 * 60 * 1000
    if not rule:
        return
    window_start = timestamp - window_ms
    count = db.execute(
        """SELECT COUNT(*) AS count FROM user_behavior_events
           WHERE user_id=? AND event_type=? AND created_at>=?""",
        (user_id, event_type, window_start),
    ).fetchone()["count"]
    if count < threshold:
        return
    bucket = timestamp - (timestamp % window_ms)
    db.execute(
        """INSERT INTO risk_candidates(
               id,user_id,rule_key,rule_label,trigger_count,window_started_at,
               window_ended_at,status,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,'OPEN',?,?)
           ON CONFLICT(user_id,rule_key,window_started_at) DO UPDATE SET
               trigger_count=excluded.trigger_count,
               window_ended_at=excluded.window_ended_at,
               updated_at=excluded.updated_at""",
        (str(uuid.uuid4()), user_id, rule[0], rule[1], count, bucket, timestamp, timestamp, timestamp),
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
            "updatedAt": None,
            "updatedBy": None,
            "changeNote": "",
        }
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    status = str(payload.get("status", "OPEN")).upper()
    if status not in {"OPEN", "CLOSED", "MAINTENANCE"}:
        status = "OPEN"
    if not bool(row["enabled"]):
        status = "CLOSED"
    return {
        "key": key,
        "enabled": bool(row["enabled"]),
        "status": status,
        "maintenanceNote": str(payload.get("maintenanceNote", ""))[:300],
        "expectedRestoreAt": payload.get("expectedRestoreAt"),
        "updatedAt": row["updated_at"],
        "updatedBy": row["updated_by"],
        "changeNote": str(row["change_note"] or "")[:300],
    }


def ensure_feature_open(db: sqlite3.Connection, key: str, label: str) -> None:
    state = feature_state(db, key)
    if state["status"] == "OPEN":
        return
    if state["status"] == "MAINTENANCE":
        note = state["maintenanceNote"] or f"{label}维护中，请稍后再试"
        raise ApiError(503, "FEATURE_MAINTENANCE", note)
    note = state["maintenanceNote"] or f"{label}暂不开放"
    raise ApiError(503, "FEATURE_CLOSED", note)


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


def reservation_usage(
    db: sqlite3.Connection, user_id: str, booking_date: str | None = None
) -> dict[str, int]:
    target_date = booking_date or date.today().isoformat()
    active_count = db.execute(
        """SELECT COUNT(*) AS count FROM reservations
           WHERE user_id=? AND booking_date=? AND status='ACTIVE'""",
        (user_id, target_date),
    ).fetchone()["count"]
    day = date.fromisoformat(target_date)
    day_start = int(datetime.combine(day, datetime.min.time()).timestamp() * 1000)
    day_end = int(
        datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp() * 1000
    )
    cancellation_count = db.execute(
        """SELECT COUNT(*) AS count FROM reservation_events
           WHERE user_id=? AND action='USER_CANCELLED'
           AND created_at>=? AND created_at<?""",
        (user_id, day_start, day_end),
    ).fetchone()["count"]
    return {"active": active_count, "cancelled": cancellation_count}


def serialize_reservation_context(
    db: sqlite3.Connection, user: sqlite3.Row
) -> dict[str, Any]:
    rules = reservation_rules(db)
    usage = reservation_usage(db, user["id"])
    restriction = active_restriction(db, user["id"])
    timestamp = now_ms()
    reservation_blocked_until = (
        restriction["reservation_blocked_until"] if restriction else None
    )
    cancellation_blocked_until = (
        restriction["cancellation_blocked_until"] if restriction else None
    )
    return {
        "rules": {
            "maxActivePerDay": rules["max_active_per_day"],
            "maxUserCancellationsPerDay": rules[
                "max_user_cancellations_per_day"
            ],
            "cancelCutoffMinutes": rules["cancel_cutoff_minutes"],
            "restrictionHoursAfterExcessCancel": rules[
                "restriction_hours_after_excess_cancel"
            ],
            "requireReservationStudentId": bool(
                rules["reservation_profile_mode"] == "REQUIRED"
            ),
            "reservationProfileMode": rules["reservation_profile_mode"],
            "updatedAt": rules["updated_at"],
        },
        "today": {
            "activeReservations": usage["active"],
            "remainingReservations": max(
                0, rules["max_active_per_day"] - usage["active"]
            ),
            "userCancellations": usage["cancelled"],
            "remainingCancellations": max(
                0,
                rules["max_user_cancellations_per_day"] - usage["cancelled"],
            ),
        },
        "restriction": {
            "reservationBlocked": bool(
                reservation_blocked_until
                and reservation_blocked_until > timestamp
            ),
            "cancellationBlocked": bool(
                cancellation_blocked_until
                and cancellation_blocked_until > timestamp
            ),
            "reservationBlockedUntil": reservation_blocked_until,
            "cancellationBlockedUntil": cancellation_blocked_until,
            "reason": restriction["reason"] if restriction else "",
        },
        "profile": {
            "studentId": user["reservation_student_id"],
            "name": user["reservation_name"],
            "consentedAt": user["reservation_profile_consented_at"],
            "version": user["reservation_profile_version"],
            "updatedAt": user["reservation_profile_updated_at"],
        },
    }


def cancellation_preview(
    db: sqlite3.Connection, reservation: sqlite3.Row
) -> dict[str, Any]:
    if reservation["status"] != "ACTIVE":
        reason = (
            "该预约已由管理员取消"
            if reservation["cancelled_by"] == "ADMIN"
            else "该预约已经取消"
        )
        return {"canCancel": False, "reason": reason}
    rules = reservation_rules(db)
    usage = reservation_usage(db, reservation["user_id"])
    restriction = active_restriction(db, reservation["user_id"])
    timestamp = now_ms()
    if (
        restriction
        and restriction["cancellation_blocked_until"]
        and restriction["cancellation_blocked_until"] > timestamp
    ):
        return {
            "canCancel": False,
            "reason": restriction["reason"] or "当前账号暂时无法继续取消预约",
        }
    start_text = reservation["time_slot"].split("-", 1)[0]
    start_at = datetime.strptime(
        f"{reservation['booking_date']} {start_text}", "%Y-%m-%d %H:%M"
    )
    if datetime.now() + timedelta(minutes=rules["cancel_cutoff_minutes"]) >= start_at:
        return {
            "canCancel": False,
            "reason": f"开场前 {rules['cancel_cutoff_minutes']} 分钟内不可自行取消",
        }
    if usage["cancelled"] >= rules["max_user_cancellations_per_day"]:
        return {
            "canCancel": False,
            "reason": f"今日主动取消已达 {rules['max_user_cancellations_per_day']} 次",
        }
    next_count = usage["cancelled"] + 1
    return {
        "canCancel": True,
        "reason": "",
        "nextCancellationCount": next_count,
        "remainingAfterCancel": max(
            0, rules["max_user_cancellations_per_day"] - next_count
        ),
    }


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
        encoded, content_type = json_response(status, payload)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
        if user["account_status"] in {"DISABLED", "PENDING_DELETION", "DELETED", "SUSPENDED"}:
            raise ApiError(403, "ACCOUNT_UNAVAILABLE", "账号当前不可用，请联系管理员")
        if user["role"] == "ADMIN" and not bool(user["admin_enabled"]):
            raise ApiError(403, "ADMIN_DISABLED", "管理员账号已停用")
        if role and user["role"] != role:
            raise ApiError(403, "FORBIDDEN", "权限不足")
        timestamp = now_ms()
        last_active = user["last_active_at"] or 0
        if user["role"] == "USER" and timestamp - last_active >= 5 * 60 * 1000:
            db.execute("UPDATE users SET last_active_at=? WHERE id=?", (timestamp, user["id"]))
            write_user_event(
                db, user["id"], "PLATFORM", "APP_ACTIVE", "SUCCESS",
                client_version=self.headers.get("X-App-Version", "")[:40] or None,
            )
        return user

    def optional_user(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        if not self.headers.get("Authorization", "").strip():
            return None
        return self.current_user(db)

    def library_reader(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        user = self.optional_user(db)
        if user:
            return user
        row = db.execute(
            "SELECT payload_json FROM remote_flags WHERE key='resource_library'"
        ).fetchone()
        payload = json.loads(row["payload_json"] or "{}") if row else {}
        if not bool(payload.get("allowGuestBrowsing", False)):
            raise ApiError(401, "UNAUTHORIZED", "登录川农账号后可浏览资料库")
        return None

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
            logging.getLogger("mychangli.server").exception("Unhandled API error")
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
        if method == "GET" and path == "/api/v1/auth/reservation-profile":
            return self.auth_reservation_profile(), 200
        if method == "PUT" and path == "/api/v1/auth/reservation-profile":
            return self.auth_reservation_profile_update(), 200
        if method == "DELETE" and path == "/api/v1/auth/reservation-profile":
            return self.auth_reservation_profile_delete(), 200
        if method == "DELETE" and path == "/api/v1/auth/account":
            return self.auth_delete_account(), 200
        if path.startswith("/api/v1/sports/"):
            # A closed module still lets a signed-in user view/cancel an existing
            # reservation. Inventory browsing and every new reservation path are
            # blocked server-side so deep links and old clients cannot bypass it.
            is_existing_reservation_access = (
                method == "GET" and path == "/api/v1/sports/reservations"
            ) or (
                method == "POST"
                and re.fullmatch(r"/api/v1/sports/reservations/[^/]+/cancel", path)
            )
            if not is_existing_reservation_access:
                with connection() as db:
                    ensure_feature_open(db, "sports_reservation", "体育预约")
        if path.startswith("/api/v1/lost-found"):
            with connection() as db:
                ensure_feature_open(db, "lost_found", "失物招领")
        if path.startswith("/api/v1/library"):
            with connection() as db:
                ensure_feature_open(db, "resource_library", "资料库")
        if method == "GET" and path == "/api/v1/sports/context":
            return self.sports_context(), 200
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
        if method == "GET" and path == "/api/v1/library":
            return self.library_list(query), 200
        match = re.fullmatch(r"/api/v1/library/([^/]+)", path)
        if match and method == "GET":
            return self.library_get(match.group(1)), 200
        match = re.fullmatch(r"/api/v1/library/([^/]+)/view", path)
        if match and method == "POST":
            return self.library_view(match.group(1)), 200
        match = re.fullmatch(r"/api/v1/library/([^/]+)/favorite", path)
        if match and method == "POST":
            return self.library_favorite(match.group(1)), 200
        match = re.fullmatch(r"/api/v1/library/([^/]+)/complaints", path)
        if match and method == "POST":
            return self.library_complaint(match.group(1)), 201
        if method == "GET" and path == "/api/v1/public/bootstrap":
            return self.public_bootstrap(), 200
        if method == "GET" and path == "/api/v1/public/website-announcements":
            return self.public_website_announcements(), 200
        if method == "GET" and path == "/api/v1/public/version/android":
            return self.public_version_check(query), 200
        if method == "GET" and path == "/api/v1/public/service-updates":
            return self.public_service_updates(), 200
        if method == "POST" and path == "/api/v1/feedback":
            return self.feedback_create(), 201
        if method == "GET" and path == "/api/v1/feedback":
            return self.feedback_list(), 200
        match = re.fullmatch(r"/api/v1/feedback/([^/]+)", path)
        if method == "GET" and match:
            return self.feedback_get(match.group(1)), 200
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
        nickname = str(data.get("nickname", f"{SCHOOL_SHORT_NAME}同学")).strip()[:30] or f"{SCHOOL_SHORT_NAME}同学"
        digest_phone = phone_digest(phone)
        username = "phone_" + digest_phone[:32]
        digest, salt = password_record(password)
        with connection() as db:
            user_id = str(uuid.uuid4())
            try:
                db.execute(
                    """INSERT INTO users(
                           id,username,password_hash,password_salt,role,account_type,
                           nickname,phone_hash,phone_masked,phone_encrypted,
                           account_status,created_at
                       ) VALUES(?,?,?,?,'USER','PHONE',?,?,?,?,'ACTIVE',?)""",
                    (
                        user_id,
                        username,
                        digest,
                        salt,
                        nickname,
                        digest_phone,
                        phone_mask(phone),
                        encrypt_phone(phone),
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
            db.execute(
                "UPDATE users SET first_login_at=?,last_active_at=? WHERE id=?",
                (now_ms(), now_ms(), user_id),
            )
            write_user_event(db, user_id, "ACCOUNT", "REGISTER_SUCCESS")
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
                if user and not user["phone_encrypted"]:
                    db.execute(
                        "UPDATE users SET phone_encrypted=? WHERE id=?",
                        (encrypt_phone(phone), user["id"]),
                    )
                    user = db.execute(
                        "SELECT * FROM users WHERE id=?", (user["id"],)
                    ).fetchone()
            else:
                username = required_text(data, "username", 48)
                user = db.execute(
                    "SELECT * FROM users WHERE username=?", (username,)
                ).fetchone()
            if not user or not verify_password(password, user["password_hash"], user["password_salt"]):
                if user and user["role"] == "USER":
                    write_user_event(db, user["id"], "ACCOUNT", "LOGIN_FAILURE", "FAILURE", risk_flag="AUTH_FAILURE")
                elif user and user["role"] == "ADMIN":
                    write_audit(db, user["id"], "ADMIN_LOGIN_FAILED", "ADMIN", user["id"], {"result": "FAILURE"})
                raise ApiError(401, "INVALID_CREDENTIALS", "账号或密码错误")
            if user["account_status"] in {"DISABLED", "PENDING_DELETION", "DELETED", "SUSPENDED"}:
                if user["role"] == "USER":
                    write_user_event(db, user["id"], "ACCOUNT", "LOGIN_DENIED", "DENIED", risk_flag="ACCOUNT_STATUS")
                raise ApiError(403, "ACCOUNT_UNAVAILABLE", "账号当前不可用")
            if user["role"] == "ADMIN" and not bool(user["admin_enabled"]):
                write_audit(
                    db,
                    user["id"],
                    "ADMIN_LOGIN_DENIED",
                    "ADMIN",
                    user["id"],
                    {"result": "DENIED", "reason": "ADMIN_DISABLED"},
                )
                raise ApiError(403, "ADMIN_DISABLED", "管理员账号已停用")
            migrated = migrate_device_data(
                db,
                str(data.get("installationId", "")).strip() or None,
                user["id"],
            )
            user = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            timestamp = now_ms()
            db.execute(
                """UPDATE users SET first_login_at=COALESCE(first_login_at,?),
                   last_active_at=? WHERE id=?""",
                (timestamp, timestamp, user["id"]),
            )
            if user["role"] == "ADMIN":
                write_audit(db, user["id"], "ADMIN_LOGIN_SUCCEEDED", "ADMIN", user["id"], {"result": "SUCCESS"})
            else:
                write_user_event(db, user["id"], "ACCOUNT", "LOGIN_SUCCESS")
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
                session = db.execute(
                    "SELECT user_id FROM sessions WHERE token_hash=?", (token_hash,)
                ).fetchone()
                if session:
                    user = db.execute("SELECT role FROM users WHERE id=?", (session["user_id"],)).fetchone()
                    if user and user["role"] == "USER":
                        write_user_event(db, session["user_id"], "ACCOUNT", "LOGOUT")
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

    def auth_reservation_profile(self) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            return serialize_reservation_context(db, user)

    def auth_reservation_profile_update(self) -> dict[str, Any]:
        data = self.body_json()
        with connection() as db:
            user = self.current_user(db)
            rules = reservation_rules(db)
            if rules["reservation_profile_mode"] == "OFF":
                raise ApiError(
                    409,
                    "RESERVATION_PROFILE_DISABLED",
                    "当前无需填写预约资料",
                )
            student_id = normalize_reservation_student_id(
                data.get("studentId"), required=True
            )
            name = normalize_reservation_name(data.get("name"), required=True)
            consented = bool(data.get("consented"))
            if not consented:
                raise ApiError(
                    400,
                    "CONSENT_REQUIRED",
                    "请确认姓名和学号真实、准确并同意预约业务使用范围后再保存",
                )
            timestamp = now_ms()
            db.execute(
                """UPDATE users SET reservation_student_id=?,reservation_name=?,
                   reservation_profile_consented_at=?,
                   reservation_student_id_hash=?,reservation_name_hash=?,
                   reservation_profile_version=?,reservation_profile_updated_at=?
                   WHERE id=?""",
                (
                    student_id,
                    name,
                    timestamp,
                    reservation_pii_digest("student-id", student_id),
                    reservation_pii_digest("real-name", name),
                    RESERVATION_PROFILE_VERSION,
                    timestamp,
                    user["id"],
                ),
            )
            updated = db.execute(
                "SELECT * FROM users WHERE id=?", (user["id"],)
            ).fetchone()
            return serialize_reservation_context(db, updated)

    def auth_reservation_profile_delete(self) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            db.execute(
                """UPDATE users SET reservation_student_id=NULL,reservation_name=NULL,
                   reservation_profile_consented_at=NULL,
                   reservation_student_id_hash=NULL,reservation_name_hash=NULL,
                   reservation_profile_version=0,reservation_profile_updated_at=?
                   WHERE id=?""",
                (now_ms(), user["id"]),
            )
            updated = db.execute(
                "SELECT * FROM users WHERE id=?", (user["id"],)
            ).fetchone()
            return serialize_reservation_context(db, updated)

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
                   phone_hash=NULL,phone_masked=NULL,phone_encrypted=NULL,
                   student_id=NULL,real_name=NULL,reservation_student_id=NULL,
                   reservation_name=NULL,reservation_profile_consented_at=NULL,
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

    def sports_context(self) -> dict[str, Any]:
        with connection() as db:
            return serialize_reservation_context(db, self.current_user(db))

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
            require_account_operation(user)
            reservation_id = str(uuid.uuid4())
            timestamp = now_ms()
            try:
                db.execute("BEGIN IMMEDIATE")
                rules = reservation_rules(db)
                if (
                    rules["reservation_profile_mode"] == "REQUIRED"
                    and (
                        not user["reservation_student_id"]
                        or not user["reservation_name"]
                        or not user["reservation_profile_consented_at"]
                    )
                ):
                    raise ApiError(
                        409,
                        "RESERVATION_PROFILE_REQUIRED",
                        "当前预约规则要求先填写并确认真实姓名和预约识别学号",
                    )
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
                       court_number,price_yuan,reservation_student_id_snapshot,
                       reservation_name_snapshot,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (reservation_id, user["id"], venue_id, booking_date, time_slot, court,
                     venue["price_yuan"], user["reservation_student_id"],
                     user["reservation_name"], timestamp, timestamp),
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
                write_user_event(db, user["id"], "SPORTS", "RESERVATION_CREATED", "SUCCESS", "RESERVATION", reservation_id)
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
        cancel = cancellation_preview(db, row)
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
            "reservationStudentIdSnapshot": row["reservation_student_id_snapshot"],
            "reservationNameSnapshot": row["reservation_name_snapshot"],
            "canCancel": bool(cancel.get("canCancel")),
            "cancelUnavailableReason": cancel.get("reason", ""),
            "nextCancellationCount": cancel.get("nextCancellationCount"),
            "remainingCancellationsAfterCancel": cancel.get("remainingAfterCancel"),
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
            require_account_operation(user)
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
            write_user_event(db, user["id"], "SPORTS", "RESERVATION_CANCELLED", "SUCCESS", "RESERVATION", reservation_id)
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
            require_account_operation(user)
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
            write_user_event(db, user["id"], "LOST_FOUND", "LOST_FOUND_PUBLISHED", "SUCCESS", "LOST_FOUND_POST", post_id)
            return self.serialize_lost(db, row, user["id"])

    def lost_update(self, post_id: str) -> dict[str, Any]:
        data = self.body_json()
        with connection() as db:
            user = self.current_user(db)
            require_account_operation(user)
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
            write_user_event(db, user["id"], "LOST_FOUND", "LOST_FOUND_EDITED", "SUCCESS", "LOST_FOUND_POST", post_id)
            return self.serialize_lost(db, updated, user["id"])

    def lost_delete(self, post_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            require_account_operation(user)
            post = db.execute("SELECT * FROM lost_found_posts WHERE id=?", (post_id,)).fetchone()
            if not post:
                raise ApiError(404, "POST_NOT_FOUND", "失物信息不存在")
            if post["owner_user_id"] != user["id"] and user["role"] != "ADMIN":
                raise ApiError(403, "FORBIDDEN", "只能删除自己的发布")
            db.execute("DELETE FROM lost_found_posts WHERE id=?", (post_id,))
            if post["image_name"]:
                run_safely_delete_upload(post["image_name"])
            write_user_event(db, user["id"], "LOST_FOUND", "LOST_FOUND_DELETED", "SUCCESS", "LOST_FOUND_POST", post_id)
            return {"deleted": True, "id": post_id}

    def lost_favorite(self, post_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            require_account_operation(user)
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
            write_user_event(db, user["id"], "LOST_FOUND", "LOST_FOUND_UNFAVORITED" if existing else "LOST_FOUND_FAVORITED", "SUCCESS", "LOST_FOUND_POST", post_id)
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
            require_account_operation(user)
            post = db.execute("SELECT * FROM lost_found_posts WHERE id=?", (post_id,)).fetchone()
            if not post or post["type"] != "FOUND" or post["status"] == "RETURNED":
                raise ApiError(409, "CLAIM_UNAVAILABLE", "当前不能申请认领")
            if post["owner_user_id"] == user["id"]:
                raise ApiError(409, "CLAIM_OWN_POST", "不能认领自己发布的信息")
            db.execute(
                "INSERT OR IGNORE INTO lost_found_claims VALUES(?,?,?)",
                (user["id"], post_id, now_ms()),
            )
            write_user_event(db, user["id"], "LOST_FOUND", "LOST_FOUND_CLAIMED", "SUCCESS", "LOST_FOUND_POST", post_id)
            return {"postId": post_id, "submitted": True, "message": "认领申请已记录；本阶段不提供站内联系"}

    def lost_report(self, post_id: str) -> dict[str, Any]:
        data = self.body_json()
        with connection() as db:
            user = self.current_user(db)
            require_account_operation(user)
            if not db.execute("SELECT 1 FROM lost_found_posts WHERE id=?", (post_id,)).fetchone():
                raise ApiError(404, "POST_NOT_FOUND", "失物信息不存在")
            report_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO lost_found_reports(id,user_id,post_id,reason,created_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(user_id,post_id)
                   DO UPDATE SET reason=excluded.reason,status='OPEN',created_at=excluded.created_at""",
                (report_id, user["id"], post_id, required_text(data, "reason", 200), now_ms()),
            )
            write_user_event(db, user["id"], "LOST_FOUND", "LOST_FOUND_REPORTED", "SUCCESS", "LOST_FOUND_POST", post_id, risk_flag="CONTENT_REPORT")
            return {"postId": post_id, "recorded": True}

    def serialize_resource(
        self, db: sqlite3.Connection, row: sqlite3.Row, user_id: str | None
    ) -> dict[str, Any]:
        favorite = False
        view = None
        if user_id:
            favorite = db.execute(
                "SELECT 1 FROM resource_favorites WHERE user_id=? AND resource_id=?",
                (user_id, row["id"]),
            ).fetchone() is not None
            view = db.execute(
                """SELECT view_count,last_viewed_at FROM resource_views
                   WHERE user_id=? AND resource_id=?""",
                (user_id, row["id"]),
            ).fetchone()
        return {
            "id": row["id"],
            "title": row["title"],
            "summary": row["summary"],
            "category": row["category"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "audienceScope": row["audience_scope"],
            "publisherLabel": row["publisher_label"],
            "sourceLabel": row["source_label"],
            "usageNote": row["usage_note"],
            "resourceType": row["resource_type"],
            "externalUrl": row["external_url"],
            "coverUrl": row["cover_url"],
            "isFree": bool(row["is_free"]),
            "futurePriceFen": row["future_price_fen"],
            "publishedAt": row["published_at"],
            "isFavorite": favorite,
            "viewCount": view["view_count"] if view else 0,
            "lastViewedAt": view["last_viewed_at"] if view else None,
        }

    def library_list(self, query: dict[str, str]) -> list[dict[str, Any]]:
        with connection() as db:
            user = self.library_reader(db)
            clauses = ["i.publish_status='PUBLISHED'"]
            values: list[Any] = []
            if not user:
                clauses.append("i.visibility='PUBLIC'")
            if query.get("category"):
                clauses.append("i.category=?")
                values.append(query["category"][:80])
            if query.get("q"):
                token = f"%{query['q'][:100]}%"
                clauses.append(
                    """(i.title LIKE ? OR i.summary LIKE ? OR i.category LIKE ?
                        OR i.tags_json LIKE ? OR i.audience_scope LIKE ?)"""
                )
                values.extend([token, token, token, token, token])
            joins = ""
            order = "i.sort_order DESC,i.published_at DESC"
            if query.get("favorites", "").lower() == "true":
                if not user:
                    raise ApiError(401, "UNAUTHORIZED", "登录后才能查看收藏")
                joins += (
                    " JOIN resource_favorites f ON f.resource_id=i.id"
                    " AND f.user_id=?"
                )
                values.insert(0, user["id"])
                order = "f.created_at DESC"
            if query.get("recent", "").lower() == "true":
                if not user:
                    raise ApiError(401, "UNAUTHORIZED", "登录后才能查看最近浏览")
                joins += (
                    " JOIN resource_views rv ON rv.resource_id=i.id"
                    " AND rv.user_id=?"
                )
                values.insert(0, user["id"])
                order = "rv.last_viewed_at DESC"
            rows = db.execute(
                f"""SELECT DISTINCT i.* FROM resource_items i{joins}
                    WHERE {' AND '.join(clauses)}
                    ORDER BY {order} LIMIT 300""",
                values,
            ).fetchall()
            return [
                self.serialize_resource(db, row, user["id"] if user else None)
                for row in rows
            ]

    def library_get(self, resource_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.library_reader(db)
            visibility_clause = "" if user else " AND visibility='PUBLIC'"
            row = db.execute(
                """SELECT * FROM resource_items
                   WHERE id=? AND publish_status='PUBLISHED'"""
                + visibility_clause,
                (resource_id,),
            ).fetchone()
            if not row:
                raise ApiError(404, "RESOURCE_NOT_FOUND", "资料不存在或已下架")
            return self.serialize_resource(db, row, user["id"] if user else None)

    def library_view(self, resource_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            require_account_operation(user)
            if not db.execute(
                """SELECT 1 FROM resource_items
                   WHERE id=? AND publish_status='PUBLISHED'""",
                (resource_id,),
            ).fetchone():
                raise ApiError(404, "RESOURCE_NOT_FOUND", "资料不存在或已下架")
            timestamp = now_ms()
            db.execute(
                """INSERT INTO resource_views(
                       user_id,resource_id,view_count,last_viewed_at
                   ) VALUES(?,?,1,?) ON CONFLICT(user_id,resource_id) DO UPDATE SET
                   view_count=resource_views.view_count+1,
                   last_viewed_at=excluded.last_viewed_at""",
                (user["id"], resource_id, timestamp),
            )
            write_user_event(db, user["id"], "RESOURCE_LIBRARY", "RESOURCE_VIEWED", "SUCCESS", "RESOURCE", resource_id)
            return {"resourceId": resource_id, "viewedAt": timestamp}

    def library_favorite(self, resource_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            require_account_operation(user)
            if not db.execute(
                """SELECT 1 FROM resource_items
                   WHERE id=? AND publish_status='PUBLISHED'""",
                (resource_id,),
            ).fetchone():
                raise ApiError(404, "RESOURCE_NOT_FOUND", "资料不存在或已下架")
            existing = db.execute(
                """SELECT 1 FROM resource_favorites
                   WHERE user_id=? AND resource_id=?""",
                (user["id"], resource_id),
            ).fetchone()
            if existing:
                db.execute(
                    "DELETE FROM resource_favorites WHERE user_id=? AND resource_id=?",
                    (user["id"], resource_id),
                )
            else:
                db.execute(
                    "INSERT INTO resource_favorites VALUES(?,?,?)",
                    (user["id"], resource_id, now_ms()),
                )
            write_user_event(db, user["id"], "RESOURCE_LIBRARY", "RESOURCE_UNFAVORITED" if existing else "RESOURCE_FAVORITED", "SUCCESS", "RESOURCE", resource_id)
            return {"resourceId": resource_id, "isFavorite": not bool(existing)}

    def library_complaint(self, resource_id: str) -> dict[str, Any]:
        data = self.body_json()
        with connection() as db:
            user = self.current_user(db)
            if not db.execute(
                "SELECT 1 FROM resource_items WHERE id=?", (resource_id,)
            ).fetchone():
                raise ApiError(404, "RESOURCE_NOT_FOUND", "资料不存在")
            complaint_id = str(uuid.uuid4())
            timestamp = now_ms()
            db.execute(
                """INSERT INTO resource_complaints(
                       id,user_id,resource_id,reason,detail,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    complaint_id,
                    user["id"],
                    resource_id,
                    required_text(data, "reason", 80),
                    str(data.get("detail", "")).strip()[:1000],
                    timestamp,
                    timestamp,
                ),
            )
            write_user_event(db, user["id"], "RESOURCE_LIBRARY", "RESOURCE_COMPLAINT_SUBMITTED", "SUCCESS", "RESOURCE", resource_id, risk_flag="CONTENT_REPORT")
            return {"id": complaint_id, "status": "OPEN"}

    def public_bootstrap(self) -> dict[str, Any]:
        with connection() as db:
            flags = []
            for row in db.execute("SELECT key FROM remote_flags ORDER BY key"):
                state = feature_state(db, row["key"])
                flags.append(
                    {
                        "key": state["key"],
                        "enabled": state["enabled"],
                        "updatedAt": state["updatedAt"],
                        "payload": {
                            "status": state["status"],
                            "maintenanceNote": state["maintenanceNote"],
                            "expectedRestoreAt": state["expectedRestoreAt"],
                        },
                    }
                )
            announcements = [
                row_dict(row) for row in db.execute(
                    "SELECT id,title,content,published_at AS publishedAt,"
                    "updated_at AS updatedAt FROM announcements "
                    "WHERE enabled=1 ORDER BY published_at DESC, updated_at DESC LIMIT 10"
                )
            ]
            version = row_dict(db.execute(
                "SELECT * FROM app_versions WHERE platform='android' AND status='PUBLISHED'"
            ).fetchone())
            if version:
                # SQLite stores booleans as integers, while Android's Gson model
                # correctly expects a JSON boolean.  A numeric value here makes
                # the whole bootstrap payload fail to deserialize and leaves all
                # remote feature gates stuck on stale cache.
                version["force_update"] = bool(version["force_update"])
            return {"flags": flags, "announcements": announcements, "version": version}

    def public_website_announcements(self) -> dict[str, Any]:
        """Return enabled website announcements, never App announcements."""
        with connection() as db:
            announcements = [
                row_dict(row)
                for row in db.execute(
                    """SELECT id,title,content,
                              published_at AS publishedAt,
                              updated_at AS updatedAt
                         FROM website_announcements
                        WHERE enabled=1
                        ORDER BY published_at DESC, updated_at DESC
                        LIMIT 10"""
                )
            ]
            return {"websiteAnnouncements": announcements}

    def public_version_check(self, query: dict[str, str]) -> dict[str, Any]:
        current_text = str(query.get("currentVersionCode", "0"))
        if not current_text.isdigit():
            raise ApiError(400, "INVALID_VERSION_CODE", "当前版本号无效")
        current = int(current_text)
        with connection() as db:
            row = db.execute(
                """SELECT platform,latest_version_code,minimum_version_code,
                   latest_version_name,update_message,download_url,apk_sha256,
                   apk_size,force_update,published_at,status
                   FROM app_versions WHERE platform='android' AND status='PUBLISHED'"""
            ).fetchone()
            if not row:
                return {"updateAvailable": False, "currentVersionCode": current, "latestVersion": None}
            version = row_dict(row)
            version["force_update"] = bool(version["force_update"])
            return {
                "updateAvailable": version["latest_version_code"] > current,
                "currentVersionCode": current,
                "latestVersion": version,
            }

    def public_service_updates(self) -> list[dict[str, Any]]:
        """Aggregate user-facing service dynamics for the App service page.

        Returns a stable, priority-sorted list of update cards. Only public
        information is included: PUBLISHED Android versions, current feature
        OPEN/MAINTENANCE/CLOSED states and enabled announcements marked visible
        on the service page. No admin, audit, token, download key or internal
        fields are exposed.
        """
        items: list[dict[str, Any]] = []
        with connection() as db:
            version = db.execute(
                """SELECT platform,latest_version_code,minimum_version_code,
                   latest_version_name,update_message,force_update,published_at
                   FROM app_versions WHERE platform='android' AND status='PUBLISHED'"""
            ).fetchone()
            if version:
                force_update = bool(version["force_update"])
                items.append(
                    {
                        "id": f"update-{version['latest_version_code']}",
                        "type": "UPDATE",
                        "priority": 100 if force_update else 70,
                        "title": "发现新版本" if not force_update else "请升级到新版本",
                        "summary": version["update_message"]
                        or f"新版本 {version['latest_version_name']} 已发布",
                        "targetType": "ABOUT_UPDATE",
                        "targetId": None,
                        "versionCode": version["latest_version_code"],
                        "versionName": version["latest_version_name"],
                        "forceUpdate": force_update,
                        "minimumVersionCode": version["minimum_version_code"],
                        "moduleKey": None,
                        "recoverAt": None,
                        "publishedAt": version["published_at"],
                        "dismissible": False,
                    }
                )

            feature_labels = {
                "sports_reservation": "体育预约",
                "lost_found": "失物招领",
                "resource_library": "资料库",
            }
            for key, label in feature_labels.items():
                state = feature_state(db, key)
                status = state["status"]
                if status == "MAINTENANCE":
                    items.append(
                        {
                            "id": f"maintenance-{key}",
                            "type": "MAINTENANCE",
                            "priority": 80,
                            "title": f"{label}维护中",
                            "summary": state["maintenanceNote"] or f"{label}正在维护，请稍后再试",
                            "targetType": "FEATURE",
                            "targetId": key,
                            "versionCode": None,
                            "versionName": None,
                            "forceUpdate": None,
                            "minimumVersionCode": None,
                            "moduleKey": key,
                            "recoverAt": state["expectedRestoreAt"],
                            "publishedAt": state["updatedAt"],
                            "dismissible": True,
                        }
                    )
                elif status == "CLOSED":
                    items.append(
                        {
                            "id": f"closed-{key}",
                            "type": "CLOSED",
                            "priority": 90,
                            "title": f"{label}暂不开放",
                            "summary": state["maintenanceNote"] or f"{label}当前暂不开放",
                            "targetType": "FEATURE",
                            "targetId": key,
                            "versionCode": None,
                            "versionName": None,
                            "forceUpdate": None,
                            "minimumVersionCode": None,
                            "moduleKey": key,
                            "recoverAt": state["expectedRestoreAt"],
                            "publishedAt": state["updatedAt"],
                            "dismissible": True,
                        }
                    )

            announcements = db.execute(
                """SELECT id,title,content,published_at FROM announcements
                   WHERE enabled=1 AND service_visible=1
                   ORDER BY published_at DESC LIMIT 5"""
            ).fetchall()
            for announcement in announcements:
                items.append(
                    {
                        "id": f"announcement-{announcement['id']}",
                        "type": "ANNOUNCEMENT",
                        "priority": 60,
                        "title": announcement["title"],
                        "summary": announcement["content"],
                        "targetType": "ANNOUNCEMENT",
                        "targetId": announcement["id"],
                        "versionCode": None,
                        "versionName": None,
                        "forceUpdate": None,
                        "minimumVersionCode": None,
                        "moduleKey": None,
                        "recoverAt": None,
                        "publishedAt": announcement["published_at"],
                        "dismissible": True,
                    }
                )

        return sorted(
            items,
            key=lambda item: (
                -int(item["priority"]),
                -int(item["publishedAt"] or 0),
                item["id"],
            ),
        )

    def feedback_create(self) -> dict[str, Any]:
        data = self.body_json()
        with connection() as db:
            user = self.current_user(db)
            require_account_operation(user)
            feedback_id = str(uuid.uuid4())
            timestamp = now_ms()
            title = required_text(data, "title", 80)
            category = normalize_feedback_category(data.get("category"))
            content = required_text(data, "content", 2000)
            db.execute(
                """INSERT INTO feedback(
                       id,user_id,category,title,content,app_version,
                       status,workflow_status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,'OPEN','OPEN',?,?)""",
                (
                    feedback_id,
                    user["id"],
                    category,
                    title,
                    content,
                    str(data.get("appVersion", ""))[:40],
                    timestamp,
                    timestamp,
                ),
            )
            db.execute(
                """INSERT INTO feedback_events(
                       id,feedback_id,actor_user_id,actor_role,action,
                       old_status,new_status,public_visible,public_message,created_at
                   ) VALUES(?,?,?,'USER','SUBMITTED',NULL,'OPEN',1,'',?)""",
                (str(uuid.uuid4()), feedback_id, user["id"], timestamp),
            )
            write_user_event(db, user["id"], "FEEDBACK", "FEEDBACK_SUBMITTED", "SUCCESS", "FEEDBACK", feedback_id)
            return {"id": feedback_id, "status": "OPEN", "workflowStatus": "OPEN"}

    def serialize_feedback_for_user(
        self, db: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        events = [
            {
                "action": event["action"],
                "oldStatus": event["old_status"],
                "newStatus": event["new_status"],
                "message": event["public_message"],
                "createdAt": event["created_at"],
            }
            for event in db.execute(
                """SELECT action,old_status,new_status,public_message,created_at
                   FROM feedback_events
                   WHERE feedback_id=? AND public_visible=1
                   ORDER BY created_at ASC""",
                (row["id"],),
            )
        ]
        return {
            "id": row["id"],
            "category": row["category"],
            "title": row["title"],
            "content": row["content"],
            "status": row["workflow_status"],
            "publicReply": row["public_reply"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "events": events,
        }

    def feedback_list(self) -> list[dict[str, Any]]:
        with connection() as db:
            user = self.current_user(db)
            rows = db.execute(
                """SELECT * FROM feedback WHERE user_id=?
                   ORDER BY created_at DESC LIMIT 200""",
                (user["id"],),
            ).fetchall()
            return [self.serialize_feedback_for_user(db, row) for row in rows]

    def feedback_get(self, feedback_id: str) -> dict[str, Any]:
        with connection() as db:
            user = self.current_user(db)
            row = db.execute(
                "SELECT * FROM feedback WHERE id=? AND user_id=?",
                (feedback_id, user["id"]),
            ).fetchone()
            if not row:
                raise ApiError(404, "FEEDBACK_NOT_FOUND", "反馈不存在")
            return self.serialize_feedback_for_user(db, row)

    def admin_route(
        self, method: str, path: str, query: dict[str, str]
    ) -> tuple[Any, int]:
        with connection() as db:
            admin = self.current_user(db, "ADMIN")
            try:
                require_admin_path_access(admin, method, path)
            except ApiError:
                write_audit(
                    db,
                    admin["id"],
                    "ADMIN_ACCESS_DENIED",
                    "ADMIN_ROUTE",
                    path,
                    {"method": method, "role": admin_role(admin)},
                )
                raise

            if method == "GET" and path.startswith("/api/v1/admin/overview/"):
                today = date.today().isoformat()
                if path.endswith("/sports"):
                    return {
                        "workspace": "SPORTS_OPERATOR",
                        "todayReservations": db.execute("SELECT COUNT(*) count FROM reservations WHERE booking_date=?", (today,)).fetchone()["count"],
                        "activeReservations": db.execute("SELECT COUNT(*) count FROM reservations WHERE status='ACTIVE'").fetchone()["count"],
                        "pendingVerification": db.execute("SELECT COUNT(*) count FROM reservations WHERE status='ACTIVE' AND verification_status='NOT_REQUIRED'").fetchone()["count"],
                        "cancelledToday": db.execute("SELECT COUNT(*) count FROM reservations WHERE status='CANCELLED' AND booking_date=?", (today,)).fetchone()["count"],
                        "venues": db.execute("SELECT COUNT(*) count FROM venues").fetchone()["count"],
                    }, 200
                if path.endswith("/lost-found"):
                    return {
                        "workspace": "LOST_FOUND_OPERATOR",
                        "openItems": db.execute("SELECT COUNT(*) count FROM lost_found_posts WHERE status IN ('SEARCHING','WAITING','CLAIMED')").fetchone()["count"],
                        "openReports": db.execute("SELECT COUNT(*) count FROM lost_found_reports WHERE status='OPEN'").fetchone()["count"],
                    }, 200
                if path.endswith("/library"):
                    return {
                        "workspace": "LIBRARY_OPERATOR",
                        "publishedResources": db.execute("SELECT COUNT(*) count FROM resource_items WHERE publish_status='PUBLISHED'").fetchone()["count"],
                        "openComplaints": db.execute("SELECT COUNT(*) count FROM resource_complaints WHERE status IN ('OPEN','PROCESSING')").fetchone()["count"],
                    }, 200
                if path.endswith("/content"):
                    return {
                        "workspace": "CONTENT_OPERATOR",
                        "publishedAnnouncements": db.execute("SELECT COUNT(*) count FROM announcements WHERE enabled=1").fetchone()["count"],
                    }, 200

            if method == "GET" and path == "/api/v1/admin/admins":
                return [
                    {
                        "id": row["id"], "username": row["username"],
                        "nickname": row["nickname"], "adminRole": admin_role(row),
                        "enabled": bool(row["admin_enabled"]), "createdAt": row["created_at"],
                    }
                    for row in db.execute("SELECT * FROM users WHERE role='ADMIN' ORDER BY created_at")
                ], 200

            if method == "POST" and path == "/api/v1/admin/admins":
                data = self.body_json()
                username = required_text(data, "username", 48)
                nickname = required_text(data, "nickname", 40)
                password = required_text(data, "password", 200)
                role_name = str(data.get("adminRole", "")).upper()
                if role_name not in ADMIN_ROLES - {"SUPER_ADMIN"}:
                    raise ApiError(400, "INVALID_ADMIN_ROLE", "运营管理员角色无效")
                if len(password) < 12:
                    raise ApiError(400, "WEAK_PASSWORD", "管理员密码至少 12 位")
                digest, salt = password_record(password)
                admin_id = str(uuid.uuid4())
                try:
                    db.execute(
                        """INSERT INTO users(id,username,password_hash,password_salt,role,
                           account_type,nickname,admin_permissions_json,admin_role,admin_enabled,
                           account_status,created_at)
                           VALUES(?,?,?,?,'ADMIN','ADMIN',?,'[]',?,1,'ACTIVE',?)""",
                        (admin_id, username, digest, salt, nickname, role_name, now_ms()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ApiError(409, "ADMIN_EXISTS", "管理员用户名已存在") from exc
                write_audit(db, admin["id"], "ADMIN_CREATED", "ADMIN", admin_id, {"adminRole": role_name})
                return {"id": admin_id, "username": username, "nickname": nickname, "adminRole": role_name, "enabled": True}, 201

            match_admin = re.fullmatch(r"/api/v1/admin/admins/([^/]+)", path)
            if method == "PATCH" and match_admin:
                data = self.body_json()
                target = db.execute("SELECT * FROM users WHERE id=? AND role='ADMIN'", (match_admin.group(1),)).fetchone()
                if not target:
                    raise ApiError(404, "ADMIN_NOT_FOUND", "管理员不存在")
                if target["id"] == admin["id"]:
                    raise ApiError(409, "CANNOT_EDIT_SELF", "不能编辑当前登录管理员本人")
                role_name = str(data.get("adminRole", admin_role(target))).upper()
                if role_name not in ADMIN_ROLES:
                    raise ApiError(400, "INVALID_ADMIN_ROLE", "管理员角色无效")
                enabled = 1 if bool(data.get("enabled", bool(target["admin_enabled"]))) else 0
                nickname = str(data.get("nickname", target["nickname"] or "")).strip()[:40] or target["nickname"]
                target_role = admin_role(target)
                target_enabled = bool(target["admin_enabled"])
                demoting_or_disabling_super = (
                    target_role == "SUPER_ADMIN"
                    and target_enabled
                    and (role_name != "SUPER_ADMIN" or not enabled)
                )
                if demoting_or_disabling_super:
                    other_active_super = db.execute(
                        """SELECT COUNT(*) AS count FROM users
                           WHERE role='ADMIN' AND admin_role='SUPER_ADMIN'
                           AND admin_enabled=1 AND id!=?""",
                        (target["id"],),
                    ).fetchone()["count"]
                    if other_active_super == 0:
                        raise ApiError(
                            409,
                            "LAST_SUPER_ADMIN_PROTECTED",
                            "系统必须至少保留一个启用的超级管理员，无法停用或降级最后一个超级管理员",
                        )
                db.execute(
                    "UPDATE users SET nickname=?,admin_role=?,admin_enabled=? WHERE id=?",
                    (nickname, role_name, enabled, target["id"]),
                )
                if not enabled:
                    db.execute("DELETE FROM sessions WHERE user_id=?", (target["id"],))
                write_audit(
                    db,
                    admin["id"],
                    "ADMIN_ROLE_UPDATED",
                    "ADMIN",
                    target["id"],
                    {
                        "oldRole": target_role,
                        "newRole": role_name,
                        "enabled": bool(enabled),
                        "nicknameChanged": nickname != target["nickname"],
                    },
                )
                return {
                    "id": target["id"],
                    "nickname": nickname,
                    "adminRole": role_name,
                    "enabled": bool(enabled),
                }, 200

            match_reset = re.fullmatch(r"/api/v1/admin/admins/([^/]+)/reset-password", path)
            if method == "POST" and match_reset:
                password = required_text(self.body_json(), "password", 200)
                if len(password) < 12:
                    raise ApiError(400, "WEAK_PASSWORD", "管理员密码至少 12 位")
                target = db.execute("SELECT id FROM users WHERE id=? AND role='ADMIN'", (match_reset.group(1),)).fetchone()
                if not target:
                    raise ApiError(404, "ADMIN_NOT_FOUND", "管理员不存在")
                digest, salt = password_record(password)
                db.execute("UPDATE users SET password_hash=?,password_salt=? WHERE id=?", (digest, salt, target["id"]))
                db.execute("DELETE FROM sessions WHERE user_id=?", (target["id"],))
                write_audit(db, admin["id"], "ADMIN_PASSWORD_RESET", "ADMIN", target["id"], {"sessionsRevoked": True})
                return {"id": target["id"], "reset": True}, 200

            if method == "GET" and path == "/api/v1/admin/users":
                clauses = ["u.role='USER'"]
                values: list[Any] = []
                append_user_search(clauses, values, query.get("q"))
                if query.get("status", "").upper() in ACCOUNT_STATUSES | {"SUSPENDED"}:
                    clauses.append("u.account_status=?")
                    values.append(query["status"].upper())
                if query.get("accountType", "").upper() in {"PHONE", "DEVICE"}:
                    clauses.append("u.account_type=?")
                    values.append(query["accountType"].upper())
                active_since = str(query.get("activeSince", "")).strip()
                if active_since.isdigit():
                    clauses.append("u.last_active_at>=?")
                    values.append(int(active_since))
                created_since = str(query.get("createdSince", "")).strip()
                if created_since.isdigit():
                    clauses.append("u.created_at>=?")
                    values.append(int(created_since))
                changed_since = str(query.get("statusChangedSince", "")).strip()
                if changed_since.isdigit():
                    clauses.append("u.account_status_changed_at>=?")
                    values.append(int(changed_since))
                if query.get("anomaly", "").lower() == "true":
                    clauses.append("EXISTS (SELECT 1 FROM risk_candidates rc WHERE rc.user_id=u.id AND rc.status IN ('OPEN','WATCHING'))")
                return [
                    full_admin_user_row(row)
                    for row in db.execute(
                        """SELECT u.id,u.account_type,u.nickname,u.phone_masked,u.phone_encrypted,
                                  u.reservation_student_id,u.reservation_name,
                                  u.reservation_profile_consented_at,
                                  u.account_status,u.account_status_reason,
                                  u.account_status_changed_at,u.account_status_expires_at,
                                  u.last_active_at,u.first_login_at,u.created_at,
                                  (SELECT COUNT(*) FROM reservations r
                                   WHERE r.user_id=u.id) AS reservation_count,
                                  (SELECT COUNT(*) FROM reservation_events e
                                   WHERE e.user_id=u.id AND e.action='USER_CANCELLED') AS cancellation_count,
                                  (SELECT COUNT(*) FROM lost_found_posts p WHERE p.owner_user_id=u.id) AS lost_found_count,
                                  (SELECT COUNT(*) FROM lost_found_reports lr WHERE lr.user_id=u.id) AS lost_report_count,
                                  (SELECT COUNT(*) FROM lost_found_claims lc WHERE lc.user_id=u.id) AS lost_claim_count,
                                  (SELECT COALESCE(SUM(rv.view_count),0) FROM resource_views rv WHERE rv.user_id=u.id) AS resource_view_count,
                                  (SELECT COUNT(*) FROM resource_favorites rf WHERE rf.user_id=u.id) AS resource_favorite_count,
                                  (SELECT COUNT(*) FROM resource_complaints rc WHERE rc.user_id=u.id) AS resource_complaint_count,
                                  (SELECT COUNT(*) FROM feedback f WHERE f.user_id=u.id) AS feedback_count,
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

            match = re.fullmatch(r"/api/v1/admin/users/([^/]+)/details", path)
            if method == "GET" and match:
                user = db.execute(
                    """SELECT id,account_type,nickname,phone_masked,phone_encrypted,
                              reservation_student_id,reservation_name,
                              reservation_profile_consented_at,
                              reservation_profile_version,
                              reservation_profile_updated_at,
                              account_status,account_status_reason,
                              account_status_changed_at,account_status_expires_at,
                              last_active_at,first_login_at,created_at
                       FROM users WHERE id=? AND role='USER'""",
                    (match.group(1),),
                ).fetchone()
                if not user:
                    raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
                return {
                    "user": full_admin_user_row(user),
                    "reservations": [
                        mask_admin_reservation_row(row)
                        for row in db.execute(
                            """SELECT r.*,v.name AS venue_name FROM reservations r
                               JOIN venues v ON v.id=r.venue_id
                               WHERE r.user_id=? ORDER BY r.created_at DESC LIMIT 100""",
                            (user["id"],),
                        )
                    ],
                    "lostFound": [
                        row_dict(row)
                        for row in db.execute(
                            """SELECT * FROM lost_found_posts WHERE owner_user_id=?
                               ORDER BY updated_at DESC LIMIT 100""",
                            (user["id"],),
                        )
                    ],
                    "lostReports": [row_dict(row) for row in db.execute(
                        "SELECT * FROM lost_found_reports WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],)
                    )],
                    "lostClaims": [row_dict(row) for row in db.execute(
                        "SELECT * FROM lost_found_claims WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],)
                    )],
                    "feedback": [
                        row_dict(row)
                        for row in db.execute(
                            """SELECT * FROM feedback WHERE user_id=?
                               ORDER BY created_at DESC LIMIT 100""",
                            (user["id"],),
                        )
                    ],
                    "resourceActivity": {
                        "favorites": db.execute(
                            "SELECT COUNT(*) AS count FROM resource_favorites WHERE user_id=?",
                            (user["id"],),
                        ).fetchone()["count"],
                        "views": db.execute(
                            "SELECT COUNT(*) AS count FROM resource_views WHERE user_id=?",
                            (user["id"],),
                        ).fetchone()["count"],
                        "complaints": db.execute(
                            "SELECT COUNT(*) AS count FROM resource_complaints WHERE user_id=?",
                            (user["id"],),
                        ).fetchone()["count"],
                        "unlocks": db.execute(
                            "SELECT COUNT(*) AS count FROM resource_unlocks WHERE user_id=?",
                            (user["id"],),
                        ).fetchone()["count"],
                    },
                    "resourceViews": [row_dict(row) for row in db.execute(
                        """SELECT rv.*,ri.title FROM resource_views rv
                           JOIN resource_items ri ON ri.id=rv.resource_id
                           WHERE rv.user_id=? ORDER BY rv.last_viewed_at DESC LIMIT 100""", (user["id"],)
                    )],
                    "resourceFavorites": [row_dict(row) for row in db.execute(
                        """SELECT rf.*,ri.title FROM resource_favorites rf
                           JOIN resource_items ri ON ri.id=rf.resource_id
                           WHERE rf.user_id=? ORDER BY rf.created_at DESC LIMIT 100""", (user["id"],)
                    )],
                    "resourceComplaints": [row_dict(row) for row in db.execute(
                        "SELECT * FROM resource_complaints WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],)
                    )],
                    "behaviorEvents": [row_dict(row) for row in db.execute(
                        """SELECT id,module,event_type,result,target_type,target_id,
                           client_source,client_version,risk_flag,created_at
                           FROM user_behavior_events WHERE user_id=?
                           ORDER BY created_at DESC LIMIT 100""", (user["id"],)
                    )],
                    "restriction": row_dict(active_restriction(db, user["id"])),
                }, 200

            match = re.fullmatch(r"/api/v1/admin/users/([^/]+)/phone", path)
            if method == "POST" and match:
                require_admin_permission(admin, "PII_VIEW")
                data = self.body_json()
                reason = required_text(data, "reason", 200)
                if len(reason) < 4:
                    raise ApiError(400, "REASON_REQUIRED", "请填写具体查看原因")
                user = db.execute(
                    """SELECT id,phone_encrypted FROM users
                       WHERE id=? AND role='USER'""",
                    (match.group(1),),
                ).fetchone()
                if not user:
                    raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
                phone = decrypt_phone(user["phone_encrypted"])
                write_audit(
                    db,
                    admin["id"],
                    "USER_PHONE_REVEALED",
                    "USER",
                    user["id"],
                    {"reason": reason, "result": "AVAILABLE" if phone else "UNAVAILABLE"},
                )
                if not phone:
                    raise ApiError(
                        409,
                        "PHONE_REVEAL_UNAVAILABLE",
                        "该历史账号尚无可解密号码，请用户重新登录后再试",
                    )
                return {"userId": user["id"], "phone": phone, "expiresInSeconds": 60}, 200

            match = re.fullmatch(
                r"/api/v1/admin/users/([^/]+)/reservation-profile", path
            )
            if method == "POST" and match:
                require_admin_permission(admin, "PII_VIEW")
                data = self.body_json()
                reason = required_text(data, "reason", 200)
                if len(reason) < 4:
                    raise ApiError(400, "REASON_REQUIRED", "请填写具体查看原因")
                user = db.execute(
                    """SELECT id,reservation_student_id,reservation_name
                       FROM users WHERE id=? AND role='USER'""",
                    (match.group(1),),
                ).fetchone()
                if not user:
                    raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
                available = bool(
                    user["reservation_student_id"] and user["reservation_name"]
                )
                write_audit(
                    db,
                    admin["id"],
                    "RESERVATION_PROFILE_REVEALED",
                    "USER",
                    user["id"],
                    {"reason": reason, "result": "AVAILABLE" if available else "UNAVAILABLE"},
                )
                if not available:
                    raise ApiError(409, "PROFILE_REVEAL_UNAVAILABLE", "该用户尚未完整填写预约资料")
                return {
                    "userId": user["id"],
                    "studentId": user["reservation_student_id"],
                    "name": user["reservation_name"],
                    "expiresInSeconds": 60,
                }, 200

            match = re.fullmatch(
                r"/api/v1/admin/reservations/([^/]+)/profile", path
            )
            if method == "POST" and match:
                if admin_role(admin) == "SPORTS_OPERATOR":
                    require_admin_permission(admin, "SPORTS_PII_VIEW")
                else:
                    require_admin_permission(admin, "PII_VIEW")
                data = self.body_json()
                reason = str(data.get("reason", "")).strip()[:200]
                reservation = db.execute(
                    """SELECT r.id,r.user_id,r.reservation_student_id_snapshot,
                              r.reservation_name_snapshot,r.verification_status,
                              u.phone_encrypted,u.nickname
                       FROM reservations r JOIN users u ON u.id=r.user_id
                       WHERE r.id=?""",
                    (match.group(1),),
                ).fetchone()
                if not reservation:
                    raise ApiError(404, "RESERVATION_NOT_FOUND", "预约不存在")
                available = bool(
                    reservation["reservation_student_id_snapshot"]
                    and reservation["reservation_name_snapshot"]
                )
                write_audit(
                    db,
                    admin["id"],
                    "RESERVATION_SNAPSHOT_REVEALED",
                    "RESERVATION",
                    reservation["id"],
                    {"reason": reason, "userId": reservation["user_id"],
                     "result": "AVAILABLE" if available else "UNAVAILABLE"},
                )
                if not available:
                    raise ApiError(409, "PROFILE_REVEAL_UNAVAILABLE", "该预约没有完整资料快照")
                return {
                    "reservationId": reservation["id"],
                    "studentId": reservation["reservation_student_id_snapshot"],
                    "name": reservation["reservation_name_snapshot"],
                    "phone": decrypt_phone(reservation["phone_encrypted"]),
                    "nickname": reservation["nickname"],
                    "verificationStatus": reservation["verification_status"],
                    "expiresInSeconds": 60,
                }, 200

            match = re.fullmatch(r"/api/v1/admin/users/([^/]+)", path)
            if method == "PATCH" and match:
                require_admin_permission(admin, "ACCOUNT_MANAGE")
                data = self.body_json()
                user = db.execute(
                    "SELECT * FROM users WHERE id=? AND role='USER'", (match.group(1),)
                ).fetchone()
                if not user:
                    raise ApiError(404, "USER_NOT_FOUND", "用户不存在")
                account_status = str(
                    data.get("accountStatus", user["account_status"])
                ).upper()
                if account_status not in ACCOUNT_STATUSES:
                    raise ApiError(400, "VALIDATION_ERROR", "账号状态无效")
                if account_status != user["account_status"]:
                    reason = required_text(data, "reason", 300)
                else:
                    reason = str(data.get("reason", user["account_status_reason"] or "")).strip()[:300]
                expires_at = data.get("expiresAt")
                if expires_at is not None:
                    try:
                        expires_at = int(expires_at)
                    except (TypeError, ValueError) as exc:
                        raise ApiError(400, "VALIDATION_ERROR", "状态到期时间无效") from exc
                nickname = str(data.get("nickname", user["nickname"] or f"{SCHOOL_SHORT_NAME}同学")).strip()[:40]
                db.execute(
                    """UPDATE users SET account_status=?,nickname=?,account_status_reason=?,
                       account_status_changed_at=?,account_status_expires_at=? WHERE id=?""",
                    (
                        account_status,
                        nickname or f"{SCHOOL_SHORT_NAME}同学",
                        reason,
                        now_ms() if account_status != user["account_status"] else user["account_status_changed_at"],
                        expires_at,
                        user["id"],
                    ),
                )
                if account_status in {"DISABLED", "PENDING_DELETION", "DELETED"}:
                    db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
                write_audit(
                    db,
                    admin["id"],
                    "USER_STATUS_UPDATED" if account_status != user["account_status"] else "USER_UPDATED",
                    "USER",
                    user["id"],
                    {
                        "oldStatus": user["account_status"],
                        "accountStatus": account_status,
                        "reason": reason[:120],
                    },
                )
                write_user_event(db, user["id"], "ACCOUNT", "ACCOUNT_STATUS_CHANGED", "SUCCESS", "USER", user["id"])
                return mask_admin_user_row(
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
                previous = row_dict(reservation_rules(db))
                profile_mode = str(
                    data.get(
                        "reservationProfileMode",
                        "REQUIRED"
                        if bool(data.get("requireReservationStudentId"))
                        else "OPTIONAL",
                    )
                ).upper()
                if profile_mode not in RESERVATION_PROFILE_MODES:
                    raise ApiError(400, "VALIDATION_ERROR", "预约资料规则无效")
                values = {
                    "max_active_per_day": int(data.get("maxActivePerDay", 0)),
                    "max_user_cancellations_per_day": int(
                        data.get("maxUserCancellationsPerDay", 0)
                    ),
                    "cancel_cutoff_minutes": int(data.get("cancelCutoffMinutes", -1)),
                    "restriction_hours_after_excess_cancel": int(
                        data.get("restrictionHoursAfterExcessCancel", 0)
                    ),
                    "require_reservation_student_id": 1
                    if profile_mode == "REQUIRED"
                    else 0,
                    "reservation_profile_mode": profile_mode,
                }
                reason = str(data.get("reason", "")).strip()[:200] or "常规规则调整"
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
                       restriction_hours_after_excess_cancel=?,
                       require_reservation_student_id=?,reservation_profile_mode=?,
                       updated_at=?
                       WHERE id='default'""",
                    (
                        values["max_active_per_day"],
                        values["max_user_cancellations_per_day"],
                        values["cancel_cutoff_minutes"],
                        values["restriction_hours_after_excess_cancel"],
                        values["require_reservation_student_id"],
                        values["reservation_profile_mode"],
                        now_ms(),
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "RESERVATION_RULES_UPDATED",
                    "RESERVATION_RULES",
                    "default",
                    {
                        "old": previous,
                        "new": values,
                        "reason": reason,
                    },
                )
                return row_dict(reservation_rules(db)), 200

            if method == "GET" and path == "/api/v1/admin/audit-logs":
                require_admin_permission(admin, "ADMIN_AUDIT_VIEW")
                limit, _ = parse_pagination(query, default_limit=100, max_limit=200)
                return [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT a.*,u.nickname AS actor_name
                           FROM admin_audit_logs a
                           LEFT JOIN users u ON u.id=a.actor_user_id
                           ORDER BY a.created_at DESC LIMIT ?""",
                        (limit,),
                    )
                ], 200

            if method == "GET" and path == "/api/v1/admin/user-events":
                require_admin_permission(admin, "USER_EVENTS_VIEW")
                clauses = ["u.role='USER'"]
                values: list[Any] = []
                append_user_search(clauses, values, query.get("user"), "u")
                for parameter, column, allowed in (
                    ("module", "e.module", None),
                    ("eventType", "e.event_type", None),
                    ("result", "e.result", USER_EVENT_RESULTS),
                    ("risk", "e.risk_flag", None),
                ):
                    raw = str(query.get(parameter, "")).strip().upper()
                    if raw and (allowed is None or raw in allowed):
                        clauses.append(f"{column}=?")
                        values.append(raw)
                for parameter, operator in (("from", ">="), ("to", "<=")):
                    raw = str(query.get(parameter, "")).strip()
                    if raw.isdigit():
                        clauses.append(f"e.created_at{operator}?")
                        values.append(int(raw))
                limit, offset = parse_pagination(query, default_limit=50, max_limit=100)
                total = db.execute(
                    """SELECT COUNT(*) AS count FROM user_behavior_events e
                       LEFT JOIN users u ON u.id=e.user_id WHERE """ + " AND ".join(clauses),
                    values,
                ).fetchone()["count"]
                rows = db.execute(
                    """SELECT e.*,u.nickname,u.phone_masked,u.account_status
                       FROM user_behavior_events e
                       LEFT JOIN users u ON u.id=e.user_id WHERE """ + " AND ".join(clauses) +
                    " ORDER BY e.created_at DESC LIMIT ? OFFSET ?",
                    values + [limit, offset],
                ).fetchall()
                return {
                    **paginated_response(
                        [row_dict(row) for row in rows],
                        total,
                        limit,
                        offset,
                    ),
                    "retentionDays": USER_EVENT_RETENTION_DAYS,
                }, 200

            if method == "GET" and path == "/api/v1/admin/risk-candidates":
                require_admin_permission(admin, "ACCOUNT_MANAGE")
                status = str(query.get("status", "")).upper()
                clauses = ["1=1"]
                values: list[Any] = []
                if status in {"OPEN", "WATCHING", "IGNORED", "CLOSED"}:
                    clauses.append("r.status=?")
                    values.append(status)
                return [row_dict(row) for row in db.execute(
                    """SELECT r.*,u.nickname,u.phone_masked,u.account_status
                       FROM risk_candidates r JOIN users u ON u.id=r.user_id
                       WHERE """ + " AND ".join(clauses) + " ORDER BY r.updated_at DESC LIMIT 300",
                    values,
                )], 200

            match = re.fullmatch(r"/api/v1/admin/risk-candidates/([^/]+)", path)
            if method == "PATCH" and match:
                require_admin_permission(admin, "ACCOUNT_MANAGE")
                data = self.body_json()
                candidate = db.execute("SELECT * FROM risk_candidates WHERE id=?", (match.group(1),)).fetchone()
                if not candidate:
                    raise ApiError(404, "RISK_CANDIDATE_NOT_FOUND", "异常候选不存在")
                status = str(data.get("status", candidate["status"])).upper()
                if status not in {"OPEN", "WATCHING", "IGNORED", "CLOSED"}:
                    raise ApiError(400, "VALIDATION_ERROR", "候选状态无效")
                reason = required_text(data, "reason", 300)
                db.execute(
                    "UPDATE risk_candidates SET status=?,admin_note=?,updated_at=? WHERE id=?",
                    (status, reason, now_ms(), candidate["id"]),
                )
                write_audit(db, admin["id"], "RISK_CANDIDATE_REVIEWED", "RISK_CANDIDATE", candidate["id"], {"oldStatus": candidate["status"], "newStatus": status, "reason": reason[:120]})
                return {"id": candidate["id"], "status": status}, 200

            if method == "GET" and path == "/api/v1/admin/overview":
                today = date.today().isoformat()
                day_start = int(datetime.combine(date.today(), datetime.min.time()).timestamp() * 1000)
                now = now_ms()
                def count(sql: str, params: tuple[Any, ...] = ()) -> int:
                    return int(db.execute(sql, params).fetchone()["count"])
                return {
                    "registeredAccounts": count("SELECT COUNT(*) AS count FROM users WHERE role='USER'"),
                    "activeAccounts": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND account_status='ACTIVE'"),
                    "limitedAccounts": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND account_status IN ('LIMITED','SUSPENDED')"),
                    "disabledAccounts": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND account_status='DISABLED'"),
                    "pendingDeletionAccounts": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND account_status='PENDING_DELETION'"),
                    "deletedAccounts": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND account_status='DELETED'"),
                    "todayRegistrations": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND created_at>=?", (day_start,)),
                    "active5m": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND last_active_at>=?", (now - 5 * 60 * 1000,)),
                    "dau": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND last_active_at>=?", (day_start,)),
                    "wau": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND last_active_at>=?", (now - 7 * 86400000,)),
                    "mau": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND last_active_at>=?", (now - 30 * 86400000,)),
                    "todayFirstLogins": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND first_login_at>=?", (day_start,)),
                    "activeReservations": count("SELECT COUNT(*) AS count FROM reservations WHERE status='ACTIVE'"),
                    "todayReservations": count("SELECT COUNT(*) AS count FROM reservations WHERE booking_date=? AND status='ACTIVE'", (today,)),
                    "todayCancelledReservations": count("SELECT COUNT(*) AS count FROM reservations WHERE status='CANCELLED' AND cancelled_at>=?", (day_start,)),
                    "lostFoundOpen": count("SELECT COUNT(*) AS count FROM lost_found_posts WHERE status IN ('SEARCHING','WAITING','CLAIMED')"),
                    "openReports": count("SELECT COUNT(*) AS count FROM lost_found_reports WHERE status='OPEN'"),
                    "openResourceComplaints": count("SELECT COUNT(*) AS count FROM resource_complaints WHERE status IN ('OPEN','PROCESSING')"),
                    "openFeedback": count("SELECT COUNT(*) AS count FROM feedback WHERE workflow_status IN ('OPEN','PROCESSING')"),
                    "enabledAnnouncements": count("SELECT COUNT(*) AS count FROM announcements WHERE enabled=1"),
                    "riskCandidates": count("SELECT COUNT(*) AS count FROM risk_candidates WHERE status='OPEN'"),
                    "loginFailures24h": count("SELECT COUNT(*) AS count FROM user_behavior_events WHERE event_type='LOGIN_FAILURE' AND created_at>=?", (now - 86400000,)),
                    "newLimitedOrDisabled24h": count("SELECT COUNT(*) AS count FROM users WHERE role='USER' AND account_status IN ('LIMITED','DISABLED','SUSPENDED') AND account_status_changed_at>=?", (now - 86400000,)),
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
                if query.get("verificationStatus", "").upper() in {
                    "NOT_REQUIRED", "PENDING", "VERIFIED", "REJECTED"
                }:
                    clauses.append("r.verification_status=?")
                    values.append(query["verificationStatus"].upper())
                if str(query.get("courtNumber", "")).isdigit():
                    clauses.append("r.court_number=?")
                    values.append(int(query["courtNumber"]))
                if query.get("reservationId"):
                    clauses.append("r.id=?")
                    values.append(str(query["reservationId"]).strip()[:80])
                if str(query.get("cancelledSince", "")).isdigit():
                    clauses.append("r.cancelled_at>=?")
                    values.append(int(query["cancelledSince"]))
                if query.get("user"):
                    append_user_search(clauses, values, query.get("user"))
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                page = max(1, int(query.get("page", "1")) if str(query.get("page", "1")).isdigit() else 1)
                raw_page_size = str(query.get("pageSize", "50")).strip()
                if not raw_page_size.isdigit():
                    raise ApiError(422, "VALIDATION_ERROR", "分页参数必须是非负整数")
                page_size = min(max(int(raw_page_size), 10), 100)
                total = db.execute(
                    """SELECT COUNT(*) AS count FROM reservations r
                       JOIN venues v ON v.id=r.venue_id
                       JOIN users u ON u.id=r.user_id""" + where,
                    values,
                ).fetchone()["count"]
                rows = db.execute(
                        """SELECT r.*,v.name AS venue_name,v.unit_label,u.nickname,
                                  u.phone_masked,u.phone_encrypted,u.reservation_student_id,
                                  u.reservation_name,
                                  u.account_type
                           FROM reservations r
                           JOIN venues v ON v.id=r.venue_id
                           JOIN users u ON u.id=r.user_id"""
                        + where
                        + " ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
                        [*values, page_size, (page - 1) * page_size],
                    ).fetchall()
                items = [full_sports_reservation_row(row) for row in rows]
                write_audit(
                    db, admin["id"], "SPORTS_RESERVATIONS_VIEWED", "SPORTS", None,
                    {"resultCount": len(items), "filtered": bool(clauses), "page": page},
                )
                if str(query.get("paged", "")).lower() == "true":
                    return {"items": items, "total": total, "page": page, "pageSize": page_size}, 200
                return items, 200

            match = re.fullmatch(r"/api/v1/admin/reservations/([^/]+)", path)
            if method == "GET" and match:
                row = db.execute(
                    """SELECT r.*,v.name AS venue_name,v.unit_label,u.nickname,
                              u.phone_masked,u.phone_encrypted,u.reservation_student_id,
                              u.reservation_name,u.account_type
                       FROM reservations r
                       JOIN venues v ON v.id=r.venue_id
                       JOIN users u ON u.id=r.user_id WHERE r.id=?""",
                    (match.group(1),),
                ).fetchone()
                if not row:
                    raise ApiError(404, "RESERVATION_NOT_FOUND", "预约不存在")
                history = db.execute(
                    """SELECT r.*,v.name AS venue_name,v.unit_label,u.nickname,
                              u.phone_masked,u.phone_encrypted,u.reservation_student_id,
                              u.reservation_name,u.account_type
                       FROM reservations r
                       JOIN venues v ON v.id=r.venue_id
                       JOIN users u ON u.id=r.user_id
                       WHERE r.user_id=? ORDER BY r.created_at DESC LIMIT 100""",
                    (row["user_id"],),
                ).fetchall()
                write_audit(
                    db, admin["id"], "SPORTS_RESERVATION_DETAILS_VIEWED",
                    "RESERVATION", row["id"], {"historyCount": len(history)},
                )
                return {
                    "reservation": full_sports_reservation_row(row),
                    "sportsHistory": [full_sports_reservation_row(item) for item in history],
                }, 200

            match = re.fullmatch(r"/api/v1/admin/reservations/([^/]+)/verify", path)
            if method == "POST" and match:
                data = self.body_json()
                status = str(data.get("status", "VERIFIED")).upper()
                if status not in {"PENDING", "VERIFIED", "REJECTED", "NOT_REQUIRED"}:
                    raise ApiError(400, "INVALID_VERIFICATION_STATUS", "核验状态无效")
                note = str(data.get("note", "")).strip()[:300]
                current = db.execute(
                    "SELECT id,verification_status FROM reservations WHERE id=?",
                    (match.group(1),),
                ).fetchone()
                if not current:
                    raise ApiError(404, "RESERVATION_NOT_FOUND", "预约不存在")
                db.execute(
                    "UPDATE reservations SET verification_status=?,updated_at=? WHERE id=?",
                    (status, now_ms(), current["id"]),
                )
                write_audit(
                    db, admin["id"], "RESERVATION_PROFILE_VERIFIED",
                    "RESERVATION", current["id"],
                    {"oldStatus": current["verification_status"], "newStatus": status,
                     "noteLength": len(note)},
                )
                return {"id": current["id"], "verificationStatus": status}, 200

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
                if query.get("user"):
                    append_user_search(clauses, values, query.get("user"))
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                rows = db.execute(
                        """SELECT p.*,u.nickname,u.phone_masked,
                           u.phone_encrypted,
                           u.reservation_student_id,u.reservation_name,
                           (SELECT COUNT(*) FROM lost_found_reports rr
                            WHERE rr.post_id=p.id AND rr.status='OPEN') AS open_reports
                           FROM lost_found_posts p
                           JOIN users u ON u.id=p.owner_user_id"""
                        + where
                        + " ORDER BY p.updated_at DESC LIMIT 500",
                        values,
                    ).fetchall()
                return [full_admin_user_row(row) if is_super_admin(admin) else mask_admin_user_row(row) for row in rows], 200

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
                clauses, values = [], []
                if query.get("status"):
                    clauses.append("r.status=?")
                    values.append(str(query["status"]).upper())
                if query.get("user"):
                    append_user_search(clauses, values, query.get("user"))
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                rows = db.execute(
                        """SELECT r.*,p.item_name,p.type,u.nickname,u.phone_masked,
                           u.phone_encrypted
                           FROM lost_found_reports r
                           JOIN lost_found_posts p ON p.id=r.post_id
                           JOIN users u ON u.id=r.user_id""" + where +
                           " ORDER BY r.created_at DESC LIMIT 500", values
                    ).fetchall()
                return [
                    full_admin_user_row(row) if is_super_admin(admin)
                    else mask_admin_user_row(row) for row in rows
                ], 200

            if method == "GET" and path == "/api/v1/admin/lost-found-claims":
                clauses, values = [], []
                if query.get("user"):
                    append_user_search(clauses, values, query.get("user"))
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                rows = db.execute(
                    """SELECT c.*,p.item_name,p.type,u.nickname,u.phone_masked,
                       u.phone_encrypted
                       FROM lost_found_claims c
                       JOIN lost_found_posts p ON p.id=c.post_id
                       JOIN users u ON u.id=c.user_id""" + where +
                    " ORDER BY c.created_at DESC LIMIT 500", values
                ).fetchall()
                return [
                    full_admin_user_row(row) if is_super_admin(admin)
                    else mask_admin_user_row(row) for row in rows
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

            if method == "GET" and path == "/api/v1/admin/resources":
                clauses, values = [], []
                if query.get("status", "").upper() in {
                    "DRAFT",
                    "PUBLISHED",
                    "UNLISTED",
                }:
                    clauses.append("publish_status=?")
                    values.append(query["status"].upper())
                if query.get("category"):
                    clauses.append("category=?")
                    values.append(query["category"][:80])
                if query.get("q"):
                    token = f"%{query['q'][:100]}%"
                    clauses.append(
                        "(title LIKE ? OR summary LIKE ? OR tags_json LIKE ?)"
                    )
                    values.extend([token, token, token])
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                return [
                    {
                        **row_dict(row),
                        "tags": json.loads(row["tags_json"] or "[]"),
                    }
                    for row in db.execute(
                        "SELECT * FROM resource_items"
                        + where
                        + " ORDER BY sort_order DESC,updated_at DESC LIMIT 500",
                        values,
                    )
                ], 200

            if method == "GET" and path == "/api/v1/admin/resource-activity":
                user_id = required_text(query, "user", 80)
                kind = str(query.get("kind", "views")).lower()
                if kind == "views":
                    rows = db.execute(
                        """SELECT rv.user_id,rv.resource_id,rv.view_count,rv.last_viewed_at,
                           ri.title,u.nickname,u.phone_masked,u.phone_encrypted
                           FROM resource_views rv
                           JOIN resource_items ri ON ri.id=rv.resource_id
                           JOIN users u ON u.id=rv.user_id WHERE rv.user_id=?
                           ORDER BY rv.last_viewed_at DESC LIMIT 500""", (user_id,)
                    ).fetchall()
                elif kind == "favorites":
                    rows = db.execute(
                        """SELECT rf.user_id,rf.resource_id,rf.created_at,
                           ri.title,u.nickname,u.phone_masked,u.phone_encrypted
                           FROM resource_favorites rf
                           JOIN resource_items ri ON ri.id=rf.resource_id
                           JOIN users u ON u.id=rf.user_id WHERE rf.user_id=?
                           ORDER BY rf.created_at DESC LIMIT 500""", (user_id,)
                    ).fetchall()
                else:
                    raise ApiError(400, "INVALID_ACTIVITY_KIND", "资料活动类型无效")
                return [
                    full_admin_user_row(row) if is_super_admin(admin)
                    else mask_admin_user_row(row) for row in rows
                ], 200

            if method == "POST" and path == "/api/v1/admin/resources":
                data = self.body_json()
                status = str(data.get("publishStatus", "DRAFT")).upper()
                visibility = str(data.get("visibility", "MEMBERS")).upper()
                if status not in {"DRAFT", "PUBLISHED", "UNLISTED"}:
                    raise ApiError(400, "VALIDATION_ERROR", "发布状态无效")
                if visibility not in {"MEMBERS", "PUBLIC"}:
                    raise ApiError(400, "VALIDATION_ERROR", "可见范围无效")
                tags = data.get("tags", [])
                if isinstance(tags, str):
                    tags = [value.strip() for value in tags.split(",") if value.strip()]
                if not isinstance(tags, list):
                    raise ApiError(400, "VALIDATION_ERROR", "标签格式无效")
                future_price = (
                    int(data["futurePriceFen"])
                    if data.get("futurePriceFen") not in {None, ""}
                    else None
                )
                if future_price is not None and future_price < 0:
                    raise ApiError(400, "VALIDATION_ERROR", "预留价格不能为负数")
                resource_id = str(uuid.uuid4())
                timestamp = now_ms()
                db.execute(
                    """INSERT INTO resource_items(
                           id,title,summary,category,tags_json,audience_scope,
                           publisher_label,source_label,usage_note,resource_type,
                           external_url,cover_url,publish_status,visibility,is_free,
                           future_price_fen,sort_order,published_at,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        resource_id,
                        required_text(data, "title", 160),
                        str(data.get("summary", "")).strip()[:2000],
                        required_text(data, "category", 80),
                        json.dumps([str(value)[:40] for value in tags[:20]], ensure_ascii=False),
                        str(data.get("audienceScope", "")).strip()[:200],
                        str(data.get("publisherLabel", f"{APP_DISPLAY_NAME}资料库")).strip()[:100]
                        or f"{APP_DISPLAY_NAME}资料库",
                        str(data.get("sourceLabel", "")).strip()[:100],
                        str(data.get("usageNote", "")).strip()[:1000],
                        str(data.get("resourceType", "EXTERNAL_LINK")).strip()[:40]
                        or "EXTERNAL_LINK",
                        validate_external_url(data.get("externalUrl")),
                        validate_external_url(data.get("coverUrl"), required=False),
                        status,
                        visibility,
                        1 if bool(data.get("isFree", True)) else 0,
                        future_price,
                        int(data.get("sortOrder", 0)),
                        timestamp if status == "PUBLISHED" else None,
                        timestamp,
                        timestamp,
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "RESOURCE_CREATED",
                    "RESOURCE",
                    resource_id,
                    {"publishStatus": status},
                )
                return {"id": resource_id, "publishStatus": status}, 201

            match = re.fullmatch(r"/api/v1/admin/resources/([^/]+)", path)
            if method == "PATCH" and match:
                current = db.execute(
                    "SELECT * FROM resource_items WHERE id=?", (match.group(1),)
                ).fetchone()
                if not current:
                    raise ApiError(404, "RESOURCE_NOT_FOUND", "资料不存在")
                data = self.body_json()
                status = str(
                    data.get("publishStatus", current["publish_status"])
                ).upper()
                visibility = str(
                    data.get("visibility", current["visibility"])
                ).upper()
                if status not in {"DRAFT", "PUBLISHED", "UNLISTED"}:
                    raise ApiError(400, "VALIDATION_ERROR", "发布状态无效")
                if visibility not in {"MEMBERS", "PUBLIC"}:
                    raise ApiError(400, "VALIDATION_ERROR", "可见范围无效")
                tags = data.get("tags", json.loads(current["tags_json"] or "[]"))
                if isinstance(tags, str):
                    tags = [value.strip() for value in tags.split(",") if value.strip()]
                if not isinstance(tags, list):
                    raise ApiError(400, "VALIDATION_ERROR", "标签格式无效")
                title = str(data.get("title", current["title"])).strip()[:160]
                category = str(
                    data.get("category", current["category"])
                ).strip()[:80]
                if not title or not category:
                    raise ApiError(400, "VALIDATION_ERROR", "资料标题和分类不能为空")
                future_price = (
                    int(data["futurePriceFen"])
                    if data.get("futurePriceFen") not in {None, ""}
                    else None
                )
                if future_price is not None and future_price < 0:
                    raise ApiError(400, "VALIDATION_ERROR", "预留价格不能为负数")
                timestamp = now_ms()
                published_at = current["published_at"]
                if status == "PUBLISHED" and not published_at:
                    published_at = timestamp
                db.execute(
                    """UPDATE resource_items SET title=?,summary=?,category=?,tags_json=?,
                       audience_scope=?,publisher_label=?,source_label=?,usage_note=?,
                       resource_type=?,external_url=?,cover_url=?,publish_status=?,
                       visibility=?,is_free=?,future_price_fen=?,sort_order=?,
                       published_at=?,updated_at=? WHERE id=?""",
                    (
                        title,
                        str(data.get("summary", current["summary"])).strip()[:2000],
                        category,
                        json.dumps([str(value)[:40] for value in tags[:20]], ensure_ascii=False),
                        str(data.get("audienceScope", current["audience_scope"])).strip()[:200],
                        str(data.get("publisherLabel", current["publisher_label"])).strip()[:100],
                        str(data.get("sourceLabel", current["source_label"])).strip()[:100],
                        str(data.get("usageNote", current["usage_note"])).strip()[:1000],
                        str(data.get("resourceType", current["resource_type"])).strip()[:40],
                        validate_external_url(
                            data.get("externalUrl", current["external_url"])
                        ),
                        validate_external_url(
                            data.get("coverUrl", current["cover_url"]), required=False
                        ),
                        status,
                        visibility,
                        1 if bool(data.get("isFree", bool(current["is_free"]))) else 0,
                        future_price,
                        int(data.get("sortOrder", current["sort_order"])),
                        published_at,
                        timestamp,
                        current["id"],
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "RESOURCE_UPDATED",
                    "RESOURCE",
                    current["id"],
                    {
                        "oldStatus": current["publish_status"],
                        "newStatus": status,
                    },
                )
                return {"id": current["id"], "publishStatus": status}, 200

            if method == "DELETE" and match:
                changed = db.execute(
                    "DELETE FROM resource_items WHERE id=?", (match.group(1),)
                ).rowcount
                if not changed:
                    raise ApiError(404, "RESOURCE_NOT_FOUND", "资料不存在")
                write_audit(
                    db,
                    admin["id"],
                    "RESOURCE_DELETED",
                    "RESOURCE",
                    match.group(1),
                )
                return {"id": match.group(1), "deleted": True}, 200

            if method == "GET" and path == "/api/v1/admin/resource-complaints":
                clauses, values = [], []
                if query.get("status", "").upper() in {
                    "OPEN",
                    "PROCESSING",
                    "RESOLVED",
                    "CLOSED",
                }:
                    clauses.append("c.status=?")
                    values.append(query["status"].upper())
                append_user_search(clauses, values, query.get("user"))
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                rows = db.execute(
                        """SELECT c.*,i.title,u.nickname,u.phone_masked,
                                  u.phone_encrypted,u.reservation_student_id,
                                  u.reservation_name
                           FROM resource_complaints c
                           JOIN resource_items i ON i.id=c.resource_id
                           JOIN users u ON u.id=c.user_id"""
                        + where
                        + " ORDER BY c.created_at DESC LIMIT 500",
                        values,
                    ).fetchall()
                return [
                    full_admin_user_row(row) if is_super_admin(admin)
                    else mask_admin_user_row(row) for row in rows
                ], 200

            match = re.fullmatch(
                r"/api/v1/admin/resource-complaints/([^/]+)", path
            )
            if method == "PATCH" and match:
                data = self.body_json()
                status = str(data.get("status", "")).upper()
                if status not in {"OPEN", "PROCESSING", "RESOLVED", "CLOSED"}:
                    raise ApiError(400, "VALIDATION_ERROR", "投诉状态无效")
                note = str(data.get("resolutionNote", "")).strip()[:1000]
                changed = db.execute(
                    """UPDATE resource_complaints SET status=?,resolution_note=?,
                       updated_at=? WHERE id=?""",
                    (status, note, now_ms(), match.group(1)),
                ).rowcount
                if not changed:
                    raise ApiError(404, "COMPLAINT_NOT_FOUND", "投诉不存在")
                write_audit(
                    db,
                    admin["id"],
                    "RESOURCE_COMPLAINT_UPDATED",
                    "RESOURCE_COMPLAINT",
                    match.group(1),
                    {"status": status},
                )
                return {"id": match.group(1), "status": status}, 200

            if method == "GET" and path == "/api/v1/admin/announcements":
                return [
                    {
                        **row_dict(row),
                        "enabled": bool(row["enabled"]),
                        "serviceVisible": bool(row["service_visible"]),
                    }
                    for row in db.execute(
                        "SELECT * FROM announcements ORDER BY published_at DESC"
                    )
                ], 200

            if method == "GET" and path == "/api/v1/admin/website-announcements":
                return [
                    {
                        **row_dict(row),
                        "enabled": bool(row["enabled"]),
                    }
                    for row in db.execute(
                        "SELECT * FROM website_announcements ORDER BY published_at DESC, updated_at DESC"
                    )
                ], 200

            if method == "POST" and path == "/api/v1/admin/website-announcements":
                data = self.body_json()
                announcement_id = str(uuid.uuid4())
                timestamp = now_ms()
                enabled = 1 if bool(data.get("enabled", True)) else 0
                db.execute(
                    """INSERT INTO website_announcements(
                           id,title,content,enabled,published_at,updated_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        announcement_id,
                        required_text(data, "title", 100),
                        required_text(data, "content", 4000),
                        enabled,
                        timestamp,
                        timestamp,
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "WEBSITE_ANNOUNCEMENT_CREATED",
                    "WEBSITE_ANNOUNCEMENT",
                    announcement_id,
                    {"enabled": bool(enabled)},
                )
                return {"id": announcement_id, "enabled": bool(enabled)}, 201

            website_match = re.fullmatch(
                r"/api/v1/admin/website-announcements/([^/]+)", path
            )
            if method == "PATCH" and website_match:
                data = self.body_json()
                current = db.execute(
                    "SELECT * FROM website_announcements WHERE id=?",
                    (website_match.group(1),),
                ).fetchone()
                if not current:
                    raise ApiError(404, "WEBSITE_ANNOUNCEMENT_NOT_FOUND", "网站公告不存在")
                title = str(data.get("title", current["title"])).strip()[:100]
                content = str(data.get("content", current["content"])).strip()[:4000]
                enabled = 1 if bool(data.get("enabled", bool(current["enabled"]))) else 0
                if not title or not content:
                    raise ApiError(400, "VALIDATION_ERROR", "网站公告标题和内容不能为空")
                updated_at = now_ms()
                db.execute(
                    """UPDATE website_announcements
                          SET title=?,content=?,enabled=?,updated_at=?
                        WHERE id=?""",
                    (title, content, enabled, updated_at, current["id"]),
                )
                write_audit(
                    db,
                    admin["id"],
                    "WEBSITE_ANNOUNCEMENT_UPDATED",
                    "WEBSITE_ANNOUNCEMENT",
                    current["id"],
                    {"enabled": bool(enabled)},
                )
                return {
                    "id": current["id"],
                    "enabled": bool(enabled),
                    "updatedAt": updated_at,
                }, 200

            if method == "DELETE" and website_match:
                changed = db.execute(
                    "DELETE FROM website_announcements WHERE id=?",
                    (website_match.group(1),),
                ).rowcount
                if not changed:
                    raise ApiError(404, "WEBSITE_ANNOUNCEMENT_NOT_FOUND", "网站公告不存在")
                write_audit(
                    db,
                    admin["id"],
                    "WEBSITE_ANNOUNCEMENT_DELETED",
                    "WEBSITE_ANNOUNCEMENT",
                    website_match.group(1),
                )
                return {"id": website_match.group(1), "deleted": True}, 200

            if method == "POST" and path == "/api/v1/admin/announcements":
                data = self.body_json()
                announcement_id = str(uuid.uuid4())
                timestamp = now_ms()
                service_visible = 1 if bool(data.get("serviceVisible", True)) else 0
                db.execute(
                    """INSERT INTO announcements(
                           id,title,content,enabled,service_visible,published_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        announcement_id,
                        required_text(data, "title", 100),
                        required_text(data, "content", 4000),
                        1 if bool(data.get("enabled", True)) else 0,
                        service_visible,
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
                    {
                        "enabled": bool(data.get("enabled", True)),
                        "serviceVisible": bool(service_visible),
                    },
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
                service_visible = (
                    1
                    if bool(data.get("serviceVisible", bool(current["service_visible"])))
                    else 0
                )
                if not title or not content:
                    raise ApiError(400, "VALIDATION_ERROR", "公告标题和内容不能为空")
                db.execute(
                    """UPDATE announcements SET title=?,content=?,enabled=?,
                       service_visible=?,updated_at=?
                       WHERE id=?""",
                    (title, content, enabled, service_visible, now_ms(), current["id"]),
                )
                write_audit(
                    db,
                    admin["id"],
                    "ANNOUNCEMENT_UPDATED",
                    "ANNOUNCEMENT",
                    current["id"],
                    {"enabled": bool(enabled), "serviceVisible": bool(service_visible)},
                )
                return {
                    "id": current["id"],
                    "enabled": bool(enabled),
                    "serviceVisible": bool(service_visible),
                }, 200

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
                            "updatedBy": row["updated_by"],
                            "changeNote": row["change_note"],
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
                require_admin_permission(admin, "*")
                feature_key = match.group(1)
                if feature_key not in CONTROLLED_FEATURES:
                    raise ApiError(404, "FEATURE_NOT_FOUND", "功能开关不存在")
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
                change_note = str(data.get("changeNote", "")).strip()[:300]
                previous = feature_state(db, feature_key)
                timestamp = now_ms()
                enabled = status != "CLOSED"
                db.execute(
                    """INSERT INTO remote_flags(
                           key,enabled,payload_json,updated_by,change_note,updated_at
                       ) VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                       enabled=excluded.enabled,payload_json=excluded.payload_json,
                       updated_by=excluded.updated_by,change_note=excluded.change_note,
                       updated_at=excluded.updated_at""",
                    (
                        feature_key,
                        1 if enabled else 0,
                        json.dumps(payload, ensure_ascii=False),
                        admin["id"],
                        change_note,
                        timestamp,
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "FEATURE_FLAG_UPDATED",
                    "REMOTE_FLAG",
                    feature_key,
                    {
                        "enabled": enabled,
                        "status": str(payload.get("status", "")),
                        "previousStatus": previous["status"],
                        "changeNoteLength": len(change_note),
                    },
                )
                return {
                    "key": feature_key,
                    "enabled": enabled,
                    "payload": payload,
                    "updatedAt": timestamp,
                    "updatedBy": admin["id"],
                    "changeNote": change_note,
                }, 200

            if method == "PUT" and path == "/api/v1/admin/version/android":
                data = self.body_json()
                latest = int(data.get("latestVersionCode", 0))
                minimum = int(data.get("minimumVersionCode", 0))
                if latest < 1 or minimum < 1 or minimum > latest:
                    raise ApiError(400, "VALIDATION_ERROR", "版本号配置无效")
                status = str(data.get("status", "DRAFT")).upper()
                if status not in {"DRAFT", "PUBLISHED", "DISABLED"}:
                    raise ApiError(400, "VALIDATION_ERROR", "版本发布状态无效")
                download_url = str(data.get("downloadUrl", "")).strip()[:500] or None
                if download_url and not re.fullmatch(r"https://[^\s]+|http://127\.0\.0\.1(?::\d+)?/[^\s]+", download_url):
                    raise ApiError(400, "UNSAFE_DOWNLOAD_URL", "下载地址必须为 HTTPS、本机地址或留空")
                sha256 = str(data.get("apkSha256", "")).strip().lower()
                if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
                    raise ApiError(400, "INVALID_APK_SHA256", "APK SHA-256 格式无效")
                apk_size = data.get("apkSize")
                apk_size = int(apk_size) if apk_size not in (None, "") else None
                if apk_size is not None and apk_size <= 0:
                    raise ApiError(400, "INVALID_APK_SIZE", "APK 大小必须大于 0")
                timestamp = now_ms()
                db.execute(
                    """INSERT INTO app_versions(
                           platform,latest_version_code,minimum_version_code,
                           latest_version_name,update_message,download_url,apk_sha256,
                           apk_size,force_update,status,published_at,created_at,
                           created_by,updated_by,updated_at
                       ) VALUES('android',?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(platform) DO UPDATE SET
                       latest_version_code=excluded.latest_version_code,
                       minimum_version_code=excluded.minimum_version_code,
                       latest_version_name=excluded.latest_version_name,
                       update_message=excluded.update_message,
                       download_url=excluded.download_url,
                       apk_sha256=excluded.apk_sha256,
                       apk_size=excluded.apk_size,
                       force_update=excluded.force_update,
                       status=excluded.status,
                       published_at=CASE WHEN excluded.status='PUBLISHED'
                           THEN COALESCE(app_versions.published_at,excluded.updated_at)
                           ELSE app_versions.published_at END,
                       updated_by=excluded.updated_by,
                       updated_at=excluded.updated_at""",
                    (
                        latest,
                        minimum,
                        required_text(data, "latestVersionName", 40),
                        str(data.get("updateMessage", ""))[:1000],
                        download_url,
                        sha256 or None,
                        apk_size,
                        1 if bool(data.get("forceUpdate", False)) else 0,
                        status,
                        timestamp if status == "PUBLISHED" else None,
                        timestamp,
                        admin["id"],
                        admin["id"],
                        timestamp,
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "APP_VERSION_UPDATED",
                    "APP_VERSION",
                    "android",
                    {"latestVersionCode": latest, "minimumVersionCode": minimum, "status": status},
                )
                return {
                    "platform": "android",
                    "latestVersionCode": latest,
                    "minimumVersionCode": minimum,
                    "status": status,
                }, 200

            if method == "GET" and path == "/api/v1/admin/feedback":
                clauses, values = [], []
                if query.get("status", "").upper() in FEEDBACK_STATUSES:
                    clauses.append("f.workflow_status=?")
                    values.append(query["status"].upper())
                if query.get("category", "").upper() in FEEDBACK_CATEGORIES:
                    clauses.append("f.category=?")
                    values.append(query["category"].upper())
                if query.get("q"):
                    keyword = str(query["q"]).strip()[:100]
                    token = f"%{keyword}%"
                    clauses.append(
                        "(f.title LIKE ? OR f.content LIKE ? OR f.id LIKE ?)"
                    )
                    values.extend([token, token, token])
                if query.get("user"):
                    append_user_search(clauses, values, query.get("user"))
                if query.get("from"):
                    start = date.fromisoformat(parse_iso_date(query["from"]))
                    clauses.append("f.created_at>=?")
                    values.append(
                        int(datetime.combine(start, datetime.min.time()).timestamp() * 1000)
                    )
                if query.get("to"):
                    end = date.fromisoformat(parse_iso_date(query["to"]))
                    clauses.append("f.created_at<?")
                    values.append(
                        int(
                            datetime.combine(
                                end + timedelta(days=1), datetime.min.time()
                            ).timestamp()
                            * 1000
                        )
                    )
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                order = "ASC" if query.get("sort", "").upper() == "ASC" else "DESC"
                return [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT f.id,f.user_id,f.category,f.title,f.content,
                                  f.app_version,f.workflow_status,
                                  f.public_reply,f.created_at,f.updated_at,
                                  u.nickname,u.phone_masked
                           FROM feedback f
                           LEFT JOIN users u ON u.id=f.user_id"""
                        + where
                        + f" ORDER BY f.created_at {order} LIMIT 500",
                        values,
                    )
                ], 200

            match = re.fullmatch(r"/api/v1/admin/feedback/([^/]+)", path)
            if method == "GET" and match:
                feedback = db.execute(
                    """SELECT f.*,u.nickname,u.phone_masked
                       FROM feedback f LEFT JOIN users u ON u.id=f.user_id
                       WHERE f.id=?""",
                    (match.group(1),),
                ).fetchone()
                if not feedback:
                    raise ApiError(404, "FEEDBACK_NOT_FOUND", "反馈不存在")
                events = [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT e.id,e.actor_user_id,e.actor_role,e.action,
                                  e.old_status,e.new_status,e.public_visible,
                                  e.public_message,e.created_at,
                                  u.nickname AS actor_name
                           FROM feedback_events e
                           LEFT JOIN users u ON u.id=e.actor_user_id
                           WHERE e.feedback_id=? ORDER BY e.created_at ASC""",
                        (match.group(1),),
                    )
                ]
                audits = [
                    row_dict(row)
                    for row in db.execute(
                        """SELECT a.id,a.actor_user_id,a.action,a.details_json,
                                  a.created_at,u.nickname AS actor_name
                           FROM admin_audit_logs a
                           LEFT JOIN users u ON u.id=a.actor_user_id
                           WHERE a.entity_type='FEEDBACK' AND a.entity_id=?
                           ORDER BY a.created_at ASC""",
                        (match.group(1),),
                    )
                ]
                return {
                    "feedback": row_dict(feedback),
                    "events": events,
                    "audit": audits,
                }, 200

            if method == "PATCH" and match:
                data = self.body_json()
                status = str(data.get("status", "")).upper()
                if status not in FEEDBACK_STATUSES:
                    raise ApiError(400, "VALIDATION_ERROR", "反馈状态无效")
                current = db.execute(
                    "SELECT * FROM feedback WHERE id=?", (match.group(1),)
                ).fetchone()
                if not current:
                    raise ApiError(404, "FEEDBACK_NOT_FOUND", "反馈不存在")
                public_reply = str(
                    data.get("publicReply", current["public_reply"] or "")
                ).strip()
                internal_note = str(
                    data.get("internalNote", current["internal_note"] or "")
                ).strip()
                if len(public_reply) > 2000 or len(internal_note) > 2000:
                    raise ApiError(400, "VALIDATION_ERROR", "处理说明或内部备注不能超过 2000 字")
                if status in {"RESOLVED", "CLOSED"} and not public_reply:
                    raise ApiError(400, "PUBLIC_REPLY_REQUIRED", "已解决或已关闭时请填写对用户可见的处理说明")
                timestamp = now_ms()
                db.execute(
                    """UPDATE feedback SET status=?,workflow_status=?,
                       public_reply=?,internal_note=?,handled_by=?,resolved_at=?,
                       updated_at=? WHERE id=?""",
                    (
                        "CLOSED" if status in {"RESOLVED", "CLOSED"} else status,
                        status,
                        public_reply,
                        internal_note,
                        admin["id"],
                        timestamp if status in {"RESOLVED", "CLOSED"} else None,
                        timestamp,
                        current["id"],
                    ),
                )
                public_changed = public_reply != (current["public_reply"] or "")
                note_changed = internal_note != (current["internal_note"] or "")
                db.execute(
                    """INSERT INTO feedback_events(
                           id,feedback_id,actor_user_id,actor_role,action,
                           old_status,new_status,public_visible,public_message,created_at
                       ) VALUES(?,?,?,'ADMIN','UPDATED',?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        current["id"],
                        admin["id"],
                        current["workflow_status"],
                        status,
                        1 if public_changed else 0,
                        public_reply if public_changed else "",
                        timestamp,
                    ),
                )
                write_audit(
                    db,
                    admin["id"],
                    "FEEDBACK_UPDATED",
                    "FEEDBACK",
                    current["id"],
                    {
                        "oldStatus": current["workflow_status"],
                        "newStatus": status,
                        "publicReplyChanged": public_changed,
                        "publicReplyLength": len(public_reply),
                        "internalNoteChanged": note_changed,
                        "internalNoteLength": len(internal_note),
                    },
                )
                return {
                    "id": current["id"],
                    "status": status,
                    "updatedAt": timestamp,
                }, 200

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
