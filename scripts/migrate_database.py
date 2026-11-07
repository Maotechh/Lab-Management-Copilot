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
        def snapshot(table: str, selected=None, exclude_balances=()) -> dict:
            columns = selected or [
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            digest = hashlib.sha256()
            metadata = conn.execute(f"PRAGMA table_info({table})").fetchall()
            keys = [row["name"] for row in sorted(metadata, key=lambda row: row["pk"]) if row["pk"]]
            condition = ""
            if exclude_balances:
                condition = f" WHERE item_id NOT IN ({','.join('?' for _ in exclude_balances)})"
            query = f"SELECT {','.join(columns)} FROM {table}{condition} ORDER BY {','.join(keys or columns)}"
            count = 0
            for row in conn.execute(query, tuple(exclude_balances)):
                count += 1
                digest.update(
                    json.dumps(
                        tuple(row), sort_keys=True, ensure_ascii=False, default=str
                    ).encode()
                )
            return {"columns": columns, "count": count, "hash": digest.hexdigest()}

        candidates = (
            "inventory_items", "inventory_transactions", "source_rows", "stock_balances",
            "stock_events", "loans", "purchase_orders", "schedules", "reservations",
            "lesson_templates", "stock_counts", "location_details", "operation_requests",
            "chat_messages", "chat_attachments", "preparations", "preparation_revisions",
        )
        tables = [table for table in candidates if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()]
        before = {table: snapshot(table) for table in tables}
        # Initializing missing balances is the only permitted addition to an
        # existing business table. Existing balances must remain byte-for-byte
        # equivalent across all original columns, including their quantities.
        missing_balances = tuple(row[0] for row in conn.execute(
            "SELECT id FROM inventory_items WHERE id NOT IN (SELECT item_id FROM stock_balances) ORDER BY id"
        )) if "stock_balances" in tables else ()

        init_db(conn)
        initialize_balances(conn)
        after = {table: snapshot(table) for table in tables}
        original_hashes = {
            table: before[table]["hash"] for table in tables
        }
        preserved = {
            table: snapshot(table, before[table]["columns"], missing_balances if table == "stock_balances" else ())
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
            "initialized_balance_ids": list(missing_balances),
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
