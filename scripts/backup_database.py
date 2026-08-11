from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_config import RUNTIME  # noqa: E402
from scripts.database_tools import backup_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a consistent My Changli SQLite backup")
    parser.add_argument("--database", type=Path, default=RUNTIME.database_path)
    parser.add_argument("--destination", type=Path, default=RUNTIME.backup_path)
    parser.add_argument("--retention-days", type=int, default=14)
    args = parser.parse_args()
    try:
        result = backup_database(
            args.database, args.destination, retention_days=args.retention_days
        )
        print(json.dumps({
            "ok": True,
            "filename": result["filename"],
            "bytes": result["bytes"],
            "sha256": result["sha256"],
            "integrity": result["integrity"],
            "removedByRetention": result["removed"],
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
