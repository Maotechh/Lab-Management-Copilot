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


def test_location_totals_exclude_invalid_legacy_quantity(db):
    from consumable_assistant.services import list_locations, preview_location

    item = make_item(db, quantity=-2)
    db.execute("DELETE FROM stock_balances WHERE item_id=?", (item,))
    db.commit()
    assert list_locations(db)[0]["total_quantity"] == 0
    assert preview_location(db, "实验室", "A1")["total_quantity"] == 0
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


def test_ai_retries_and_auth_failure(monkeypatch):
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
            if len(calls) < 3:
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
    monkeypatch.setattr(genai.httpx, "Client", Client)
    monkeypatch.setattr(genai.time, "sleep", lambda _: None)
    monkeypatch.setattr(genai, "record_health", lambda *args: None)
    assert genai.parse_inventory_message("test")["operations"][0]["quantity"] == 1
    assert len(calls) == 3


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
