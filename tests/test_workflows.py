from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

os.environ["GENAI_REQUIRED"] = "0"
os.environ["GENAI_BASE_URL"] = ""
os.environ["GENAI_FALLBACK_PROVIDERS_JSON"] = "[]"
os.environ["GENAI_FALLBACK_MODELS"] = ""
os.environ["CONSUMABLE_AUTO_SEED"] = "0"

from consumable_assistant import ledger as stock
from consumable_assistant import workflows as flow
from consumable_assistant.database import connect, init_db
from consumable_assistant.services import infer_action, parse_transaction_preview


@pytest.fixture
def db(tmp_path):
    with connect(tmp_path / "test.db") as conn:
        init_db(conn)
        yield conn


def scripted_dsh(monkeypatch, db, plans):
    """Exercise native tool callbacks without contacting a model in unit tests."""
    from consumable_assistant import chat_runtime
    from consumable_assistant.server import dispatch, history_rows

    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    def transport(payload):
        while True:
            plan = next(plans)
            if plan is None:
                raise chat_runtime.DSHUnavailable("测试模型暂时不可用")
            for call in plan.get("calls", []):
                with connect(path) as conn:
                    result = chat_runtime.execute_tool(conn, payload["runId"], payload["callbackToken"], call["name"], call["arguments"], dispatch, history_rows)
                if result.get("status") == "pending_confirmation":
                    return {"runId": payload["runId"], "status": "completed", "reply": "", "preview": True}
            if not plan.get("calls"):
                return {"runId": payload["runId"], "status": "completed", "reply": plan["reply"]}
    monkeypatch.setattr(chat_runtime, "run_dsh", transport)


def make_item(
    conn, name="玻璃烧杯", quantity=100, spec="100mL", unit="个", location="A1"
):
    item_id = flow.create_item(
        conn,
        {
            "item_name": name,
            "spec": spec,
            "unit": unit,
            "lab": "实验室",
            "location_code": location,
        },
    )
    conn.execute(
        "UPDATE stock_balances SET ready=? WHERE item_id=?", (str(quantity), item_id)
    )
    conn.execute(
        "UPDATE inventory_items SET quantity=? WHERE id=?", (quantity, item_id)
    )
    conn.commit()
    return item_id


def write(conn, kind, data, request_id=None):
    from consumable_assistant.server import dispatch

    return stock.write(
        conn,
        request_id or uuid.uuid4().hex,
        "测试教师",
        "test-device",
        {"kind": kind, "data": data},
        lambda: dispatch(conn, kind, data, "测试教师"),
    )


def operation(conn, item_id, action, quantity, **extra):
    data = {
        "item_id": item_id,
        "version": stock.ensure_balance(conn, item_id)["version"],
        "action": action,
        "quantity": str(quantity),
        **extra,
    }
    conn.commit()
    return write(conn, "stock", data)


