"""Persistent experiment demand, partial reservations and procurement drafts.

All mutations run inside the caller's preview savepoint or confirmation transaction.
A receipt never allocates stock; replenishment is a separate confirmed operation.
"""
from __future__ import annotations

import json
from decimal import Decimal

from . import ledger as stock
from . import workflows as flow


def _allocate(conn, data: dict, sources: list[dict]) -> tuple[list, list]:
    start, end = data["starts_at"], data["ends_at"]
    versions = data.get("item_versions", {})
    # Match schedule's conservative rule when a candidate serves both actions.
    consumption_ids = {int(value) for line in sources if line["action"] == "consume"
                       for value in line["item_ids"]}
    assigned_totals: dict[int, Decimal] = {}
    allocations, demands = [], []
    for source in sources:
        items = [stock.ensure_balance(conn, int(value))
                 for value in dict.fromkeys(source["item_ids"])]
        if not items:
            raise ValueError("每个需求至少需要一个明确的物品编号")
        first = items[0]
        for item in items:
            stock.check_version(item, versions.get(str(item["item_id"])))
            if any((item.get(key) or "") != (first.get(key) or "")
                   for key in ("normalized_name", "spec", "brand", "unit")):
                raise ValueError("同一条目只能分配名称、规格、品牌和单位相同的物品")
        if "unmet_quantity" in source:
            required = stock.number(source["unmet_quantity"])
        else:
            calculated = flow.template_lines({**data, "lines": [
                {**source, "item_id": first["item_id"]}]} )[0]
            required = stock.convert(first, calculated["quantity"], source.get("unit"))
        remaining, located = required, []
        for item in items:
            item_id = item["item_id"]
            held = stock.reserved(conn, item_id,
                                  None if item_id in consumption_ids else start, end)
            available = max(Decimal(0), stock.number(item["ready"]) - held
                            - assigned_totals.get(item_id, Decimal(0)))
            eligible = bool(item["active"] and not item["review_reason"])
            assigned = min(remaining, available) if eligible else Decimal(0)
            located.append({"item_id": item_id, "lab": item["lab"],
                            "location_code": item["location_code"],
                            "available_quantity": stock.text(available if eligible else 0),
                            "quantity": stock.text(assigned),
                            "review_reason": item["review_reason"], "active": item["active"]})
            if assigned:
                assigned_totals[item_id] = assigned_totals.get(item_id, Decimal(0)) + assigned
                allocations.append({"item_id": item_id, "action": source["action"],
                                    "quantity": stock.text(assigned), "unit": item["unit"],
                                    "version": item["version"]})
                remaining -= assigned
        prior = stock.number(source.get("reserved_quantity", 0))
        demands.append({**source, "item_name": first["item_name"], "spec": first["spec"],
                        "calculation_unit": source.get("calculation_unit", source.get("unit")),
                        "unit": first["unit"],
                        "required_quantity": source.get("required_quantity", stock.text(required)),
                        "reserved_quantity": stock.text(prior + required - remaining),
                        "unmet_quantity": stock.text(remaining),
                        "locations": source.get("locations", []) + located})
    return allocations, demands


def _schedule(conn, data, allocations, actor):
    if not allocations:
        return None
    return flow.schedule(conn, {**data, "lines": allocations}, actor)["id"]


def _draft(conn, line, data, actor, quantity=None):
    item = stock.ensure_balance(conn, line["item_ids"][0])
    result = flow.purchase(conn, {
        "action": "draft", "item_id": item["item_id"],
        "quantity": quantity or line["unmet_quantity"], "owner": actor,
        "version": item["version"], "note": f"实验准备：{data['name']}"}, actor)
    line["purchase_draft_id"] = result["id"]


