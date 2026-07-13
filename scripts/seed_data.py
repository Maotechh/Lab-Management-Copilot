# ruff: noqa: E402
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumable_assistant.database import connect, init_db
from consumable_assistant.importer import import_all_sources
from consumable_assistant.services import get_stats


def main() -> None:
    with connect() as conn:
        init_db(conn)
        summary = import_all_sources(conn)
        stats = get_stats(conn)
    print("导入完成")
    print(summary)
    print(stats)


if __name__ == "__main__":
    main()