def sheet(path, headers, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_exact_inventory_identity_excludes_similar_names_and_keeps_locations(db):
    from consumable_assistant.services import search_items

    first = make_item(db, name="烧杯专用A", location="A1")
    second = make_item(db, name="烧杯专用A", location="B1")
    make_item(db, name="烧杯专用B")
    for query in ("烧杯专用A", "100mL烧杯专用A", "烧杯专用A100mL"):
        assert {row["id"] for row in search_items(db, query)} == {first, second}
    assert len(search_items(db, "烧杯")) == 3


def test_inventory_query_reports_actual_lab_names_without_guessing(db):
    from consumable_assistant.chat import _run_read_tool

    make_item(db)
    result = _run_read_tool(db, "unused", "inventory_search", {"query": "玻璃烧杯", "lab": "实验室A9"}, [], None)
    assert result["rows"] == []
    assert result["available_labs"] == ["实验室"]


def test_inventory_query_separates_unique_lab_and_location_suffix(db):
    from consumable_assistant.chat import _run_read_tool

    item_id = make_item(db, name="玻璃烧杯", location="A1")
    result = _run_read_tool(
        db,
        "unused",
        "inventory_search",
        {"query": "100mL玻璃烧杯", "lab": "实验室A1"},
        [],
        None,
    )

    assert [row["id"] for row in result["rows"]] == [item_id]
    assert result["resolved_scope"] == {
        "lab": "实验室",
        "location_code": "A1",
        "source": "exact_database_name_and_location_match",
    }
    assert "query_guidance" not in result


@pytest.mark.parametrize(
    "message,expected",
    [
        ("100mL烧杯用了2个不放回", "consume"),
        ("100mL烧杯没有用掉5个", "needs_review"),
        ("准备采购10个100mL烧杯", "needs_review"),
        ("如果用了2个烧杯", "needs_review"),
        ("明天归还20个烧杯", "needs_review"),
        ("100mL烧杯已归还2个", "return"),
    ],
)
def test_intent(message, expected):
    assert infer_action(message) == expected


def test_model_result_receives_same_intent_validation(db, monkeypatch):
    from consumable_assistant import services

    make_item(db)
    monkeypatch.setattr(services, "model_is_enabled", lambda: True)
    monkeypatch.setattr(
        services,
        "parse_inventory_message",
        lambda _: {
            "operations": [
                {"action": "inbound", "quantity": 10, "item_query": "玻璃烧杯"}
            ]
        },
    )
    assert parse_transaction_preview(db, "准备采购10个玻璃烧杯")["operations"][0][
        "needs_review"
    ]


def test_undo_preserves_later_inbound(db):
    item = make_item(db)
    first = operation(db, item, "consume", 10)
    operation(db, item, "inbound", 20)
    write(
        db,
        "undo",
        {"event_id": first["id"], "version": stock.view(db, item)["version"]},
    )
    assert stock.view(db, item)["ready"] == "120"


def test_borrow_partial_return_damage(db):
    item = make_item(db)
    loan = operation(
        db,
        item,
        "borrow",
        20,
        borrower="教师甲",
        purpose="实验课",
        destination="教室",
        due_at="2026-12-01T12:00:00+08:00",
    )["loan_id"]
    assert stock.view(db, item)["owned_quantity"] == "100"
    operation(db, item, "return", 18, loan_id=loan, return_state="cleaning")
    operation(db, item, "return", 2, loan_id=loan, return_state="damaged")
    current = stock.view(db, item)
    assert (
        current["ready"],
        current["cleaning"],
        current["borrowed_quantity"],
        current["owned_quantity"],
    ) == ("80", "18", "0", "98")
    operation(db, item, "state", 18, from_state="cleaning", to_state="ready")
    assert stock.view(db, item)["ready"] == "98"
    with pytest.raises(ValueError):
        operation(db, item, "return", 1, loan_id=loan, return_state="ready")


def test_return_must_reference_loan(db):
    item = make_item(db)
    with pytest.raises(ValueError):
        operation(db, item, "return", 30, loan_id=999, return_state="ready")
    assert stock.view(db, item)["ready"] == "100"


def test_units_and_decimal(db):
    item = make_item(db, name="载玻片", quantity=10, unit="盒", spec="50片/盒")
    with pytest.raises(ValueError):
        operation(db, item, "consume", 3, unit="片")
    write(
        db,
        "metadata",
        {
            "item_id": item,
            "version": stock.view(db, item)["version"],
            "conversions": {"片": "0.02"},
        },
    )
    operation(db, item, "consume", 3, unit="片")
    assert stock.view(db, item)["ready"] == "9.94"
    operation(db, item, "inbound", "0.06", unit="盒")
    assert stock.view(db, item)["ready"] == "10"
    with pytest.raises(ValueError):
        operation(db, item, "inbound", "NaN")


def test_version_and_idempotency(db):
    item = make_item(db)
    payload = {"item_id": item, "version": 1, "action": "consume", "quantity": "10"}
    request = uuid.uuid4().hex
    first = write(db, "stock", payload, request)
    assert write(db, "stock", payload, request) == first
    assert stock.view(db, item)["ready"] == "90"
    with pytest.raises(stock.Conflict):
        write(db, "stock", {**payload, "quantity": "11"}, request)
    with pytest.raises(stock.Conflict):
        write(db, "stock", payload)


def test_bulk_rollback(db):
    item = make_item(db)
    operations = [
        {"item_id": item, "version": 1, "action": "consume", "quantity": "10"},
        {"item_id": item, "version": 1, "action": "consume", "quantity": "1000"},
    ]
    with pytest.raises(ValueError):
        write(db, "bulk", {"operations": operations})
    assert stock.view(db, item)["ready"] == "100"
    assert db.execute("SELECT COUNT(*) FROM stock_events").fetchone()[0] == 0


def test_concurrent_teachers(tmp_path):
    path = tmp_path / "concurrency.db"
    with connect(path) as conn:
        init_db(conn)
        item = make_item(conn)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        with connect(path) as conn:
            barrier.wait(timeout=5)
            try:
                write(
                    conn,
                    "stock",
                    {
                        "item_id": item,
                        "version": 1,
                        "action": "consume",
                        "quantity": "10",
                    },
                )
                results.append("ok")
            except stock.Conflict:
                results.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(results) == ["conflict", "ok"]
    with connect(path) as conn:
        operation(conn, item, "consume", 10)
        assert stock.view(conn, item)["ready"] == "80"


def test_preparation_strict_spec_and_shared_allocation(db, tmp_path):
    make_item(db, quantity=100, spec="50mL")
    right = make_item(db, quantity=2, location="A2")
    path = tmp_path / "prepare.xlsx"
    sheet(
        path, ["耗材名称", "规格", "需求数量", "单位"], [["玻璃烧杯", "100mL", 3, "个"]]
    )
    result = flow.preparation(db, path)
    assert result["rows"][0]["available_quantity"] == "2"
    assert result["rows"][0]["missing_quantity"] == "1"
    assert {loc["item_id"] for loc in result["rows"][0]["locations"]} == {right}
    sheet(
        path,
        ["耗材名称", "规格", "需求数量", "单位", "使用方式"],
        [
            ["玻璃烧杯", "50mL", 80, "个", "消耗"],
            ["玻璃烧杯", "50mL", 80, "个", "消耗"],
        ],
    )
    result = flow.preparation(db, path)
    assert [row["missing_quantity"] for row in result["rows"]] == ["0", "60"]
    with pytest.raises(ValueError):
        write(
            db, "prepare", {"preview_id": result["preview_id"], "selected_rows": [2, 3]}
        )
    assert stock.view(db, right)["ready"] == "2"


def test_preparation_requires_explicit_usage(db, tmp_path):
    make_item(db)
    path = tmp_path / "prepare.xlsx"
    sheet(
        path, ["耗材名称", "规格", "需求数量", "单位"], [["玻璃烧杯", "100mL", 3, "个"]]
    )
    result = flow.preparation(db, path)
    payload = {"preview_id": result["preview_id"], "selected_rows": [2]}
    with pytest.raises(ValueError):
        write(db, "prepare", payload)
    assert (
        write(db, "prepare", {**payload, "actions": {"2": "consume"}})[
            "transaction_count"
        ]
        == 1
    )


def test_chat_query_result_can_create_and_confirm_a_bound_write(db, monkeypatch):
    from consumable_assistant import chat
    from consumable_assistant.server import dispatch

    item = make_item(db)
    conversation = chat.new_conversation(db, "chat-workflow")
    plans = iter(
        [
            {
                "reply": "我先查询当前耗材。",
                "calls": [{"name": "inventory_search", "arguments": {"query": "玻璃烧杯"}}],
            },
            {
                "reply": "已找到匹配物品。",
                "calls": [
                    {
                        "name": "business_preview",
                        "arguments": {
                            "kind": "stock",
                            "payload": {"item_id": item, "action": "consume", "quantity": "2", "unit": "个"},
                        },
                    }
                ],
            },
        ]
    )
    scripted_dsh(monkeypatch, db, plans)
    response = chat.process_message(
        db,
        conversation["conversation_id"],
        chat.ChatMessageRequest(message_id=uuid.uuid4().hex, text="请处理刚才查到的烧杯"),
        "Teacher",
        "chat-workflow",
        dispatch,
        lambda *args: [],
    )
    assert response["status"] == "pending_confirmation"
    assert response["plan_hash"] == db.execute(
        "SELECT plan_hash FROM chat_tasks WHERE id=?", (response["task_id"],)
    ).fetchone()[0]
    assert response["preview"]["operation"]["details"]["quantity"] == "2"
    assert stock.view(db, item)["ready"] == "100"
    task = db.execute("SELECT * FROM chat_tasks WHERE id=?", (response["task_id"],)).fetchone()
    with pytest.raises(stock.Conflict):
        chat.confirm_task(
            db, task, "Teacher", "chat-workflow", uuid.uuid4().hex, dispatch,
            expected_plan_hash="0" * 64,
        )
    assert db.execute("SELECT status FROM chat_tasks WHERE id=?", (response["task_id"],)).fetchone()[0] == "pending_confirmation"
    result = chat.confirm_task(
        db, task, "Teacher", "chat-workflow", uuid.uuid4().hex, dispatch,
        expected_plan_hash=response["plan_hash"],
    )
    assert result["quantity_after"] == 98.0
    assert stock.view(db, item)["ready"] == "98"


def test_chat_tool_rounds_reuse_prior_query_results(db, monkeypatch):
    from consumable_assistant import chat

    make_item(db)
    conversation = chat.new_conversation(db, "chat-query")
    plans = iter(
        [
            {"reply": "查询中。", "calls": [{"name": "inventory_search", "arguments": {"query": "烧杯"}}]},
            {"reply": "再次核对。", "calls": [{"name": "inventory_search", "arguments": {"query": "烧杯"}}]},
            {"reply": "结果已核对。", "calls": []},
        ]
    )
    scripted_dsh(monkeypatch, db, plans)
    result = chat.process_message(
        db,
        conversation["conversation_id"],
        chat.ChatMessageRequest(message_id=uuid.uuid4().hex, text="查找烧杯"),
        "Teacher",
        "chat-query",
        lambda *args: {},
        lambda *args: [],
    )
    assert result["reply"].startswith("以下内容来自服务器保存的查询或分析结果")
    assert db.execute("SELECT COUNT(*) FROM chat_tool_calls").fetchone()[0] == 1


def test_chat_preparation_attachment_is_committed_with_its_tool_record(db, monkeypatch, tmp_path):
    from consumable_assistant import chat

    make_item(db)
    path = tmp_path / "preparation.xlsx"
    sheet(
        path,
        ["耗材名称", "规格", "需求数量", "单位", "使用方式"],
        [["玻璃烧杯", "100mL", 3, "个", "消耗"]],
    )
    conversation = chat.new_conversation(db, "chat-file")
    attachment_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO chat_attachments(id,conversation_id,filename,content_type,size,sha256,path) VALUES (?,?,?,?,?,?,?)",
        (attachment_id, conversation["conversation_id"], path.name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", path.stat().st_size, "test", str(path)),
    )
    db.commit()
    plans = iter(
        [
            {"reply": "我核對清單。", "calls": [{"name": "prepare_attachment", "arguments": {"attachment_id": attachment_id}}]},
            {"reply": "已完成清單核對。", "calls": []},
        ]
    )
    scripted_dsh(monkeypatch, db, plans)
    response = chat.process_message(
        db,
        conversation["conversation_id"],
        chat.ChatMessageRequest(message_id=uuid.uuid4().hex, text="請核對這份清單", attachments=[attachment_id]),
        "Teacher",
        "chat-file",
        lambda *args: {},
        lambda *args: [],
    )
    assert response["tool_results"][0]["result"]["type"] == "preparation"
    assert response["tool_results"][0]["result"]["row_count"] == 1
    assert db.execute("SELECT COUNT(*) FROM upload_previews WHERE mode='prepare'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM chat_tool_calls WHERE name='prepare_attachment'").fetchone()[0] == 1


def test_chat_business_contract_rejects_missing_loan_and_purchase_fields():
    from consumable_assistant.chat_contracts import validate_payload

    with pytest.raises(ValueError):
        validate_payload(
            "stock", {"item_id": 1, "action": "borrow", "quantity": "2"}
        )
    with pytest.raises(ValueError):
        validate_payload(
            "purchase", {"item_id": 1, "action": "order", "quantity": "2"}
        )
    assert validate_payload(
        "stock",
        {
            "item_id": 1,
            "action": "borrow",
            "quantity": "2",
            "borrower": "Teacher",
            "purpose": "Lab class",
            "destination": "Room 1",
            "due_at": "2026-12-01T12:00:00+08:00",
            "version": 99,
        },
    )["quantity"] == "2"


def test_search_alert_and_history_quantities_keep_decimal_text(db, monkeypatch):
    from consumable_assistant import server
    from consumable_assistant.services import list_alerts

    item = make_item(db, quantity="0.3")
    operation(db, item, "threshold", "0.5")
    assert list_alerts(db)[0]["missing_quantity"] == "0.2"
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(server, "connect", lambda: connect(path))
    with TestClient(server.app) as client:
        result = client.get("/api/items/search?q=烧杯").json()
        assert result["quantity_by_unit"]["个"] == "0.3"
    operation(db, item, "consume", "0.1")
    history = server.history_rows(db, {})
    assert next(row for row in history if row["action"] == "consume")["delta_quantity"] == "-0.1"


def test_upload_preview_duplicate_and_history(db, tmp_path):
    path = tmp_path / "upload.xlsx"
    sheet(
        path,
        ["耗材名称", "数量", "数量单位", "实验室", "位置"],
        [["载玻片", 10, "片", "实验室", "A1"]],
    )
    first = flow.preview_import(db, path, "upload.xlsx", "add")
    assert db.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 0
    write(db, "import", {"preview_id": first["preview_id"]})
    second = flow.preview_import(db, path, "upload.xlsx", "add")
    assert second["duplicate_file"]
    with pytest.raises(stock.Conflict):
        write(db, "import", {"preview_id": second["preview_id"]})
    item = db.execute("SELECT id FROM inventory_items").fetchone()[0]
    assert stock.view(db, item)["ready"] == "10"
    write(
        db,
        "import",
        {
            "preview_id": second["preview_id"],
            "allow_duplicate": True,
            "note": "另一批到货",
        },
    )
    assert stock.view(db, item)["ready"] == "20"


def schedule_payload(conn, item, start, end, quantity=8, action="borrow"):
    return {
        "name": "实验一",
        "course": "测试课程",
        "venue": "教学实验室",
        "starts_at": start,
        "ends_at": end,
        "people": 30,
        "group_size": 2,
        "lines": [
            {
                "item_id": item,
                "version": stock.view(conn, item)["version"],
                "quantity": str(quantity),
                "action": action,
            }
        ],
    }


def test_scheduling_overlap_and_cleaning(db):
    item = make_item(db, quantity=10)
    first = write(
        db,
        "schedule",
        schedule_payload(
            db, item, "2026-12-01T10:00:00+08:00", "2026-12-01T11:00:00+08:00"
        ),
    )["id"]
    with pytest.raises(ValueError):
        write(
            db,
            "schedule",
            schedule_payload(
                db, item, "2026-12-01T10:30:00+08:00", "2026-12-01T11:30:00+08:00"
            ),
        )
    second = write(
        db,
        "schedule",
        schedule_payload(
            db, item, "2026-12-01T12:00:00+08:00", "2026-12-01T13:00:00+08:00"
        ),
    )["id"]
    result = write(
        db,
        "execute_schedule",
        {
            "schedule_id": first,
            "version": 1,
            "item_versions": {str(item): stock.view(db, item)["version"]},
            "borrower": "实验班",
        },
    )
    loan = result["transactions"][0]["loan_id"]
    operation(db, item, "return", 8, loan_id=loan, return_state="cleaning")
    with pytest.raises(ValueError):
        write(
            db,
            "execute_schedule",
            {
                "schedule_id": second,
                "version": 1,
                "item_versions": {str(item): stock.view(db, item)["version"]},
                "borrower": "实验班",
            },
        )
    operation(db, item, "state", 8, from_state="cleaning", to_state="ready")
    write(
        db,
        "execute_schedule",
        {
            "schedule_id": second,
            "version": 1,
            "item_versions": {str(item): stock.view(db, item)["version"]},
            "borrower": "实验班",
        },
    )
    assert stock.view(db, item)["ready"] == "2"


def test_consumables_cannot_be_reserved_twice_at_different_times(db):
    item = make_item(db, quantity=10)
    write(
        db,
        "schedule",
        schedule_payload(
            db,
            item,
            "2026-12-01T10:00:00+08:00",
            "2026-12-01T11:00:00+08:00",
            action="consume",
        ),
    )
    with pytest.raises(ValueError):
        write(
            db,
            "schedule",
            schedule_payload(
                db,
                item,
                "2026-12-02T10:00:00+08:00",
                "2026-12-02T11:00:00+08:00",
                action="consume",
            ),
        )


def test_templates_calculate_groups():
    result = flow.template_lines(
        {
            "people": 31,
            "group_size": 2,
            "lines": [
                {
                    "item_id": 1,
                    "action": "borrow",
                    "per_group": 2,
                    "per_person": 1,
                    "fixed": 2,
                    "spare": 3,
                }
            ],
        }
    )
    assert result[0]["quantity"] == "68"


def test_purchase_partial_delivery(db):
    item = make_item(db, quantity=0)
    order = write(
        db,
        "purchase",
        {
            "item_id": item,
            "version": 1,
            "quantity": "10",
            "owner": "采购教师",
            "expected_at": "2026-12-01T12:00:00+08:00",
        },
    )["id"]
    assert stock.view(db, item)["ready"] == "0"
    write(
        db,
        "purchase",
        {
            "action": "receive",
            "order_id": order,
            "order_version": 1,
            "version": 1,
            "quantity": "3",
        },
    )
    assert stock.view(db, item)["ready"] == "3"
    with pytest.raises(ValueError):
        write(
            db,
            "purchase",
            {
                "action": "receive",
                "order_id": order,
                "order_version": 2,
                "version": 2,
                "quantity": "8",
            },
        )
    assert stock.view(db, item)["ready"] == "3"


def test_transfer_preserves_history_and_archive(db):
    item = make_item(db)
    operation(db, item, "consume", 10)
    moved = write(
        db,
        "transfer",
        {
            "item_id": item,
            "version": 2,
            "quantity": "90",
            "target_lab": "实验室",
            "target_location": "A2",
        },
    )
    assert moved["source"]["ready"] == "0" and moved["target"]["ready"] == "90"
    write(
        db,
        "archive_location",
        {"lab": "实验室", "location_code": "A1", "note": "位置调整"},
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM stock_events WHERE item_id=?", (item,)
        ).fetchone()[0]
        == 3
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM inventory_items WHERE id=?", (item,)
        ).fetchone()[0]
        == 1
    )


