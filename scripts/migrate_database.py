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
        def snapshot(table: str) -> dict:
            columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            digest = hashlib.sha256()
            query = f"SELECT {','.join(columns)} FROM {table} ORDER BY id"
            count = 0
            for row in conn.execute(query):
                count += 1
                digest.update(
                    json.dumps(
                        tuple(row), sort_keys=True, ensure_ascii=False, default=str
                    ).encode()
                )
            return {"columns": columns, "count": count, "hash": digest.hexdigest()}

        tables = ("inventory_items", "inventory_transactions", "source_rows")
        before = {table: snapshot(table) for table in tables}

        def digest(table, columns):
            value = hashlib.sha256()
            query = f"SELECT {','.join(columns)} FROM {table} ORDER BY id"
            for row in conn.execute(query):
                value.update(
                    json.dumps(
                        tuple(row), sort_keys=True, ensure_ascii=False, default=str
                    ).encode()
                )
            return value.hexdigest()

        init_db(conn)
        initialize_balances(conn)
        after = {table: snapshot(table) for table in tables}
        original_hashes = {
            table: before[table]["hash"] for table in tables
        }
        preserved = {
            table: {
                "columns": before[table]["columns"],
                "count": before[table]["count"],
                "hash": digest(table, before[table]["columns"]),
            }
            for table in tables
        }
        if (
            any(
                preserved[table]["count"] != before[table]["count"]
                or preserved[table]["hash"] != before[table]["hash"]
                for table in tables
            )
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
            "preserved": preserved,
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
