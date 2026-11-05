from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from consumable_assistant.database import connect, init_db
from consumable_assistant.importer import backfill_duplicate_source_rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore source-row history for duplicate spreadsheet rows without changing inventory."
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--attachment-dir", type=Path, default=ROOT / "Attachment")
    args = parser.parse_args()

    with connect(args.database) as conn:
        init_db(conn)
        inserted = backfill_duplicate_source_rows(conn, args.attachment_dir)
        result = {
            "duplicate_source_rows_added": inserted,
            "source_rows_total": conn.execute(
                "SELECT COUNT(*) FROM source_rows"
            ).fetchone()[0],
            "inventory_items": conn.execute(
                "SELECT COUNT(*) FROM inventory_items"
            ).fetchone()[0],
        }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