def test_migration_preserves_invalid_original(db):
    item = make_item(db, quantity=-2)
    db.execute("DELETE FROM stock_balances WHERE item_id=?", (item,))
    db.commit()
    current = stock.view(db, item)
    db.commit()
    assert (
        current["ready"] == "0"
        and current["original_quantity"] == "-2.0"
        and current["review_reason"]
    )
    with pytest.raises(ValueError):
        operation(db, item, "consume", 1)
    operation(db, item, "count", 4, note="实物清点四盒")
    assert (
        stock.view(db, item)["ready"] == "4"
        and not stock.view(db, item)["review_reason"]
    )


def test_migration_preserves_legacy_rows_when_schema_grows(tmp_path):
    """A v1 database keeps original values while v4 adds source-row fields."""
    import json
    import sqlite3

    from scripts.migrate_database import migrate

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_migrations(
                version INTEGER PRIMARY KEY,
                applied_at TEXT DEFAULT CURRENT_TIMESTAMP,
                report TEXT
            );
            INSERT INTO schema_migrations(version, report) VALUES (1, '{}');
            CREATE TABLE inventory_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_key TEXT NOT NULL UNIQUE,
                category TEXT,
                lab TEXT,
                location_code TEXT,
                item_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                spec TEXT,
                brand TEXT,
                unit TEXT,
                quantity REAL NOT NULL DEFAULT 0,
                threshold REAL NOT NULL DEFAULT 0,
                remark TEXT,
                source_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE inventory_transactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                delta_quantity REAL NOT NULL DEFAULT 0,
                quantity_before REAL NOT NULL,
                quantity_after REAL NOT NULL,
                threshold_before REAL,
                threshold_after REAL,
                unit TEXT,
                note TEXT,
                actor TEXT,
                source_type TEXT NOT NULL DEFAULT 'manual',
                source_ref TEXT,
                undone_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE loans(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL);
            CREATE TABLE reservations(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL);
            CREATE TABLE purchase_orders(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL);
            CREATE TABLE stock_balances(
                item_id INTEGER PRIMARY KEY,
                ready TEXT NOT NULL,
                cleaning TEXT NOT NULL DEFAULT '0',
                repair TEXT NOT NULL DEFAULT '0',
                quarantine TEXT NOT NULL DEFAULT '0',
                version INTEGER NOT NULL DEFAULT 1,
                review_reason TEXT,
                original_quantity TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                usage_type TEXT NOT NULL DEFAULT 'unclassified',
                conversions TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE source_rows(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                row_hash TEXT NOT NULL,
                source_file TEXT NOT NULL,
                source_sheet TEXT,
                source_row_number INTEGER,
                category TEXT,
                lab TEXT,
                location_code TEXT,
                item_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                spec TEXT,
                brand TEXT,
                quantity REAL NOT NULL DEFAULT 0,
                unit TEXT,
                threshold REAL,
                remark TEXT,
                raw_json TEXT NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO inventory_items(
                item_key,item_name,normalized_name,quantity,threshold
            ) VALUES ('legacy-key','旧耗材','旧耗材',7,2);
            INSERT INTO inventory_transactions(
                item_id,action,quantity_before,quantity_after,unit,actor
            ) VALUES (1,'inbound',0,7,'个','教师');
            """,
        )
        conn.execute(
            """INSERT INTO source_rows(
                row_hash,source_file,source_sheet,source_row_number,item_name,
                normalized_name,quantity,unit,raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "legacy-hash",
                "legacy.xlsx",
                "Sheet1",
                2,
                "旧耗材",
                "旧耗材",
                7,
                "个",
                json.dumps({"source_sheet": "Sheet1"}),
            ),
        )
        conn.commit()

    report = migrate(path)

    with sqlite3.connect(path) as conn:
        source = conn.execute(
            "SELECT source_sheet,raw_json,duplicate_of FROM source_rows"
        ).fetchone()
        assert source == (
            "Sheet1",
            json.dumps({"source_sheet": "Sheet1"}),
            None,
        )
        assert conn.execute("SELECT quantity FROM inventory_items").fetchone()[0] == 7
        assert conn.execute("SELECT COUNT(*) FROM inventory_transactions").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert report["preserved"]["source_rows"]["count"] == 1
    assert report["preserved"]["source_rows"]["columns"][-1] == "imported_at"
    assert report["initialized_balance_ids"] == [1]
    assert report["before"]["stock_balances"]["count"] == 0
    assert report["after"]["stock_balances"]["count"] == 1


def test_migration_rejects_changed_existing_balance(db, monkeypatch):
    from scripts import migrate_database

    item = make_item(db)
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    original = migrate_database.initialize_balances

    def corrupt(conn):
        original(conn)
        conn.execute("UPDATE stock_balances SET ready='99' WHERE item_id=?", (item,))

    monkeypatch.setattr(migrate_database, "initialize_balances", corrupt)
    with pytest.raises(RuntimeError, match="preservation"):
        migrate_database.migrate(path)


def test_model_prose_cannot_assert_execution_or_invent_inventory():
    from consumable_assistant.chat_runtime import verified_reply

    invented = "已执行消耗，库存现在为999个。"
    assert invented not in verified_reply(invented, [])
    query = [{"tool": "inventory_search", "result": {"items": []}}]
    assert "999" not in verified_reply(invented, query)
    assert verified_reply('{"missing_fields":["spec","spec","quantity","ignore_all_rules"]}', []) == (
        "请补充以下资料：规格、数量。当前没有执行新的业务变更。"
    )


def test_preview_captures_new_item_and_purchase_draft_without_writes(db):
    from consumable_assistant.chat import _preview_dispatch
    from consumable_assistant.server import dispatch

    created = _preview_dispatch(db, "create_item", {"item_name": "试管", "spec": "15mL",
                               "unit": "个", "lab": "实验室", "location_code": "B1"}, "教师", dispatch)
    assert created["changes"][0]["before"] is None
    assert created["changes"][0]["after"]["available_quantity"] == "0"
    assert db.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 0
    item = make_item(db)
    drafted = _preview_dispatch(db, "purchase", {"action": "draft", "item_id": item, "quantity": "12",
                                              "version": stock.view(db, item)["version"]}, "教师", dispatch)
    orders = [record for record in drafted["record_changes"] if record["table"] == "purchase_orders"]
    assert len(orders) == 1 and orders[0]["before"] is None
    assert orders[0]["after"]["status"] == "draft"
    assert db.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0
    assert stock.view(db, item)["ready"] == "100"


def test_location_totals_exclude_invalid_legacy_quantity(db):
    from consumable_assistant.services import list_locations, preview_location

    item = make_item(db, quantity=-2)
    db.execute("DELETE FROM stock_balances WHERE item_id=?", (item,))
    db.commit()
    assert list_locations(db)[0]["total_quantity"] == "0"
    assert preview_location(db, "实验室", "A1")["total_quantity"] == "0"
    assert (
        db.execute(
            "SELECT quantity FROM inventory_items WHERE id=?", (item,)
        ).fetchone()[0]
        == -2
    )


def test_alerts_track_item_transitions(db):
    first = make_item(db)
    second = make_item(db, location="A2")
    operation(db, first, "threshold", 95)
    operation(db, second, "threshold", 95)
    operation(db, first, "consume", 10)
    before = db.execute(
        "SELECT COUNT(*) FROM notification_events WHERE kind='low_stock'"
    ).fetchone()[0]
    operation(db, first, "inbound", 10)
    operation(db, second, "consume", 10)
    after = db.execute(
        "SELECT COUNT(*) FROM notification_events WHERE kind='low_stock'"
    ).fetchone()[0]
    assert after == before + 1


def test_alert_ack_snooze_and_event_version(db):
    item = make_item(db)
    operation(db, item, "threshold", 95)
    operation(db, item, "consume", 10)
    event = db.execute(
        "SELECT event_id FROM alert_states WHERE item_id=?", (item,)
    ).fetchone()[0]
    write(
        db,
        "alert",
        {
            "item_id": item,
            "event_id": event,
            "action": "snooze",
            "until": "2026-12-01T10:00:00+08:00",
        },
    )
    assert db.execute(
        "SELECT snooze_until FROM alert_states WHERE item_id=?", (item,)
    ).fetchone()[0]
    write(db, "alert", {"item_id": item, "event_id": event, "action": "acknowledge"})
    assert (
        db.execute(
            "SELECT acknowledged FROM alert_states WHERE item_id=?", (item,)
        ).fetchone()[0]
        == 1
    )
    operation(db, item, "inbound", 10)
    with pytest.raises(stock.Conflict):
        write(
            db, "alert", {"item_id": item, "event_id": event, "action": "acknowledge"}
        )


def test_spec_constraints_survive_model_omission(db, monkeypatch):
    from consumable_assistant import services

    make_item(db, spec="50mL")
    expected = make_item(db, spec="100mL")
    monkeypatch.setattr(services, "model_is_enabled", lambda: True)
    monkeypatch.setattr(
        services,
        "parse_inventory_message",
        lambda _: {
            "operations": [
                {"action": "consume", "quantity": 2, "item_query": "玻璃烧杯"}
            ]
        },
    )
    result = services.parse_transaction_preview(db, "100mL玻璃烧杯用了2个")
    assert [item["id"] for item in result["operations"][0]["candidates"]] == [expected]


def test_api_requires_request_identity_and_retries(db, monkeypatch):
    from consumable_assistant import server

    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    item = make_item(db)
    monkeypatch.setattr(server, "connect", lambda: connect(path))
    with TestClient(server.app) as client:
        payload = {"item_id": item, "version": 1, "action": "consume", "quantity": "10"}
        assert client.post("/api/actions/stock", json=payload).status_code == 400
        headers = {
            "X-Request-ID": uuid.uuid4().hex,
            "X-Actor": "teacher",
            "X-Device-ID": "device",
        }
        response = client.post("/api/actions/stock", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        assert (
            client.post("/api/actions/stock", json=payload, headers=headers).json()
            == response.json()
        )
        assert (
            client.post(
                "/api/actions/stock",
                json=payload,
                headers={**headers, "Origin": "https://external.invalid"},
            ).status_code
            == 403
        )


def test_chat_conversation_preview_confirm_is_idempotent(db, monkeypatch):
    from consumable_assistant import server

    item = make_item(db)
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(server, "connect", lambda: connect(path))
    scripted_dsh(monkeypatch, db, iter([
        {"calls": [{"name": "inventory_search", "arguments": {"query": "玻璃烧杯"}}]},
        {"calls": [{"name": "business_preview", "arguments": {"kind": "stock", "payload": {"item_id": item, "action": "consume", "quantity": "2", "unit": "个"}}}]},
    ]))
    with TestClient(server.app) as client:
        headers = {"X-Device-ID": "chat-device"}
        created = client.post("/api/chat/conversations", headers=headers)
        assert created.status_code == 200, created.text
        identity = created.json()
        auth = {**headers, "X-Chat-Token": identity["token"], "X-Actor": "Teacher"}
        message = {
            "message_id": uuid.uuid4().hex,
            "text": "玻璃烧杯用了2个不放回",
        }
        response = client.post(
            f"/api/chat/conversations/{identity['conversation_id']}/messages",
            headers=auth,
            json=message,
        )
        assert response.status_code == 200, response.text
        queued = response.json()
        import time

        for _ in range(100):
            run = client.get(f"/api/chat/runs/{queued['run_id']}").json()
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        task = run["result"]
        assert task["status"] == "pending_confirmation"
        assert stock.view(db, item)["ready"] == "100"
        repeated = client.post(
            f"/api/chat/conversations/{identity['conversation_id']}/messages", headers=auth, json=message,
        )
        assert repeated.json()["run_id"] == queued["run_id"]
        assert repeated.json()["result"] == task
        confirmation = uuid.uuid4().hex
        confirmed = client.post(
            f"/api/chat/tasks/{task['task_id']}/confirm",
            headers=auth,
            json={"request_id": confirmation, "plan_hash": task["plan_hash"]},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert stock.view(db, item)["ready"] == "98"
        repeated_confirmation = client.post(
            f"/api/chat/tasks/{task['task_id']}/confirm",
            headers=auth,
            json={"request_id": uuid.uuid4().hex, "plan_hash": task["plan_hash"]},
        )
        assert repeated_confirmation.json() == confirmed.json()
        assert stock.view(db, item)["ready"] == "98"
        assert client.get(
            f"/api/chat/conversations/{identity['conversation_id']}",
            headers={"X-Device-ID": "another-device"},
        ).status_code == 200


def test_chat_rejects_confirmed_write_with_stale_version(db, monkeypatch):
    from consumable_assistant import chat

    item = make_item(db)
    conversation = chat.new_conversation(db, "device")
    preview = {
        "operations": [
            {
                "id": "op-0",
                "raw_text": "玻璃烧杯消耗2个",
                "action": "consume",
                "action_label": "消耗",
                "quantity": 2,
                "unit": "个",
                "item_query": "玻璃烧杯",
                "candidates": [stock.view(db, item)],
                "needs_review": False,
                "confidence": 1,
            }
        ]
    }
    response = chat._transaction_task(
        db,
        conversation["conversation_id"],
        1,
        uuid.uuid4().hex,
        "玻璃烧杯消耗2个",
        preview,
        [],
    )
    chat.prepare_transaction_task(
        db,
        conversation["conversation_id"],
        response["task_id"],
        [{"item_id": item, "action": "consume", "quantity": 2}],
        "Teacher",
        lambda conn, kind, data, actor: __import__(
            "consumable_assistant.server", fromlist=["dispatch"]
        ).dispatch(conn, kind, data, actor),
    )
    operation(db, item, "consume", 1)
    task = db.execute("SELECT * FROM chat_tasks WHERE id=?", (response["task_id"],)).fetchone()
    with pytest.raises(chat.PlanChanged) as changed:
        chat.confirm_task(
            db,
            task,
            "Teacher",
            "device",
            uuid.uuid4().hex,
            lambda conn, kind, data, actor: __import__(
                "consumable_assistant.server", fromlist=["dispatch"]
            ).dispatch(conn, kind, data, actor),
        )
    assert stock.view(db, item)["ready"] == "99"
    assert db.execute("SELECT status FROM chat_tasks WHERE id=?", (response["task_id"],)).fetchone()[0] == "expired"
    refreshed = changed.value.response
    assert refreshed["status"] == "pending_confirmation"
    assert refreshed["task_id"] != response["task_id"]
    assert refreshed["previous_preview"]["changes"][0]["before"]["available_quantity"] == "100"
    assert refreshed["preview"]["changes"][0]["before"]["available_quantity"] == "99"
    assert refreshed["preview"]["changes"][0]["after"]["available_quantity"] == "97"
    from consumable_assistant.server import dispatch

    with pytest.raises(stock.Conflict):
        chat.confirm_task(db, task, "Teacher", "device", uuid.uuid4().hex, dispatch)
    replacement = db.execute("SELECT * FROM chat_tasks WHERE id=?", (refreshed["task_id"],)).fetchone()
    result = chat.confirm_task(db, replacement, "Another teacher", "phone", uuid.uuid4().hex, dispatch,
                               expected_plan_hash=refreshed["plan_hash"])
    assert stock.view(db, item)["ready"] == "97"
    assert chat.confirm_task(db, replacement, "Teacher", "device", uuid.uuid4().hex, dispatch,
                             expected_plan_hash=refreshed["plan_hash"]) == result
    assert stock.view(db, item)["ready"] == "97"


def test_unverified_fallback_cannot_be_called(monkeypatch):
    from consumable_assistant import genai

    monkeypatch.setattr(genai, "GENAI_MODEL", "deepseek-pro")
    monkeypatch.setattr(genai, "GENAI_BASE_URL", "http://primary.test/v1")
    monkeypatch.setenv("GENAI_FALLBACK_MODELS", "")
    monkeypatch.setenv(
        "GENAI_FALLBACK_PROVIDERS_JSON",
        '[{"name":"GLM test","model":"glm-chat","base_url":"http://glm.test/v1"}]',
    )
    monkeypatch.setattr(genai.httpx, "Client", lambda **kwargs: pytest.fail("An unverified provider was contacted"))
    with pytest.raises(genai.AIUnavailable, match="备用模型"):
        genai.parse_inventory_message("玻璃烧杯消耗2个")


def test_no_implicit_alternative_models(monkeypatch):
    from consumable_assistant import genai

    monkeypatch.setattr(genai, "GENAI_API_KEY", "")
    monkeypatch.setattr(genai, "GENAI_BASE_URL", "")
    monkeypatch.setenv("GENAI_FALLBACK_MODELS", "")
    monkeypatch.delenv("GENAI_FALLBACK_PROVIDERS_JSON", raising=False)
    assert genai.provider_entries() == []
    assert not genai.model_is_enabled()


def test_deployment_uses_only_qualified_school_deepseek_without_primary_key(tmp_path):
    from scripts.configure_environment import configure

    path = tmp_path / "environment"
    path.write_text(
        "GENAI_REQUIRED=0\nVAPID_PRIVATE_KEY=existing\nVAPID_PUBLIC_KEY=existing\n",
        encoding="utf-8",
    )
    configure(path)
    configured = path.read_text(encoding="utf-8")
    assert "GENAI_REQUIRED=1" in configured
    assert "GENAI_API_KEY=\n" in configured
    assert "GENAI_MODE=responses\n" in configured
    assert "GENAI_BASE_URL=http://10.15.171.204:30969/v1" in configured
    assert "GENAI_FALLBACK_PROVIDERS_JSON=[]" in configured
    assert "PUBLIC_BASE_URL=https://10.15.21.115" in configured


def test_inventory_parser_uses_school_responses_without_credentials(monkeypatch):
    import json

    import httpx

    from consumable_assistant import genai

    requests = []

    def endpoint(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert request.url == "http://school.test/v1/responses"
        assert "authorization" not in request.headers
        assert payload["reasoning"] == {"effort": "high"}
        assert payload["max_output_tokens"] == 65536
        assert payload["stream"] is False and payload["store"] is False
        assert "temperature" not in payload
        return httpx.Response(200, json={
            "model": "deepseek-pro", "status": "completed",
            "output": [{"type": "reasoning", "summary": []}, {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": '{"operations":[{"action":"inbound","quantity":1}]}'}],
            }],
        })

    client_type = httpx.Client
    monkeypatch.setattr(genai.httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(endpoint), **kwargs))
    monkeypatch.setattr(genai, "GENAI_API_KEY", "")
    monkeypatch.setattr(genai, "GENAI_BASE_URL", "http://school.test/v1")
    monkeypatch.setattr(genai, "GENAI_MODE", "responses")
    monkeypatch.setattr(genai, "GENAI_MODEL", "deepseek-pro")
    monkeypatch.setattr(genai, "GENAI_MAX_OUTPUT_TOKENS", 65536)
    monkeypatch.setenv("GENAI_FALLBACK_PROVIDERS_JSON", "[]")
    monkeypatch.setenv("GENAI_FALLBACK_MODELS", "")
    monkeypatch.setattr(genai, "record_health", lambda *args: None)
    monkeypatch.setattr(genai, "record_provider_check", lambda *args: None)
    assert genai.parse_inventory_message("100mL烧杯实际入库1个")["operations"] == [{"action": "inbound", "quantity": 1}]
    assert len(requests) == 1


def test_chat_retries_saved_message_after_ai_outage(db, monkeypatch):
    from consumable_assistant import chat

    conversation = chat.new_conversation(db, "device")
    replies = iter(
        [
            None,
            {"reply": "已查询当前校内记录。", "calls": [], "_provider": "Qwen"},
        ]
    )
    scripted_dsh(monkeypatch, db, replies)
    request = chat.ChatMessageRequest(message_id=uuid.uuid4().hex, text="有哪些烧杯")

    first = chat.process_message(
        db,
        conversation["conversation_id"],
        request,
        "Teacher",
        "device",
        lambda *args: {},
        lambda *args: [],
    )
    assert first["status"] == "failed"

    assert chat.process_message(db, conversation["conversation_id"], request, "Teacher", "device", lambda *args: {}, lambda *args: []) == first
    request = request.model_copy(update={"message_id": uuid.uuid4().hex, "text": "继续查询烧杯"})
    second = chat.process_message(
        db,
        conversation["conversation_id"],
        request,
        "Teacher",
        "device",
        lambda *args: {},
        lambda *args: [],
    )
    assert second["status"] == "answered"
    assert db.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE conversation_id=? AND request_id=? AND role='assistant'",
        (conversation["conversation_id"], request.message_id),
    ).fetchone()[0] == 1


def test_chat_rejects_reused_message_id_with_different_content(db, monkeypatch):
    from consumable_assistant import chat

    conversation = chat.new_conversation(db, "device")
    scripted_dsh(monkeypatch, db, iter([{"reply": "已查询", "calls": []}]))
    message_id = uuid.uuid4().hex
    first = chat.process_message(
        db,
        conversation["conversation_id"],
        chat.ChatMessageRequest(message_id=message_id, text="查询100mL烧杯"),
        "Teacher",
        "device",
        lambda *args: {},
        lambda *args: [],
    )
    with pytest.raises(stock.Conflict):
        chat.process_message(
            db,
            conversation["conversation_id"],
            chat.ChatMessageRequest(message_id=message_id, text="查询完全不同的物品"),
            "Teacher",
            "device",
            lambda *args: {},
            lambda *args: [],
        )
    assert first["status"] == "answered"
    assert db.execute(
        "SELECT content FROM chat_messages WHERE conversation_id=? AND request_id=? AND role='user'",
        (conversation["conversation_id"], message_id),
    ).fetchone()[0] == "查询100mL烧杯"


def test_chat_serializes_concurrent_messages_per_conversation(db, monkeypatch):
    from consumable_assistant import chat, chat_runtime

    conversation = chat.new_conversation(db, "device")
    model_started = threading.Event()
    release_model = threading.Event()

    def fake_model(*args, **kwargs):
        model_started.set()
        assert release_model.wait(timeout=5)
        return {"reply": "已查询", "status": "completed"}

    monkeypatch.setattr(chat_runtime, "run_dsh", fake_model)
    first_result = []
    database_path = Path(db.execute("PRAGMA database_list").fetchone()[2])

    def first_request():
        with connect(database_path) as conn:
            first_result.append(
                chat.process_message(
                    conn,
                    conversation["conversation_id"],
                    chat.ChatMessageRequest(message_id=uuid.uuid4().hex, text="查询烧杯"),
                    "Teacher",
                    "device",
                    lambda *args: {},
                    lambda *args: [],
                )
            )

    thread = threading.Thread(target=first_request)
    thread.start()
    assert model_started.wait(timeout=5)
    with connect(database_path) as conn:
        second = chat.process_message(
            conn, conversation["conversation_id"],
            chat.ChatMessageRequest(message_id=uuid.uuid4().hex, text="查询量筒"),
            "Teacher", "another-device", lambda *args: {}, lambda *args: [],
        )
        assert second["status"] == "queued"
        assert chat_runtime.claim(conn, second["run_id"]) is None
    release_model.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert first_result[0]["status"] == "answered"
    claimed = chat_runtime.claim(db, second["run_id"])
    assert claimed is not None
    assert claimed["device_id"] == "another-device"
    assert db.execute(
        "SELECT COUNT(*) FROM chat_tasks WHERE conversation_id=? "
        "AND status IN ('awaiting_details','pending_confirmation','executing')",
        (conversation["conversation_id"],),
    ).fetchone()[0] <= 1


def test_v4_migration_preserves_source_rows_and_expires_older_active_tasks(db):
    from consumable_assistant.migrations import migrate_runtime

    conversation = __import__("consumable_assistant.chat", fromlist=["new_conversation"]).new_conversation(
        db, "migration-device"
    )
    message_ids = []
    for request_id in ("migration-old", "migration-new"):
        cursor = db.execute(
            "INSERT INTO chat_messages(conversation_id,request_id,role,content) VALUES (?,?,?,?)",
            (conversation["conversation_id"], request_id, "user", request_id),
        )
        message_ids.append(cursor.lastrowid)
    db.execute("DROP INDEX idx_chat_one_active_task")
    for index, message_id in enumerate(message_ids):
        db.execute(
            "INSERT INTO chat_tasks(id,conversation_id,message_id,status,kind,plan_json,plan_hash,preview_json,expires_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"migration-task-{index}",
                conversation["conversation_id"],
                message_id,
                "pending_confirmation",
                "stock",
                "{}",
                "hash",
                "{}",
                "2099-01-01T00:00:00+00:00",
                f"2026-01-0{index + 1} 00:00:00",
            ),
        )
    db.execute("DROP TABLE chat_task_events")
    db.execute("DELETE FROM schema_migrations WHERE version=4")
    db.execute("CREATE UNIQUE INDEX legacy_source_rows_hash ON source_rows(row_hash)")
    db.commit()
    migrate_runtime(db)
    statuses = {
        row["id"]: row["status"]
        for row in db.execute("SELECT id,status FROM chat_tasks WHERE id LIKE 'migration-task-%'")
    }
    assert statuses == {
        "migration-task-0": "expired",
        "migration-task-1": "pending_confirmation",
    }
    assert db.execute("SELECT COUNT(*) FROM chat_task_events").fetchone()[0] == 2
    assert not any(
        index["unique"]
        for index in db.execute("PRAGMA index_list(source_rows)")
    )


def test_source_import_retains_duplicate_rows_and_actual_sheet_name(db, tmp_path):
    from consumable_assistant.importer import SourceConfig, import_source

    path = tmp_path / "duplicates.xlsx"
    sheet(
        path,
        ["类别", "耗材名称", "位置", "数量", "单位", "预警"],
        [
            ["测试", "重复烧杯", "A1", 10, "个", 2],
            ["测试", "重复烧杯", "A1", 10, "个", 2],
        ],
    )
    config = SourceConfig(
        filename=path.name,
        category="测试",
        lab="测试室",
        name_col="耗材名称",
        location_col="位置",
        quantity_col="数量",
        unit_col="单位",
        threshold_col="预警",
    )
    result = import_source(db, path, config)
    rows = list(db.execute("SELECT source_sheet,source_row_number,duplicate_of FROM source_rows ORDER BY id"))
    item = db.execute("SELECT quantity FROM inventory_items WHERE item_name='重复烧杯'").fetchone()
    assert result["imported_rows"] == 1
    assert result["skipped_rows"] == 1
    assert [(row["source_sheet"], row["source_row_number"]) for row in rows] == [
        ("Sheet", 2),
        ("Sheet", 3),
    ]
    assert rows[0]["duplicate_of"] is None
    assert rows[1]["duplicate_of"] == db.execute(
        "SELECT id FROM source_rows ORDER BY id LIMIT 1"
    ).fetchone()[0]
    assert item["quantity"] == 10
    repeated = import_source(db, path, config)
    assert repeated["imported_rows"] == 0
    assert db.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0] == 2
    assert db.execute(
        "SELECT quantity FROM inventory_items WHERE item_name='重复烧杯'"
    ).fetchone()[0] == 10
