from __future__ import annotations

import tempfile
from pathlib import Path
import sys
import os

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["GENAI_API_KEY"] = ""

from openpyxl import Workbook

from consumable_assistant.database import connect, init_db
from consumable_assistant.importer import import_all_sources
from consumable_assistant.services import (
    apply_transaction,
    get_stats,
    parse_prepare_file,
    parse_transaction_preview,
    search_items,
)


def make_prepare_file(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "清单"
    sheet.append(["耗材名称", "需求数量", "数量单位"])
    sheet.append(["100mL烧杯", 5, "个"])
    sheet.append(["0.6mL离心管", 100, "个"])
    workbook.save(path)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        with connect(db_path) as conn:
            init_db(conn)
            summary = import_all_sources(conn)
            stats = get_stats(conn)
            assert summary["total_imported_rows"] >= 1000, summary
            assert stats["item_count"] >= 890, stats

            search_rows = search_items(conn, "100mL烧杯", limit=10)
            assert search_rows, "search returned no rows"

            preview = parse_transaction_preview(conn, "100mL烧杯入库 2 个")
            assert preview["action"] == "inbound", preview
            assert preview["quantity"] == 2, preview
            assert preview["candidates"], preview

            item_id = int(preview["candidates"][0]["id"])
            before = float(preview["candidates"][0]["quantity"])
            txn = apply_transaction(conn, item_id, "inbound", 2, note="smoke test")
            assert float(txn["quantity_after"]) == before + 2, txn

            prepare_path = Path(tmp) / "prepare.xlsx"
            make_prepare_file(prepare_path)
            prepare = parse_prepare_file(conn, prepare_path)
            assert prepare["row_count"] == 2, prepare
            assert all("missing_quantity" in row for row in prepare["rows"]), prepare

        print("smoke test passed")


if __name__ == "__main__":
    main()