def create(conn, data: dict, actor: str) -> dict:
    start = stock.moment(stock.required(data, "starts_at"))
    end = stock.moment(stock.required(data, "ends_at"))
    if start >= end:
        raise ValueError("结束时间必须晚于开始时间")
    data = {**data, "starts_at": start, "ends_at": end}
    allocations, demands = _allocate(conn, data, data["lines"])
    schedule_id = _schedule(conn, data, allocations, actor)
    for line in demands:
        if stock.number(line["unmet_quantity"]):
            _draft(conn, line, data, actor)
    status = "awaiting_procurement" if any(stock.number(line["unmet_quantity"])
                                           for line in demands) else "reserved"
    preparation_id = conn.execute(
        "INSERT INTO preparations(name,course,venue,starts_at,ends_at,people,group_size,lines,"
        "schedule_id,actor,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (data["name"], data["course"], data["venue"], start, end, data.get("people", 0),
         data.get("group_size", 1), json.dumps(demands, ensure_ascii=False), schedule_id, actor, status),
    ).lastrowid
    if schedule_id:
        conn.execute("INSERT INTO preparation_schedules(preparation_id,schedule_id) VALUES (?,?)", (preparation_id, schedule_id))
    for line in demands:
        if line.get("purchase_draft_id"):
            conn.execute("INSERT INTO preparation_purchases VALUES (?,?)", (preparation_id, line["purchase_draft_id"]))
    return view(conn, preparation_id)


def view(conn, preparation_id):
    row = conn.execute("SELECT * FROM preparations WHERE id=?", (preparation_id,)).fetchone()
    if not row:
        raise ValueError("实验准备记录不存在")
    result = dict(row)
    result["lines"] = json.loads(result["lines"])
    result["schedules"] = [dict(row) for row in conn.execute(
        "SELECT s.id,s.status,s.version,p.retired FROM schedules s JOIN preparation_schedules p "
        "ON p.schedule_id=s.id WHERE p.preparation_id=? ORDER BY s.id", (preparation_id,))]
    result["purchase_draft_ids"] = [line["purchase_draft_id"] for line in result["lines"]
                                    if line.get("purchase_draft_id")]
    result["purchases"] = [dict(order) for order in conn.execute(
        "SELECT o.* FROM purchase_orders o JOIN preparation_purchases p ON p.order_id=o.id "
        "WHERE p.preparation_id=? ORDER BY o.id", (preparation_id,))]
    return {"ok": True, "type": "preparation", **result}


def _cancel_reservations(conn, current, actor, note):
    for row in current["schedules"]:
        if row["status"] == "reserved":
            conn.execute("UPDATE schedules SET status='cancelled',version=version+1 WHERE id=?", (row["id"],))
            for entry in conn.execute("SELECT item_id FROM reservations WHERE schedule_id=?", (row["id"],)):
                stock.event(conn, entry["item_id"], "cancel_reservation", {}, actor, note, row["id"])
                stock.alerts_changed(conn, entry["item_id"])


def _sync_drafts(conn, current, demands, actor, note):
    # Keep actual orders intact, and account for outstanding quantities before
    # creating replacement drafts. No ordered quantity counts as physical stock.
    pending = {}
    for order in current["purchases"]:
        if order["status"] == "draft":
            flow.purchase(conn, {"action": "cancel", "order_id": order["id"],
                                 "order_version": order["version"], "note": note}, actor)
        elif order["status"] == "ordered":
            item = stock.ensure_balance(conn, order["item_id"])
            key = tuple(item.get(field) or "" for field in ("normalized_name", "spec", "brand", "unit"))
            pending[key] = pending.get(key, Decimal(0)) + stock.number(order["quantity"]) - stock.number(order["received"])
    for line in demands:
        line.pop("purchase_draft_id", None)
        item = stock.ensure_balance(conn, line["item_ids"][0])
        key = tuple(item.get(field) or "" for field in ("normalized_name", "spec", "brand", "unit"))
        unmet = stock.number(line["unmet_quantity"])
        ordered = min(unmet, pending.get(key, Decimal(0)))
        pending[key] = pending.get(key, Decimal(0)) - ordered
        line["ordered_pending_quantity"] = stock.text(ordered)
        if unmet > ordered:
            _draft(conn, line, current, actor, stock.text(unmet - ordered))
            conn.execute("INSERT INTO preparation_purchases VALUES (?,?)", (current["id"], line["purchase_draft_id"]))