def test_ai_retries_only_the_qualified_provider(monkeypatch):
    import httpx

    from consumable_assistant import genai

    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append(url)
            if len(calls) < 2:
                raise httpx.ConnectTimeout("timeout")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"operations":[{"action":"inbound","quantity":1}]}'
                            }
                        }
                    ]
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(genai, "model_is_enabled", lambda: True)
    monkeypatch.setattr(genai, "GENAI_API_KEY", "test-key")
    monkeypatch.setattr(genai, "GENAI_BASE_URL", "http://primary.test/v1")
    monkeypatch.setattr(genai, "GENAI_MODE", "openai")
    monkeypatch.setenv("GENAI_FALLBACK_PROVIDERS_JSON", "[]")
    monkeypatch.setattr(genai.httpx, "Client", Client)
    monkeypatch.setattr(genai.time, "sleep", lambda _: None)
    monkeypatch.setattr(genai, "record_health", lambda *args: None)
    monkeypatch.setattr(genai, "record_provider_check", lambda *args: None)
    assert genai.parse_inventory_message("test")["operations"][0]["quantity"] == 1
    assert len(calls) == 2


def test_purchase_receipt_cannot_bypass_order_history(db):
    item = make_item(db, quantity=0)
    order = write(
        db,
        "purchase",
        {
            "item_id": item,
            "version": 1,
            "quantity": "10",
            "owner": "采购教师",
            "expected_at": "2026-12-01T12:00:00+08:00",
        },
    )["id"]
    write(
        db,
        "purchase",
        {
            "action": "receive",
            "order_id": order,
            "order_version": 1,
            "version": 1,
            "quantity": "3",
        },
    )
    event = db.execute(
        "SELECT id FROM stock_events WHERE action='purchase_receive'"
    ).fetchone()[0]
    with pytest.raises(ValueError):
        write(db, "undo", {"event_id": event, "version": 2})
    assert (
        db.execute(
            "SELECT received FROM purchase_orders WHERE id=?", (order,)
        ).fetchone()[0]
        == "3"
    )
    assert stock.view(db, item)["ready"] == "3"


