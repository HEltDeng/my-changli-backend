from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.database_tools import verify_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a My Changli SQLite backup")
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    report = verify_backup(args.backup)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
