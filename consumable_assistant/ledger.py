from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, DefaultContext, InvalidOperation, getcontext
from typing import Any

from .normalization import normalize_text

STATES = ("ready", "cleaning", "repair", "quarantine")
DefaultContext.prec = 50
getcontext().prec = 50


class Conflict(ValueError):
    pass


def number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else 0))
    except InvalidOperation as exc:
        raise ValueError("数量必须是有效数字") from exc
    if (
        not result.is_finite()
        or abs(result) > Decimal("1e12")
        or result.as_tuple().exponent < -24
    ):
        raise ValueError("数量超出允许范围或精度")
    return result


def text(value: Any) -> str:
    return format(number(value).normalize(), "f")


def required(data: dict, key: str) -> str:
    result = str(data.get(key) or "").strip()
    if not result:
        raise ValueError(f"请填写 {key}")
    return result


def moment(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("时间需要包含时区")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, TypeError) as exc:
        raise ValueError("请填写有效的日期时间及其时区") from exc


def ensure_balance(conn: sqlite3.Connection, item_id: int) -> dict:
    item = conn.execute(
        "SELECT * FROM inventory_items WHERE id=?", (item_id,)
    ).fetchone()
    if not item:
        raise ValueError("耗材不存在")
    balance = conn.execute(
        "SELECT * FROM stock_balances WHERE item_id=?", (item_id,)
    ).fetchone()
    if not balance:
        old = number(item["quantity"])
        legacy = conn.execute(
            "SELECT COUNT(*) FROM inventory_transactions WHERE item_id=? AND action IN ('borrow','return')",
            (item_id,),
        ).fetchone()[0]
        reason = (
            "原始数量为负数，请实物清点"
            if old < 0
            else ("历史借还需要核对" if legacy else None)
        )
        conn.execute(
            "INSERT INTO stock_balances(item_id,ready,quarantine,review_reason,original_quantity) VALUES (?,?,?,?,?)",
            (
                item_id,
                text(max(old, 0)) if not reason else "0",
                text(max(old, 0)) if reason else "0",
                reason,
                str(item["quantity"]),
            ),
        )
        balance = conn.execute(
            "SELECT * FROM stock_balances WHERE item_id=?", (item_id,)
        ).fetchone()
    return {**dict(item), **dict(balance)}


def initialize_balances(conn: sqlite3.Connection) -> None:
    for row in conn.execute("SELECT id FROM inventory_items").fetchall():
        ensure_balance(conn, row["id"])
    for row in conn.execute(
        "SELECT t.id,i.item_name,i.spec,i.unit,i.lab,i.location_code,i.category FROM inventory_transactions t JOIN inventory_items i ON i.id=t.item_id LEFT JOIN legacy_snapshots s ON s.transaction_id=t.id WHERE s.transaction_id IS NULL"
    ).fetchall():
        snapshot = dict(row)
        conn.execute(
            "INSERT INTO legacy_snapshots VALUES (?,?)",
            (snapshot.pop("id"), json.dumps(snapshot, ensure_ascii=False)),
        )
    conn.commit()


def reserved(
    conn: sqlite3.Connection,
    item_id: int,
    start: str | None = None,
    end: str | None = None,
    exclude: int = 0,
) -> Decimal:
    query = """SELECT r.quantity,r.action,s.starts_at,s.ends_at FROM reservations r JOIN schedules s ON s.id=r.schedule_id
               WHERE r.item_id=? AND s.status='reserved' AND s.id!=?"""
    rows = conn.execute(query, (item_id, exclude)).fetchall()
    if start is None:
        # Without a selected time, keep all promised stock unavailable for ad-hoc use.
        return sum((number(row["quantity"]) for row in rows), Decimal(0))
    changes = []
    consumed = Decimal(0)
    for row in rows:
        if row["action"] == "consume":
            consumed += number(row["quantity"])
            continue
        left, right = max(start, row["starts_at"]), min(end or start, row["ends_at"])
        if left < right:
            changes.extend(
                [(left, number(row["quantity"])), (right, -number(row["quantity"]))]
            )
    current = peak = Decimal(0)
    for _, delta in sorted(changes, key=lambda entry: (entry[0], entry[1])):
        current += delta
        peak = max(peak, current)
    return peak + consumed