def test_inbound_undo_checks_transfer_dependency(db):
    item = make_item(db)
    entry = operation(db, item, "inbound", 20)
    write(
        db,
        "transfer",
        {
            "item_id": item,
            "version": 2,
            "quantity": "5",
            "target_lab": "实验室",
            "target_location": "A2",
        },
    )
    with pytest.raises(stock.Conflict):
        write(db, "undo", {"event_id": entry["id"], "version": 3})
    assert stock.view(db, item)["ready"] == "115"


def test_later_borrow_reservation_protects_against_new_consumption(db):
    item = make_item(db, quantity=10)
    write(
        db,
        "schedule",
        schedule_payload(
            db,
            item,
            "2026-12-02T10:00:00+08:00",
            "2026-12-02T11:00:00+08:00",
            quantity=8,
        ),
    )
    with pytest.raises(ValueError):
        write(
            db,
            "schedule",
            schedule_payload(
                db,
                item,
                "2026-12-01T10:00:00+08:00",
                "2026-12-01T11:00:00+08:00",
                quantity=8,
                action="consume",
            ),
        )
    assert db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 1


def test_manual_preparation_match_reallocates_whole_list(db, tmp_path):
    small = make_item(db, quantity=4, spec="50mL")
    large = make_item(db, quantity=8, spec="100mL")
    path = tmp_path / "ambiguous.xlsx"
    sheet(
        path,
        ["耗材名称", "需求数量", "单位", "使用方式"],
        [["玻璃烧杯", 3, "个", "消耗"], ["玻璃烧杯", 3, "个", "消耗"]],
    )
    result = flow.preparation(db, path)
    assert result["rows"][0]["status"] == "需要确认匹配"
    result = write(
        db,
        "prepare_remap",
        {"preview_id": result["preview_id"], "selections": {"2": small, "3": small}},
    )
    assert [row["missing_quantity"] for row in result["rows"]] == ["0", "2"]
    with pytest.raises(ValueError):
        write(
            db, "prepare", {"preview_id": result["preview_id"], "selected_rows": [2, 3]}
        )
    assert stock.view(db, small)["ready"] == "4"
    result = write(
        db,
        "prepare_remap",
        {"preview_id": result["preview_id"], "selections": {"3": large}},
    )
    write(db, "prepare", {"preview_id": result["preview_id"], "selected_rows": [2, 3]})
    assert (
        stock.view(db, small)["ready"] == "1" and stock.view(db, large)["ready"] == "5"
    )


