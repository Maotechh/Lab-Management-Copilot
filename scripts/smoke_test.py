from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["GENAI_API_KEY"] = ""
os.environ["GENAI_BASE_URL"] = ""
os.environ["GENAI_FALLBACK_PROVIDERS_JSON"] = "[]"
os.environ["GENAI_FALLBACK_MODELS"] = ""
os.environ["GENAI_REQUIRED"] = "0"
os.environ["CONSUMABLE_AUTO_SEED"] = "0"

from openpyxl import Workbook

from consumable_assistant import ledger as stock
from consumable_assistant import workflows as flow
from consumable_assistant.database import connect, init_db
from consumable_assistant.importer import import_all_sources
from consumable_assistant.server import dispatch
from consumable_assistant.services import (
    get_stats,
    parse_transaction_preview,
    search_items,
)


def execute(conn, kind, data):
    return stock.write(
        conn,
        uuid.uuid4().hex,
        "验证教师",
        "smoke-device",
        {"kind": kind, "data": data},
        lambda: dispatch(conn, kind, data, "验证教师"),
    )


def main():
    with (
        tempfile.TemporaryDirectory() as directory,
        connect(Path(directory) / "test.db") as conn,
    ):
        init_db(conn)
        summary = import_all_sources(conn)
        stock.initialize_balances(conn)
        assert summary["total_imported_rows"] >= 1000
        stats = get_stats(conn)
        assert stats["item_count"] >= 890
        assert search_items(conn, "100mL烧杯")
        for message, action in [
            ("100mL烧杯入库2个", "inbound"),
            ("100mL烧杯用了2个不放回", "consume"),
            ("准备采购10个100mL烧杯", "needs_review"),
            ("没有用掉2个烧杯", "needs_review"),
        ]:
            parsed = parse_transaction_preview(conn, message)
            assert parsed["operations"][0]["action"] == action, parsed
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "inventory_items",
                "inventory_transactions",
                "source_rows",
            )
        }
        path = Path(directory) / "inventory.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["类别", "耗材名称", "规格", "数量", "单位", "实验室", "位置"])
        sheet.append(["测试", "验证烧杯", "100mL", 100, "个", "测试室", "TEST-A"])
        workbook.save(path)
        preview = flow.preview_import(conn, path, path.name, "add")
        assert (
            conn.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0]
            == before["inventory_items"]
        )
        execute(conn, "import", {"preview_id": preview["preview_id"]})
        item = search_items(conn, "验证烧杯")[0]
        assert item["ready"] == "100"

        def operation(action, quantity, **extra):
            return execute(
                conn,
                "stock",
                {
                    "item_id": item["id"],
                    "version": stock.view(conn, item["id"])["version"],
                    "action": action,
                    "quantity": str(quantity),
                    **extra,
                },
            )

        loan = operation(
            "borrow",
            20,
            borrower="教师",
            purpose="实验",
            destination="教室",
            due_at="2026-12-01T12:00:00+08:00",
        )["loan_id"]
        operation("return", 18, loan_id=loan, return_state="cleaning")
        operation("return", 2, loan_id=loan, return_state="damaged")
        assert stock.view(conn, item["id"])["owned_quantity"] == "98"
        operation("state", 18, from_state="cleaning", to_state="ready")
        consumed = operation("consume", 3)
        operation("inbound", 5)
        execute(
            conn,
            "undo",
            {
                "event_id": consumed["id"],
                "version": stock.view(conn, item["id"])["version"],
            },
        )
        assert stock.view(conn, item["id"])["ready"] == "103"
        assert (
            conn.execute("SELECT COUNT(*) FROM inventory_transactions").fetchone()[0]
            == before["inventory_transactions"]
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        print(
            "Smoke test passed: original imports, intent, upload preview, loan lifecycle, inverse undo and history preservation"
        )


if __name__ == "__main__":
    main()
