"""Local administration commands for My Changli.

Passwords are read with getpass and never echoed or written to command history.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid

import server


def read_password(confirm: bool = True) -> str:
    password = getpass.getpass("管理员密码（至少 12 位）: ")
    if len(password) < 12:
        raise ValueError("管理员密码至少需要 12 位")
    if confirm and password != getpass.getpass("再次输入管理员密码: "):
        raise ValueError("两次输入的密码不一致")
    return password


def create_admin(username: str) -> None:
    username = username.strip()
    if not username or len(username) > 48:
        raise ValueError("管理员用户名长度应为 1 至 48 个字符")
    password = read_password()
    digest, salt = server.password_record(password)
    with server.connection() as db:
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError("该用户名已经存在")
        db.execute(
            """INSERT INTO users(
                   id,username,password_hash,password_salt,role,account_type,nickname,
                   admin_permissions_json,admin_role,admin_enabled,created_at
               ) VALUES(?,?,?,?, 'ADMIN','ADMIN','本地管理员','["*"]','SUPER_ADMIN',1,?)""",
            (str(uuid.uuid4()), username, digest, salt, server.now_ms()),
        )
    print("管理员账号创建成功。")


def reset_password(username: str) -> None:
    password = read_password()
    digest, salt = server.password_record(password)
    with server.connection() as db:
        changed = db.execute(
            """UPDATE users SET password_hash=?,password_salt=?
               WHERE username=? AND role='ADMIN'""",
            (digest, salt, username.strip()),
        ).rowcount
    if not changed:
        raise ValueError("未找到该管理员账号")
    print("管理员密码已更新，旧登录会话仍可在后台退出。")


def has_admin() -> int:
    with server.connection() as db:
        exists = db.execute(
            "SELECT 1 FROM users WHERE role='ADMIN' LIMIT 1"
        ).fetchone()
    return 0 if exists else 1


def main() -> int:
    server.initialize_database()
    parser = argparse.ArgumentParser(description="川农本地后台账号管理")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="创建管理员")
    create.add_argument("username")
    reset = commands.add_parser("reset-password", help="重置管理员密码")
    reset.add_argument("username")
    commands.add_parser("has-admin", help="检查是否已有管理员")
    args = parser.parse_args()
    try:
        if args.command == "create-admin":
            create_admin(args.username)
        elif args.command == "reset-password":
            reset_password(args.username)
        else:
            return has_admin()
        return 0
    except (ValueError, KeyboardInterrupt) as error:
        print(f"操作未完成：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