def test_failed_import_preserves_every_row(db, tmp_path):
    item = make_item(db)
    write(db, "metadata", {"item_id": item, "version": 1, "category": "未分类"})
    path = tmp_path / "failed.xlsx"
    sheet(
        path,
        ["耗材名称", "规格", "数量", "单位", "实验室", "位置"],
        [
            ["新物品", "", 10, "个", "实验室", "A1"],
            ["玻璃烧杯", "100mL", 3, "个", "实验室", "A1"],
        ],
    )
    preview = flow.preview_import(db, path, path.name, "add")
    operation(db, item, "consume", 1)
    count = db.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0]
    with pytest.raises(stock.Conflict):
        write(db, "import", {"preview_id": preview["preview_id"]})
    assert db.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == count
    assert stock.view(db, item)["ready"] == "99"


def test_stats_and_export_use_state_balances(db):
    import io

    from openpyxl import load_workbook

    from consumable_assistant.services import export_inventory_workbook, get_stats

    item = make_item(db)
    operation(
        db,
        item,
        "borrow",
        20,
        borrower="教师",
        purpose="实验",
        destination="教室",
        due_at="2026-12-01T12:00:00+08:00",
    )
    assert get_stats(db)["transaction_count"] == 1
    row = list(load_workbook(io.BytesIO(export_inventory_workbook(db))).active.values)
    assert row[1][6] == 80 and row[1][11:13] == ("100", "20")


def test_summary_totals_preserve_decimal_precision(db):
    from consumable_assistant.services import (
        get_stats,
        list_locations,
        preview_location,
    )

    first = make_item(db, name="精度测试甲", quantity="0.1")
    second = make_item(db, name="精度测试乙", quantity="0.2", location="A1")
    assert first != second
    category = next(
        row for row in get_stats(db)["categories"] if row["category"] == ""
    )
    assert category["total_quantity"] == "0.3"
    assert next(
        row for row in list_locations(db) if row["lab"] == "实验室" and row["location_code"] == "A1"
    )["total_quantity"] == "0.3"
    assert preview_location(db, "实验室", "A1")["total_quantity"] == "0.3"


def test_restore_is_complete(db, tmp_path):
    import importlib.util

    module_path = Path(__file__).resolve().parents[1] / "scripts" / "backup_database.py"
    spec = importlib.util.spec_from_file_location("backup_database", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    item = make_item(db)
    operation(db, item, "consume", "0.25")
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    snapshot = module.backup(path, tmp_path / "backups")
    counts = module.verify_restore(snapshot)
    assert counts["stock_events"] == 1
    with sqlite3.connect(snapshot) as restored:
        assert (
            restored.execute(
                "SELECT ready FROM stock_balances WHERE item_id=?", (item,)
            ).fetchone()[0]
            == "99.75"
        )
        assert (
            restored.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0]
            == 1
        )


