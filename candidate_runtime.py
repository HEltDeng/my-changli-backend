"""Explicit isolated acceptance entrypoint; never use with production data."""
import os
from pathlib import Path
from runtime_config import RUNTIME


def validate_boundary():
    root = Path("/var/lib/mychangli-candidate")
    if not RUNTIME.is_production or os.environ.get("MY_CHANGLI_CANDIDATE_ONLY") != "1":
        raise RuntimeError("Candidate requires hardened transport and candidate-only collection")
    if RUNTIME.port != 8793 or RUNTIME.host != "127.0.0.1":
        raise RuntimeError("Candidate must use dedicated loopback port")
    for path in (RUNTIME.database_path, RUNTIME.upload_path, RUNTIME.log_path, RUNTIME.backup_path):
        if root not in path.resolve().parents:
            raise RuntimeError("Candidate persistent path escaped isolated state directory")
    if RUNTIME.admin_origin != "https://candidate.mychangliapp.cn" or RUNTIME.allowed_hosts != ("candidate.mychangliapp.cn",):
        raise RuntimeError("Candidate HTTPS origin mismatch")


def main():
    validate_boundary()
    import server
    import production
    server.initialize_database()
    # Random bootstrap credentials are supplied only in the protected candidate env file.
    server.bootstrap_admin()
    with server.connection() as db:
        db.execute("UPDATE users SET nickname='隔离测试管理员' WHERE role='ADMIN'")
        db.execute("UPDATE app_update_policies SET update_enabled=0,force_update_enabled=0")
    production.main()


if __name__ == "__main__":
    main()