def view(
    conn: sqlite3.Connection,
    item_id: int,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    item = ensure_balance(conn, item_id)
    borrowed = sum(
        (
            number(row[0])
            for row in conn.execute(
                "SELECT outstanding FROM loans WHERE item_id=?", (item_id,)
            )
        ),
        Decimal(0),
    )
    held = reserved(conn, item_id, start, end)
    ready = number(item["ready"])
    return {
        **item,
        "quantity": float(ready),
        "quantity_text": text(ready),
        "ready_quantity": text(ready),
        "borrowed_quantity": text(borrowed),
        "reserved_quantity": text(held),
        "available_quantity": text(max(ready - held, 0)),
        "owned_quantity": text(
            sum((number(item[state]) for state in STATES), borrowed)
        ),
        "unit_conversions": json.loads(item["conversions"]),
    }


def convert(item: dict, quantity: Any, unit: str | None) -> Decimal:
    amount = number(quantity)
    if unit and normalize_text(unit) != normalize_text(item.get("unit")):
        conversions = json.loads(item["conversions"])
        factor = conversions.get(unit)
        if factor is None:
            raise ValueError(
                f"单位不一致：库存单位为 {item.get('unit') or '未设置'}，请先确认包装换算"
            )
        amount *= number(factor)
    return amount


def alerts_changed(conn: sqlite3.Connection, item_id: int) -> None:
    item = ensure_balance(conn, item_id)
    low = int(
        item["active"]
        and number(item["threshold"]) > 0
        and number(item["ready"]) - reserved(conn, item_id) < number(item["threshold"])
    )
    previous = conn.execute(
        "SELECT * FROM alert_states WHERE item_id=?", (item_id,)
    ).fetchone()
    if previous and previous["is_low"] == low:
        return
    event = conn.execute(
        "INSERT INTO notification_events(item_id,kind) VALUES (?,?)",
        (item_id, "low_stock" if low else "stock_updated"),
    ).lastrowid
    conn.execute(
        "INSERT INTO alert_states(item_id,is_low,event_id) VALUES (?,?,?) ON CONFLICT(item_id) DO UPDATE SET is_low=excluded.is_low,event_id=excluded.event_id,acknowledged=0,snooze_until=NULL",
        (item_id, low, event),
    )
    if low:
        conn.execute(
            "INSERT OR IGNORE INTO push_deliveries(event_id,subscription_id) SELECT ?,id FROM push_subscriptions WHERE active=1",
            (event,),
        )


def event(
    conn: sqlite3.Connection,
    item_id: int,
    action: str,
    changes: dict,
    actor: str,
    note: str = "",
    related_id: int | None = None,
) -> int:
    snapshot = dict(
        conn.execute(
            "SELECT item_name,spec,unit,lab,location_code,category FROM inventory_items WHERE id=?",
            (item_id,),
        ).fetchone()
    )
    snapshot["quantity_after"] = ensure_balance(conn, item_id)["ready"]
    return int(
        conn.execute(
            "INSERT INTO stock_events(item_id,action,changes,snapshot,note,actor,related_id) VALUES (?,?,?,?,?,?,?)",
            (
                item_id,
                action,
                json.dumps(changes),
                json.dumps(snapshot, ensure_ascii=False),
                note,
                actor,
                related_id,
            ),
        ).lastrowid
    )


def update_states(
    conn: sqlite3.Connection, item: dict, changes: dict[str, Any]
) -> None:
    values = {
        state: number(item[state]) + number(changes.get(state, 0)) for state in STATES
    }
    if any(value < 0 for value in values.values()):
        raise ValueError("库存数量不足或状态数量不一致")
    conn.execute(
        "UPDATE stock_balances SET ready=?,cleaning=?,repair=?,quarantine=?,version=version+1 WHERE item_id=?",
        (*[text(values[state]) for state in STATES], item["item_id"]),
    )
    conn.execute(
        "UPDATE inventory_items SET quantity=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (float(values["ready"]), item["item_id"]),
    )
    alerts_changed(conn, item["item_id"])


def check_version(item: dict, expected: Any) -> None:
    if expected is None or int(expected) != item["version"]:
        raise Conflict("库存已经变化，请刷新预览后重新确认")


def write(
    conn: sqlite3.Connection,
    request_id: str,
    actor: str,
    device_id: str,
    payload: dict,
    fn: Callable[[], dict],
) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = write_in_transaction(
            conn, request_id, actor, device_id, payload, fn
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def write_in_transaction(
    conn: sqlite3.Connection,
    request_id: str,
    actor: str,
    device_id: str,
    payload: dict,
    fn: Callable[[], dict],
) -> dict:
    if (
        not request_id
        or len(request_id) > 100
        or not actor.strip()
        or not device_id.strip()
    ):
        raise ValueError("请登记操作人，并提供请求编号和设备编号")
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    old = conn.execute(
        "SELECT * FROM operation_requests WHERE request_id=?", (request_id,)
    ).fetchone()
    if old:
        if (
            old["fingerprint"] != fingerprint
            or old["actor"] != actor
            or old["device_id"] != device_id
        ):
            raise Conflict("请求编号已经用于其他操作")
        return json.loads(old["result"])
    first = conn.execute("SELECT COALESCE(MAX(id),0) FROM stock_events").fetchone()[0]
    result = fn()
    conn.execute(
        "UPDATE stock_events SET request_id=?,device_id=? WHERE id>?",
        (request_id, device_id, first),
    )
    conn.execute(
        "INSERT INTO operation_requests(request_id,fingerprint,result,actor,device_id) VALUES (?,?,?,?,?)",
        (
            request_id,
            fingerprint,
            json.dumps(result, ensure_ascii=False, default=str),
            actor,
            device_id,
        ),
    )
    return result


def operate(
    conn: sqlite3.Connection, op: dict, actor: str, checked: bool = False
) -> dict:
    item_id = int(required(op, "item_id"))
    item = ensure_balance(conn, item_id)
    if not item["active"]:
        raise ValueError("耗材已停用")
    if not checked:
        check_version(item, op.get("version"))
    action = required(op, "action")
    if op.get("quantity") is None or op.get("quantity") == "":
        raise ValueError("请填写操作数量")
    quantity = convert(item, op.get("quantity"), op.get("unit"))
    if quantity < 0 or (
        quantity == 0 and action not in ("adjust", "count", "threshold")
    ):
        raise ValueError("请填写有效的非负数量")
    changes: dict[str, Any] = {}
    related = None
    note = str(op.get("note") or "")
    if item["review_reason"] and action not in ("count", "threshold"):
        raise ValueError("此物品需要先进行实物清点")
    if action in ("inbound", "consume", "adjust", "count"):
        delta = (
            quantity
            if action == "inbound"
            else -quantity
            if action == "consume"
            else quantity - number(item["ready"])
        )
        if delta < 0 and number(item["ready"]) + delta < reserved(
            conn, item_id, op.get("reservation_start"), op.get("reservation_end")
        ):
            raise ValueError("数量已被实验安排预留，请先调整安排")
        changes = {"ready": text(delta)}
        if action in ("adjust", "count"):
            note = required(op, "note")
            changes["quarantine"] = (
                text(-number(item["quarantine"])) if item["review_reason"] else "0"
            )
            conn.execute(
                "UPDATE stock_balances SET review_reason=NULL WHERE item_id=?",
                (item_id,),
            )
            changes["review_reason_before"] = item["review_reason"]
    elif action == "threshold":
        changes = {
            "threshold_before": str(item["threshold"]),
            "threshold_after": text(quantity),
        }
        conn.execute(
            "UPDATE inventory_items SET threshold=? WHERE id=?",
            (float(quantity), item_id),
        )
    elif action == "borrow":
        if quantity > number(item["ready"]) - reserved(
            conn, item_id, op.get("reservation_start"), op.get("reservation_end")
        ):
            raise ValueError("可借数量不足，部分库存已预留")
        related = int(
            conn.execute(
                "INSERT INTO loans(item_id,quantity,outstanding,borrower,purpose,destination,due_at,actor,schedule_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    text(quantity),
                    text(quantity),
                    required(op, "borrower"),
                    required(op, "purpose"),
                    required(op, "destination"),
                    moment(required(op, "due_at")),
                    actor,
                    op.get("schedule_id"),
                ),
            ).lastrowid
        )
        changes = {"ready": text(-quantity), "borrowed": text(quantity)}
    elif action == "return":
        related = int(op.get("loan_id") or 0)
        loan = conn.execute(
            "SELECT * FROM loans WHERE id=? AND item_id=?", (related, item_id)
        ).fetchone()
        if not loan or quantity > number(loan["outstanding"]):
            raise ValueError("归还数量超过对应借用记录的未归还数量")
        state = required(op, "return_state")
        if state not in (*STATES, "damaged", "lost"):
            raise ValueError("请选择归还后的物品状态")
        conn.execute(
            "UPDATE loans SET outstanding=? WHERE id=?",
            (text(number(loan["outstanding"]) - quantity), related),
        )
        changes = {"borrowed": text(-quantity)}
        if state in STATES:
            changes[state] = text(quantity)
    elif action == "state":
        source, target = required(op, "from_state"), required(op, "to_state")
        if (
            source not in STATES
            or target not in (*STATES, "damaged", "lost")
            or source == target
        ):
            raise ValueError("物品状态无效")
        if source == "ready" and quantity > number(item["ready"]) - reserved(
            conn, item_id
        ):
            raise ValueError("部分库存已被实验安排预留")
        changes = {source: text(-quantity)}
        if target in STATES:
            changes[target] = text(quantity)
    else:
        raise ValueError("不支持的库存动作")
    update_states(conn, item, changes)
    event_id = event(conn, item_id, action, changes, actor, note, related)
    if action in ("count", "adjust"):
        conn.execute(
            "INSERT INTO stock_counts(item_id,before_quantity,actual_quantity,reason,actor,event_id) VALUES (?,?,?,?,?,?)",
            (item_id, item["ready"], text(quantity), note, actor, event_id),
        )
    return {
        "id": event_id,
        "item_id": item_id,
        "loan_id": related,
        "action": action,
        "quantity_after": float(ensure_balance(conn, item_id)["ready"]),
        "item": view(conn, item_id),
    }


def undo(conn: sqlite3.Connection, event_id: int, actor: str, version: int) -> dict:
    old = conn.execute("SELECT * FROM stock_events WHERE id=?", (event_id,)).fetchone()
    if (
        not old
        or old["reversed_by"]
        or old["action"]
        not in ("inbound", "consume", "threshold", "adjust", "count", "state", "borrow")
    ):
        raise ValueError("此记录不能直接撤销，请使用对应业务的更正操作")
    item = ensure_balance(conn, old["item_id"])
    check_version(item, version)
    changes = json.loads(old["changes"])
    if old["action"] == "borrow":
        loan = conn.execute(
            "SELECT * FROM loans WHERE id=?", (old["related_id"],)
        ).fetchone()
        if loan["outstanding"] != loan["quantity"]:
            raise ValueError("借用已经发生归还或破损处理，不能直接撤销")
        conn.execute("UPDATE loans SET outstanding='0' WHERE id=?", (loan["id"],))
    if (
        old["action"] == "inbound"
        and conn.execute(
            "SELECT 1 FROM stock_events WHERE item_id=? AND id>? AND action NOT IN ('inbound','threshold','metadata','alert_acknowledge','alert_snooze','alert_remind') LIMIT 1",
            (old["item_id"], event_id),
        ).fetchone()
    ):
        raise Conflict("入库后已有使用、借还、转移或预留，请通过清点更正")
    if (
        old["action"] in ("state", "count", "adjust")
        and conn.execute(
            "SELECT 1 FROM stock_events WHERE item_id=? AND id>? LIMIT 1",
            (old["item_id"], event_id),
        ).fetchone()
    ):
        raise Conflict("后续已有相关操作，请通过实物清点或状态转移更正")
    inverse = {
        key: text(-number(value))
        for key, value in changes.items()
        if key in (*STATES, "borrowed")
    }
    if "threshold_after" in changes:
        if number(item["threshold"]) != number(changes["threshold_after"]):
            raise Conflict("预警数量后来已改变，请重新设置")
        conn.execute(
            "UPDATE inventory_items SET threshold=? WHERE id=?",
            (float(number(changes["threshold_before"])), old["item_id"]),
        )
    if number(item["ready"]) + number(inverse.get("ready", 0)) < reserved(
        conn, old["item_id"]
    ):
        raise ValueError("撤销会影响已预留库存")
    update_states(conn, item, inverse)
    if changes.get("review_reason_before"):
        conn.execute(
            "UPDATE stock_balances SET review_reason=? WHERE item_id=?",
            (changes["review_reason_before"], old["item_id"]),
        )
    new = event(
        conn, old["item_id"], "undo", inverse, actor, f"撤销记录 {event_id}", event_id
    )
    conn.execute("UPDATE stock_events SET reversed_by=? WHERE id=?", (new, event_id))
    return {"ok": True, "id": new, "item": view(conn, old["item_id"])}