def test_notification_retry_expiry_and_generic_payload(db, monkeypatch):
    from consumable_assistant import notifications

    original_send = notifications.send
    from types import SimpleNamespace

    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(notifications, "connect", lambda: connect(path))
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-key")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:operator@example.org")
    item = make_item(db)
    db.execute(
        "INSERT INTO push_subscriptions(endpoint,subscription,actor,device_id) VALUES ('https://fcm.googleapis.com/test','{}','teacher','device')"
    )
    db.commit()
    operation(db, item, "threshold", 95)
    operation(db, item, "consume", 10)

    def fail(_):
        raise TimeoutError("temporary")

    monkeypatch.setattr(notifications, "send", fail)
    notifications.tick()
    delivery = db.execute("SELECT * FROM push_deliveries").fetchone()
    assert delivery["status"] == "pending" and delivery["attempts"] == 1
    db.execute("UPDATE push_deliveries SET next_attempt='2000-01-01'")
    db.commit()

    def expired(_):
        error = RuntimeError("expired")
        error.response = SimpleNamespace(status_code=410)
        raise error

    monkeypatch.setattr(notifications, "send", expired)
    notifications.tick()
    assert db.execute("SELECT active FROM push_subscriptions").fetchone()[0] == 0
    assert db.execute("SELECT status FROM push_deliveries").fetchone()[0] == "expired"
    with pytest.raises(ValueError):
        notifications.validate_subscription(
            {"endpoint": "https://127.0.0.1/push", "keys": {"auth": "a", "p256dh": "b"}}
        )
    import json

    import pywebpush

    captured = {}
    monkeypatch.setattr(notifications, "validate_subscription", lambda _: None)
    monkeypatch.setattr(pywebpush, "webpush", lambda **kwargs: captured.update(kwargs))
    original_send({})
    payload = json.loads(captured["data"])
    assert (
        set(payload) == {"title", "body", "url"} and "玻璃烧杯" not in captured["data"]
    )


def preparation_data(ids, **extra):
    return {"name": "容量测量", "course": "化学实验", "venue": "实验室",
            "starts_at": "2026-12-01T08:00:00+08:00", "ends_at": "2026-12-01T12:00:00+08:00",
            "people": 5, "group_size": 2,
            "lines": [{"item_ids": ids, "action": "borrow", "per_group": "2", "spare": "1"}],
            **extra}


def test_preparation_partial_stock_draft_and_explicit_replenish(db):
    from consumable_assistant.chat import _fill_versions
    from consumable_assistant.preparations import view

    first = make_item(db, quantity=2)
    second = make_item(db, quantity=3, location="B1")
    result = write(db, "preparation", _fill_versions(db, "preparation", preparation_data([first, second])))
    assert result["lines"][0]["required_quantity"] == "7"
    assert result["lines"][0]["reserved_quantity"] == "5"
    assert result["lines"][0]["unmet_quantity"] == "2"
    draft_id = result["purchase_draft_ids"][0]
    draft = db.execute("SELECT * FROM purchase_orders WHERE id=?", (draft_id,)).fetchone()
    assert draft["status"] == "draft"
    assert stock.view(db, first)["ready_quantity"] == "2"
    with pytest.raises(ValueError, match="草案"):
        write(db, "purchase", _fill_versions(db, "purchase", {"action": "receive", "order_id": draft_id, "quantity": "2"}))
    write(db, "purchase", _fill_versions(db, "purchase", {"action": "place", "order_id": draft_id, "owner": "教师",
                                                        "expected_at": "2026-11-30T12:00:00+08:00"}))
    write(db, "purchase", _fill_versions(db, "purchase", {"action": "receive", "order_id": draft_id, "quantity": "1"}))
    assert view(db, result["id"])["lines"][0]["unmet_quantity"] == "2"
    assert stock.view(db, first)["reserved_quantity"] == "2"
    updated = write(db, "preparation_update", _fill_versions(db, "preparation_update", {
        "preparation_id": result["id"], "action": "replenish", "note": "部分到货后补充预留"}))
    assert updated["lines"][0]["unmet_quantity"] == "1"
    assert len(updated["schedules"]) == 2
    assert stock.view(db, first)["reserved_quantity"] == "3"
    cancelled = write(db, "preparation_update", _fill_versions(db, "preparation_update", {
        "preparation_id": result["id"], "action": "cancel", "note": "课程取消"}))
    assert cancelled["status"] == "cancelled"
    assert stock.view(db, first)["reserved_quantity"] == "0"
    assert db.execute("SELECT status FROM purchase_orders WHERE id=?", (draft_id,)).fetchone()[0] == "ordered"


def test_preparation_repeated_decimal_demands_and_review_stock(db):
    from consumable_assistant.chat import _fill_versions

    item = make_item(db, quantity="0.3", unit="L")
    pending = make_item(db, quantity=20, unit="L", location="B2")
    db.execute("UPDATE stock_balances SET review_reason='待清点' WHERE item_id=?", (pending,))
    db.commit()
    lines = [{"item_ids": [item, pending], "action": action, "quantity": "0.2"}
             for action in ("borrow", "consume")]
    result = write(db, "preparation", _fill_versions(db, "preparation", preparation_data([item], lines=lines)))
    assert [line["reserved_quantity"] for line in result["lines"]] == ["0.2", "0.1"]
    assert [line["unmet_quantity"] for line in result["lines"]] == ["0", "0.1"]
    assert result["lines"][0]["locations"][1]["available_quantity"] == "0"
    assert stock.view(db, item)["reserved_quantity"] == "0.3"


def test_mixed_plan_rolls_back_all_steps(db):
    from consumable_assistant.chat import _fill_versions

    item = make_item(db, quantity=10)
    payload = _fill_versions(db, "mixed", {"operations": [
        {"kind": "stock", "payload": {"item_id": item, "action": "consume", "quantity": "4"}},
        {"kind": "stock", "payload": {"item_id": item, "action": "consume", "quantity": "7"}},
    ]})
    with pytest.raises(ValueError):
        write(db, "mixed", payload)
    assert stock.view(db, item)["ready_quantity"] == "10"
    assert db.execute("SELECT count(*) FROM stock_events").fetchone()[0] == 0


def test_preparation_revision_preserves_history_and_releases_old_stock(db):
    from consumable_assistant.chat import _fill_versions

    item = make_item(db, quantity=5)
    original = write(db, "preparation", _fill_versions(db, "preparation", preparation_data([item])))
    old_order = original["purchase_draft_ids"][0]
    write(db, "purchase", _fill_versions(db, "purchase", {"action": "place", "order_id": old_order,
          "owner": "采购教师", "expected_at": "2026-11-30T12:00:00+08:00"}))
    revised = write(db, "preparation_update", _fill_versions(db, "preparation_update", {
        "preparation_id": original["id"], "action": "revise", "people": 3, "note": "人数改为三人"}))
    assert revised["lines"][0]["required_quantity"] == "5"
    assert revised["lines"][0]["reserved_quantity"] == "5"
    assert revised["schedules"][0]["status"] == "cancelled"
    assert revised["schedules"][0]["retired"] == 1
    assert revised["purchases"][0]["status"] == "ordered"
    assert stock.view(db, item)["reserved_quantity"] == "5"
    assert db.execute("SELECT count(*) FROM preparation_revisions").fetchone()[0] == 1
    replenished = write(db, "preparation_update", _fill_versions(db, "preparation_update", {
        "preparation_id": original["id"], "action": "replenish", "note": "检查准备结果"}))
    assert replenished["lines"][0]["unmet_quantity"] == "0"
    assert len(replenished["schedules"]) == 2
    write(db, "execute_schedule", _fill_versions(db, "execute_schedule", {
        "schedule_id": revised["schedules"][1]["id"], "borrower": "实验教师"}))
    with pytest.raises(ValueError, match="实际取用"):
        write(db, "preparation_update", _fill_versions(db, "preparation_update", {
            "preparation_id": original["id"], "action": "revise", "people": 1, "note": "修改人数"}))
    assert stock.view(db, item)["borrowed_quantity"] == "5"


def test_preparation_revision_preserves_original_calculation_units(db):
    from consumable_assistant.chat import _fill_versions

    item = make_item(db, quantity=2, unit="盒", spec="50片/盒")
    write(db, "metadata", {"item_id": item, "version": 1, "conversions": {"片": "0.02"}})
    payload = preparation_data([item], people=10, lines=[{"item_ids": [item], "action": "consume",
                                                      "per_person": "1", "unit": "片"}])
    original = write(db, "preparation", _fill_versions(db, "preparation", payload))
    assert original["lines"][0]["required_quantity"] == "0.2"
    revised = write(db, "preparation_update", _fill_versions(db, "preparation_update", {
        "preparation_id": original["id"], "action": "revise", "people": 20, "note": "增加人数"}))
    assert revised["lines"][0]["required_quantity"] == "0.4"
    assert stock.view(db, item)["reserved_quantity"] == "0.4"


