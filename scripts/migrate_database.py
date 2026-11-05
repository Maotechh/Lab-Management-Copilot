from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from consumable_assistant.database import connect, init_db
from consumable_assistant.ledger import initialize_balances


def migrate(path: Path) -> dict:
    with connect(path) as conn:
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("inventory_items", "inventory_transactions", "source_rows")
        }

        def digest(table):
            value = hashlib.sha256()
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
                value.update(
                    json.dumps(dict(row), sort_keys=True, ensure_ascii=False).encode()
                )
            return value.hexdigest()

        original_hashes = {table: digest(table) for table in before}
        init_db(conn)
        initialize_balances(conn)
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        if (
            before != after
            or original_hashes != {table: digest(table) for table in before}
            or conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
        ):
            raise RuntimeError("Migration preservation check failed")
        review = [
            dict(row)
            for row in conn.execute(
                "SELECT item_id,original_quantity,review_reason FROM stock_balances WHERE review_reason IS NOT NULL"
            )
        ]
        differences = [
            dict(row)
            for row in conn.execute(
                "SELECT i.id,i.item_name,i.quantity AS legacy_quantity,b.original_quantity,b.ready,b.cleaning,b.repair,b.quarantine,b.review_reason FROM inventory_items i JOIN stock_balances b ON b.item_id=i.id ORDER BY i.id"
            )
        ]
        report = {
            "before": before,
            "after": after,
            "original_hashes": original_hashes,
            "needs_review": review,
            "item_differences": differences,
            "integrity": "ok",
        }
        conn.execute(
            "UPDATE schema_migrations SET report=? WHERE version=1",
            (json.dumps(report, ensure_ascii=False),),
        )
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = migrate(args.database)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(output, encoding="utf-8")
        args.report.chmod(0o600)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "item_differences"},
            ensure_ascii=False,
        )
    )
