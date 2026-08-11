from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_config import RUNTIME  # noqa: E402
from scripts.database_tools import restore_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline restore of a My Changli SQLite backup")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--database", type=Path, default=RUNTIME.database_path)
    parser.add_argument("--safety-backup-directory", type=Path, default=RUNTIME.backup_path)
    parser.add_argument("--confirm-offline-restore", action="store_true")
    args = parser.parse_args()
    if not args.confirm_offline_restore:
        print("Refusing restore without --confirm-offline-restore", file=sys.stderr)
        return 2
    try:
        result = restore_database(
            args.backup, args.database, args.safety_backup_directory
        )
        print(json.dumps({
            "ok": True,
            "restored": result["restored"],
            "integrity": result["integrity"],
            "safetyBackup": result["safetyBackup"].name if result["safetyBackup"] else None,
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