def test_form_preview_cross_device_confirmation_and_retry(db, monkeypatch):
    from urllib.parse import quote

    from consumable_assistant import server

    item = make_item(db)
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(server, "connect", lambda: connect(path))
    headers = {"X-Request-ID": uuid.uuid4().hex, "X-Actor": quote("发起教师"), "X-Device-ID": "desktop"}
    payload = {"item_id": item, "action": "consume", "quantity": "2"}
    with TestClient(server.app) as client:
        response = client.post("/api/actions/stock", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["status"] == "pending_confirmation"
        assert stock.view(db, item)["ready"] == "100"
        confirm = client.post(f"/api/chat/tasks/{preview['task_id']}/confirm",
                             headers={"X-Actor": quote("确认教师"), "X-Device-ID": "phone"},
                             json={"request_id": uuid.uuid4().hex, "plan_hash": preview["plan_hash"]})
        assert confirm.status_code == 200, confirm.text
        assert stock.view(db, item)["ready"] == "98"
        retry = client.post("/api/actions/stock", json=payload, headers=headers).json()
        assert retry["status"] == "completed"
        assert retry["result"] == confirm.json()["result"]
        task = db.execute("SELECT * FROM chat_tasks WHERE id=?", (preview["task_id"],)).fetchone()
        assert (task["initiator"], task["confirmer"], task["confirm_device"]) == ("发起教师", "确认教师", "phone")


@pytest.mark.parametrize('kind', ['template', 'location_details'])
def test_nonstock_plan_refreshes_changed_records_without_execution(db, kind):
    from consumable_assistant import chat
    from consumable_assistant.server import dispatch

    item = make_item(db)
    conversation = chat.new_conversation(db, 'desktop')['conversation_id']
    message_id = db.execute("INSERT INTO chat_messages(conversation_id,request_id,role,content) VALUES (?,?,'user','修改资料')", (conversation, uuid.uuid4().hex)).lastrowid
    if kind == 'template':
        template = dispatch(db, kind, {'name': '原模板', 'course': '化学', 'lines': [{'item_id': item, 'action': 'consume', 'fixed': '1'}]}, 'Teacher')['id']
        payload = {'template_id': template, 'name': '修改后模板', 'course': '化学', 'lines': [{'item_id': item, 'action': 'consume', 'fixed': '2'}]}
    else:
        dispatch(db, kind, {'lab': '实验室', 'location_code': 'A1', 'description': '原说明'}, 'Teacher')
        payload = {'lab': '实验室', 'location_code': 'A1', 'description': '修改后说明'}
    db.commit()
    preview = chat._business_task(db, conversation, message_id, uuid.uuid4().hex, {'kind': kind, 'payload': payload}, [], dispatch, 'Teacher', validate_references=False)
    db.commit()
    if kind == 'template':
        db.execute("UPDATE lesson_templates SET name='另一教师修改',version=version+1 WHERE id=?", (template,))
    else:
        db.execute("UPDATE location_details SET description='另一教师修改',version=version+1 WHERE lab='实验室' AND location_code='A1'")
    db.commit()
    task = db.execute('SELECT * FROM chat_tasks WHERE id=?', (preview['task_id'],)).fetchone()
    with pytest.raises(chat.PlanChanged) as changed:
        chat.confirm_task(db, task, 'Another teacher', 'phone', uuid.uuid4().hex, dispatch, expected_plan_hash=preview['plan_hash'])
    result = changed.value.response
    assert result['status'] == 'pending_confirmation'
    assert result['task_id'] != preview['task_id']
    assert result['previous_preview'] == preview['preview']
    assert stock.view(db, item)['ready'] == '100'
    if kind == 'template':
        assert db.execute('SELECT name FROM lesson_templates WHERE id=?', (template,)).fetchone()[0] == '另一教师修改'
    else:
        assert db.execute("SELECT description FROM location_details WHERE lab='实验室' AND location_code='A1'").fetchone()[0] == '另一教师修改'


def queued_preview(db):
    from consumable_assistant import chat, chat_runtime
    from consumable_assistant.server import dispatch, history_rows

    item = make_item(db)
    conversation = chat.new_conversation(db, 'desktop')['conversation_id']
    request = chat.ChatMessageRequest(message_id=uuid.uuid4().hex, text='预览消耗两个烧杯')
    queued = chat_runtime.enqueue(db, conversation, request, '发起教师', 'desktop')
    run = chat_runtime.claim(db, queued['run_id'])
    chat_runtime.execute_tool(db, run['id'], run['callback_token'], 'inventory_search',
                              {'query': '玻璃烧杯'}, dispatch, history_rows)
    preview = chat_runtime.execute_tool(db, run['id'], run['callback_token'], 'business_preview',
                                        {'kind': 'stock', 'payload': {'item_id': item, 'action': 'consume', 'quantity': '2'}}, dispatch, history_rows)
    assert preview['status'] == 'pending_confirmation'
    return item, conversation, request, run, preview


def test_cancelling_a_running_preview_revokes_tools_and_confirmation(db):
    from consumable_assistant import chat, chat_runtime
    from consumable_assistant.server import dispatch, history_rows

    item, _, _, run, preview = queued_preview(db)
    chat_runtime.cancel_run(db, run['id'])
    with pytest.raises(stock.Conflict, match='取消'):
        chat_runtime.execute_tool(db, run['id'], run['callback_token'], 'inventory_search', {}, dispatch, history_rows)
    task = db.execute('SELECT * FROM chat_tasks WHERE id=?', (preview['task_id'],)).fetchone()
    with pytest.raises(stock.Conflict):
        chat.confirm_task(db, task, '确认教师', 'phone', uuid.uuid4().hex, dispatch, expected_plan_hash=preview['plan_hash'])
    response = chat_runtime.finish(db, run, preview)
    assert response['status'] == 'cancelled'
    assert stock.view(db, item)['ready'] == '100'
    assert db.execute('SELECT callback_hash FROM chat_runs WHERE id=?', (run['id'],)).fetchone()[0] is None


def test_restart_preserves_queue_and_expires_interrupted_preview_without_replay(db):
    from consumable_assistant import chat, chat_runtime

    item, conversation, request, run, preview = queued_preview(db)
    next_request = chat.ChatMessageRequest(message_id=uuid.uuid4().hex, text='查询当前库存')
    queued = chat_runtime.enqueue(db, conversation, next_request, '另一教师', 'phone')
    chat_runtime.recover(db)
    assert db.execute('SELECT status FROM chat_runs WHERE id=?', (run['id'],)).fetchone()[0] == 'failed'
    assert db.execute('SELECT status FROM chat_tasks WHERE id=?', (preview['task_id'],)).fetchone()[0] == 'expired'
    assert chat_runtime.enqueue(db, conversation, request, '发起教师', 'desktop')['run_id'] == run['id']
    assert chat_runtime.claim(db, queued['run_id'])['message_id'] == queued['message_id']
    assert stock.view(db, item)['ready'] == '100'
    assert db.execute('SELECT COUNT(*) FROM stock_events').fetchone()[0] == 0


def test_two_simultaneous_confirmers_execute_one_business_change(db):
    import concurrent.futures

    from consumable_assistant import chat
    from consumable_assistant.server import dispatch

    item, _, _, _, preview = queued_preview(db)
    path = Path(db.execute('PRAGMA database_list').fetchone()[2])
    barrier = threading.Barrier(2)

    def confirm(device):
        with connect(path) as conn:
            task = conn.execute('SELECT * FROM chat_tasks WHERE id=?', (preview['task_id'],)).fetchone()
            barrier.wait(timeout=5)
            return chat.confirm_task(conn, task, device, device, uuid.uuid4().hex, dispatch,
                                     expected_plan_hash=preview['plan_hash'])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(confirm, ('phone-a', 'phone-b')))
    assert first == second
    assert stock.view(db, item)['ready'] == '98'
    assert db.execute("SELECT COUNT(*) FROM stock_events WHERE action='consume'").fetchone()[0] == 1


def test_expired_preview_cannot_execute_after_reconnection(db):
    from consumable_assistant import chat
    from consumable_assistant.server import dispatch

    item, _, _, _, preview = queued_preview(db)
    db.execute("UPDATE chat_tasks SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (preview['task_id'],))
    db.commit()
    task = db.execute('SELECT * FROM chat_tasks WHERE id=?', (preview['task_id'],)).fetchone()
    with pytest.raises(ValueError, match='三十分钟'):
        chat.confirm_task(db, task, '教师', 'phone', uuid.uuid4().hex, dispatch, expected_plan_hash=preview['plan_hash'])
    assert stock.view(db, item)['ready'] == '100'


def test_shared_event_stream_resumes_after_the_last_delivered_event(db, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from consumable_assistant import chat, chat_runtime, server

    conversation = chat.new_conversation(db, 'desktop')['conversation_id']
    ids = [chat_runtime.event(db, conversation, 'message', {'number': number}) for number in range(3)]
    db.commit()
    path = Path(db.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr(server, 'connect', lambda: connect(path))

    async def connected():
        return False

    async def read_one():
        request = SimpleNamespace(headers={'x-device-id': 'another-phone', 'last-event-id': str(ids[1])}, is_disconnected=connected)
        response = await server.chat_events(conversation, request, after=ids[0])
        try:
            return await anext(response.body_iterator)
        finally:
            await response.body_iterator.aclose()

    event = asyncio.run(read_one())
    assert event.startswith(f'id: {ids[2]}\nevent: message\n')
    assert '"number":2' in event


def test_marked_notification_test_targets_one_device_and_preserves_inventory(db, monkeypatch):
    from consumable_assistant import notifications
    from scripts.check_device_notification import queue

    item = make_item(db)
    for device in ('iphone', 'android'):
        db.execute('INSERT INTO push_subscriptions(endpoint,subscription,actor,device_id) VALUES (?,?,?,?)',
                   ('https://push.apple.com/' + device, '{}', 'Teacher', device))
    db.commit()
    queued = queue(db, 'iphone', 0)
    assert db.execute('SELECT COUNT(*) FROM push_deliveries').fetchone()[0] == 1
    assert db.execute('SELECT item_id FROM notification_events WHERE id=?', (queued['event_id'],)).fetchone()[0] is None
    path = Path(db.execute('PRAGMA database_list').fetchone()[2])
    monkeypatch.setattr(notifications, 'connect', lambda: connect(path))
    monkeypatch.setenv('VAPID_PRIVATE_KEY', 'test-key')
    monkeypatch.setenv('VAPID_SUBJECT', 'https://example.org/contact')
    sent = []
    monkeypatch.setattr(notifications, 'send', lambda subscription, **options: sent.append(options))
    notifications.tick()
    assert sent == [{'test': True}]
    assert db.execute('SELECT status FROM push_deliveries').fetchone()[0] == 'sent'
    assert stock.view(db, item)['ready'] == '100'
    assert db.execute('SELECT COUNT(*) FROM stock_events').fetchone()[0] == 0
