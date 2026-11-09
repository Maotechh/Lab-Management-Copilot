"""Procurement of specified materials with no inventory identity yet."""
from . import ledger as stock
from . import workflows as flow
from .matching import same_material


def operate(conn, data, actor):
    row = conn.execute("SELECT * FROM procurement_demands WHERE id=?", (data["demand_id"],)).fetchone()
    if not row or row["version"] != data.get("version") or row["status"] not in {"draft", "ordered"}:
        raise stock.Conflict("采购需求已经变化或已完成，请重新查询并预览")
    action = data["action"]
    if action == "place":
        if row["status"] != "draft":
            raise ValueError("只有采购草案可以登记订购")
        conn.execute("UPDATE procurement_demands SET status='ordered',actor=?,expected_at=?,version=version+1 WHERE id=?",
                     (stock.required(data, "owner"), stock.moment(stock.required(data, "expected_at")), row["id"]))
    elif action == "cancel":
        conn.execute("UPDATE procurement_demands SET status='cancelled',note=?,version=version+1 WHERE id=?",
                     (stock.required(data, "note"), row["id"]))
    elif action == "receive":
        if row["status"] != "ordered":
            raise ValueError("采购草案需要先登记订购，才能登记到货")
        quantity = stock.number(data.get("quantity"))
        if quantity <= 0 or quantity > stock.number(row["quantity"]) - stock.number(row["received"]):
            raise ValueError("到货数量必须大于零且不超过尚未到货数量")
        lab, location = stock.required(data, "lab"), stock.required(data, "location_code")
        items = [dict(item) for item in conn.execute("SELECT * FROM inventory_items WHERE lab=? AND location_code=? AND unit=?", (lab, location, row["unit"]))
                 if same_material(row["item_name"], row["spec"], item["item_name"], item["spec"])]
        if len(items) > 1:
            raise ValueError("目标位置存在多个相同规格记录，请先核对物品资料")
        if items:
            item_id = items[0]["id"]
        else:
            item_id = flow.create_item(conn, {"item_name": row["item_name"], "spec": row["spec"], "unit": row["unit"],
                                             "lab": lab, "location_code": location, "category": data.get("category")})
            stock.event(conn, item_id, "create", {}, actor, f"采购需求 {row['id']} 实际到货")
        receipt = stock.operate(conn, {"item_id": item_id, "action": "inbound", "quantity": stock.text(quantity),
                                       "note": f"采购需求 {row['id']} 实际到货"}, actor, checked=True)
        conn.execute("UPDATE stock_events SET action='purchase_receive',related_id=? WHERE id=?", (row["id"], receipt["id"]))
        conn.execute("INSERT INTO procurement_receipts(demand_id,item_id,quantity,event_id) VALUES (?,?,?,?)",
                     (row["id"], item_id, stock.text(quantity), receipt["id"]))
        total = stock.number(row["received"]) + quantity
        conn.execute("UPDATE procurement_demands SET received=?,status=?,version=version+1 WHERE id=?",
                     (stock.text(total), "received" if total == stock.number(row["quantity"]) else "ordered", row["id"]))
    else:
        raise ValueError("采购操作无效")
    return {"ok": True, "type": "procurement", **dict(conn.execute("SELECT * FROM procurement_demands WHERE id=?", (row["id"],)).fetchone())}