def update(conn, data: dict, actor: str) -> dict:
    current = view(conn, int(data["preparation_id"]))
    if current["version"] != data.get("version") or current["status"] == "cancelled":
        raise stock.Conflict("实验准备记录已经变化，请重新预览")
    note = stock.required(data, "note")
    changed_fields = {"name", "course", "venue", "starts_at", "ends_at", "people", "group_size", "lines"}
    if data["action"] != "revise" and changed_fields.intersection(data):
        raise ValueError("修改需求、人数或时间必须使用修改准备操作")
    conn.execute("INSERT INTO preparation_revisions(preparation_id,version,actor,note,snapshot) VALUES (?,?,?,?,?)",
                 (current["id"], current["version"], actor, note, json.dumps(current, ensure_ascii=False)))
    if data["action"] == "cancel":
        if any(row["status"] == "executed" for row in current["schedules"]):
            raise ValueError("已有实际取用记录，不能取消整个准备。请分别处理消耗和借还记录")
        _cancel_reservations(conn, current, actor, note)
        # Only unplaced drafts are cancelled. Actual purchase records remain visible.
        for order in current["purchases"]:
            if order["status"] == "draft":
                flow.purchase(conn, {"action": "cancel", "order_id": order["id"],
                                     "order_version": order["version"], "note": note}, actor)
        conn.execute("UPDATE preparations SET status='cancelled',version=version+1 WHERE id=?", (current["id"],))
    elif data["action"] == "replenish":
        if any(row["status"] == "cancelled" and not row["retired"] for row in current["schedules"]):
            raise ValueError("有关联预留已被取消，请重新建立包含完整需求的准备计划")
        allocations, demands = _allocate(conn, {**current, **data}, current["lines"])
        schedule_id = _schedule(conn, current, allocations, actor)
        if schedule_id:
            conn.execute("INSERT INTO preparation_schedules(preparation_id,schedule_id) VALUES (?,?)", (current["id"], schedule_id))
        _sync_drafts(conn, current, demands, actor, note)
        status = "awaiting_procurement" if any(stock.number(line["unmet_quantity"])
                                               for line in demands) else "reserved"
        conn.execute("UPDATE preparations SET lines=?,status=?,version=version+1 WHERE id=?",
                     (json.dumps(demands, ensure_ascii=False), status, current["id"]))
    elif data["action"] == "revise":
        if any(row["status"] == "executed" for row in current["schedules"]):
            raise ValueError("已有实际取用记录，不能修改原准备需求。请建立新的准备并关联原记录")
        _cancel_reservations(conn, current, actor, note)
        conn.execute("UPDATE preparation_schedules SET retired=1 WHERE preparation_id=?", (current["id"],))
        revised = {**current, **data}
        revised["starts_at"] = stock.moment(revised["starts_at"])
        revised["ends_at"] = stock.moment(revised["ends_at"])
        if revised["starts_at"] >= revised["ends_at"]:
            raise ValueError("结束时间必须晚于开始时间")
        fields = {"item_ids", "action", "unit", "quantity", "per_person", "per_group", "fixed", "spare", "source"}
        sources = [{key: value for key, value in line.items() if key in fields}
                   for line in revised["lines"]]
        if "lines" not in data:
            for source, original in zip(sources, current["lines"], strict=True):
                source["unit"] = original.get("calculation_unit", original.get("unit"))
        allocations, demands = _allocate(conn, revised, sources)
        schedule_id = _schedule(conn, revised, allocations, actor)
        if schedule_id:
            conn.execute("INSERT INTO preparation_schedules(preparation_id,schedule_id) VALUES (?,?)", (current["id"], schedule_id))
        _sync_drafts(conn, {**current, "name": revised["name"]}, demands, actor, note)
        status = "awaiting_procurement" if any(stock.number(line["unmet_quantity"]) for line in demands) else "reserved"
        conn.execute("UPDATE preparations SET name=?,course=?,venue=?,starts_at=?,ends_at=?,people=?,group_size=?,lines=?,schedule_id=?,status=?,version=version+1 WHERE id=?",
                     (revised["name"], revised["course"], revised["venue"], revised["starts_at"], revised["ends_at"],
                      revised["people"], revised["group_size"], json.dumps(demands, ensure_ascii=False), schedule_id, status, current["id"]))
    else:
        raise ValueError("实验准备操作无效")
    return view(conn, current["id"])
