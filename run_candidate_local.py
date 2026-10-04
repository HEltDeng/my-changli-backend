"""Loopback-only candidate environment. Never points at or copies a production database."""
import argparse
import json
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8792)
    args = parser.parse_args()
    state = args.state_dir.resolve()
    repo = Path(__file__).resolve().parent
    if state == repo or repo in state.parents or not 1024 <= args.port <= 65535:
        parser.error("Use a dedicated output directory outside the repository and an unprivileged port")
    marker = state / "CANDIDATE_ONLY"
    if state.exists() and any(state.iterdir()) and not marker.exists():
        parser.error("Refusing a nonempty directory without the candidate marker")
    state.mkdir(parents=True, exist_ok=True)
    marker.write_text("Isolated preview data. No production import.\n", encoding="utf-8")
    credentials = state / "local-test-access.json"
    if not credentials.exists():
        fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"username": "candidate-admin", "password": secrets.token_urlsafe(24)}, stream)
    account = json.loads(credentials.read_text(encoding="utf-8"))
    os.environ.update(MY_CHANGLI_ENV="test", MY_CHANGLI_HOST="127.0.0.1",
        MY_CHANGLI_PORT=str(args.port), MY_CHANGLI_CANDIDATE_ONLY="1",
        MY_CHANGLI_DB_PATH=str(state / "candidate.db"), MY_CHANGLI_UPLOAD_PATH=str(state / "uploads"),
        MY_CHANGLI_LOG_PATH=str(state / "candidate.log"), MY_CHANGLI_BACKUP_PATH=str(state / "backups"),
        MY_CHANGLI_ADMIN_ORIGIN=f"http://127.0.0.1:{args.port}", MY_CHANGLI_ALLOWED_HOSTS="127.0.0.1,localhost",
        MY_CHANGLI_ADMIN_USERNAME=account["username"], MY_CHANGLI_ADMIN_PASSWORD=account["password"])
    import server
    server.initialize_database()
    server.bootstrap_admin()
    with server.connection() as db:
        db.execute("UPDATE app_update_policies SET update_enabled=0,force_update_enabled=0")
    print(f"Candidate only: http://127.0.0.1:{args.port}/admin/; access file: {credentials}", flush=True)
    server.ThreadingHTTPServer(("127.0.0.1", args.port), server.ApiHandler).serve_forever()


if __name__ == "__main__":
    main()
