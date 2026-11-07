from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from decimal import Decimal
from pathlib import Path

from . import ledger as stock
from .importer import add_aliases
from .normalization import normalize_text, stable_key


def create_item(conn: sqlite3.Connection, data: dict) -> int:
    name = stock.required(data, "item_name")
    unit = stock.required(data, "unit")
    fields = [
        data.get("category"),
        data.get("lab"),
        data.get("location_code"),
        name,
        data.get("spec"),
        data.get("brand"),
        unit,
    ]
    key = stable_key(fields)
    old = conn.execute(
        "SELECT id FROM inventory_items WHERE item_key=?", (key,)
    ).fetchone()
    if old:
        return int(old["id"])
    item_id = int(
        conn.execute(
            "INSERT INTO inventory_items(item_key,category,lab,location_code,item_name,normalized_name,spec,brand,unit,quantity,threshold,remark) VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
            (
                key,
                data.get("category"),
                data.get("lab"),
                data.get("location_code"),
                name,
                normalize_text(name),
                data.get("spec"),
                data.get("brand"),
                unit,
                float(stock.number(data.get("threshold"))),
                data.get("remark"),
            ),
        ).lastrowid
    )
    stock.ensure_balance(conn, item_id)
    add_aliases(conn, item_id, name)
    return item_id


def update_item(conn: sqlite3.Connection, data: dict, actor: str) -> dict:
    item = stock.ensure_balance(conn, int(data["item_id"]))
    stock.check_version(item, data.get("version"))
    fields = (
        "item_name",
        "spec",
        "brand",
        "lab",
        "location_code",
        "category",
        "remark",
    )
    values = {field: data.get(field, item[field]) for field in fields}
    if (values["lab"], values["location_code"]) != (
        item["lab"],
        item["location_code"],
    ) and (
        stock.number(stock.view(conn, item["item_id"])["owned_quantity"])
        or stock.reserved(conn, item["item_id"])
    ):
        raise ValueError("有数量的物品请通过数量转移调整位置")
    if not str(values["item_name"] or "").strip():
        raise ValueError("耗材名称不能为空")
    key = stable_key(
        [
            values["category"],
            values["lab"],
            values["location_code"],
            values["item_name"],
            values["spec"],
            values["brand"],
            item["unit"],
        ]
    )
    if conn.execute(
        "SELECT id FROM inventory_items WHERE item_key=? AND id!=?",
        (key, item["item_id"]),
    ).fetchone():
        raise ValueError("目标位置已有相同物品，请使用数量转移")
    conversions = data.get("conversions", json.loads(item["conversions"]))
    if not isinstance(conversions, dict) or any(
        stock.number(value) <= 0 for value in conversions.values()
    ):
        raise ValueError("包装换算必须是大于零的倍数")
    if any(
        not str(key).strip() or normalize_text(key) == normalize_text(item["unit"])
        for key in conversions
    ):
        raise ValueError("包装单位不能为空，也不能覆盖基础单位")
    usage = data.get("usage_type", item["usage_type"])
    if usage not in ("consumable", "reusable", "unclassified"):
        raise ValueError("请选择有效的使用方式")
    conn.execute(
        "UPDATE inventory_items SET item_name=?,spec=?,brand=?,lab=?,location_code=?,category=?,remark=?,item_key=?,normalized_name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (
            *[values[field] for field in fields],
            key,
            normalize_text(values["item_name"]),
            item["item_id"],
        ),
    )
    conn.execute(
        "UPDATE stock_balances SET conversions=?,usage_type=?,version=version+1 WHERE item_id=?",
        (
            json.dumps({key: stock.text(value) for key, value in conversions.items()}),
            usage,
            item["item_id"],
        ),
    )
    stock.event(
        conn,
        item["item_id"],
        "metadata",
        {"before": {field: item[field] for field in fields}, "after": values},
        actor,
    )
    return {"item": stock.view(conn, item["item_id"])}


def transfer(conn: sqlite3.Connection, data: dict, actor: str) -> dict:
    item = stock.ensure_balance(conn, int(data["item_id"]))
    stock.check_version(item, data.get("version"))
    target_lab, target_location = (
        stock.required(data, "target_lab"),
        stock.required(data, "target_location"),
    )
    if (target_lab, target_location) == (item["lab"], item["location_code"]):
        raise ValueError("原位置与目标位置相同")
    state = data.get("state", "ready")
    quantity = stock.convert(item, data.get("quantity"), data.get("unit"))
    if state not in stock.STATES or quantity <= 0:
        raise ValueError("转移数量或状态无效")
    if state == "ready" and quantity > stock.number(item["ready"]) - stock.reserved(
        conn, item["item_id"]
    ):
        raise ValueError("转移数量超过未预留数量")
    destination = {**item, "lab": target_lab, "location_code": target_location}
    target_id = create_item(conn, destination)
    target = stock.ensure_balance(conn, target_id)
    if not target["active"] or target["review_reason"]:
        raise ValueError("目标位置物品已停用或需要清点")
    stock.update_states(conn, item, {state: -quantity})
    stock.event(
        conn,
        item["item_id"],
        "transfer_out",
        {state: stock.text(-quantity)},
        actor,
        str(data.get("note") or ""),
        target_id,
    )
    stock.update_states(conn, target, {state: quantity})
    stock.event(
        conn,
        target_id,
        "transfer_in",
        {state: stock.text(quantity)},
        actor,
        str(data.get("note") or ""),
        item["item_id"],
    )
    return {
        "source": stock.view(conn, item["item_id"]),
        "target": stock.view(conn, target_id),
    }


def archive_location(conn: sqlite3.Connection, data: dict, actor: str) -> dict:
    rows = conn.execute(
        "SELECT i.id FROM inventory_items i JOIN stock_balances b ON b.item_id=i.id WHERE COALESCE(i.lab,'')=? AND COALESCE(i.location_code,'')=? AND b.active=1",
        (data.get("lab") or "", data.get("location_code") or ""),
    ).fetchall()
    if not rows:
        raise ValueError("位置没有有效物品")
    for row in rows:
        item = stock.view(conn, row["id"])
        if (
            stock.number(item["owned_quantity"])
            or stock.number(item["reserved_quantity"])
            or item["review_reason"]
        ):
            raise ValueError("请先转移或处理所有数量，并完成待核对事项，再停用位置")
        conn.execute(
            "UPDATE stock_balances SET active=0,version=version+1 WHERE item_id=?",
            (row["id"],),
        )
        stock.event(conn, row["id"], "archive", {}, actor, stock.required(data, "note"))
    return {"ok": True, "archived_count": len(rows)}


def preview_import(
    conn: sqlite3.Connection,
    path: Path,
    filename: str,
    mode: str,
    category: str = "",
    commit: bool = True,
) -> dict:
    from .services import map_upload_row

    if mode not in ("add", "overwrite"):
        raise ValueError("上传方式无效")
    from .attachments import uploaded_frame

    df = uploaded_frame(path).dropna(how="all")
    df.columns = [str(col).strip() for col in df.columns]
    rows, errors, seen = [], [], set()
    for index, row in df.iterrows():
        try:
            mapped = map_upload_row(row.to_dict(), list(df.columns), category)
            if not mapped:
                continue
            if not mapped["unit"]:
                raise ValueError("必须填写数量单位")
            if (
                mapped["threshold"] is not None
                and stock.number(mapped["threshold"]) < 0
            ):
                raise ValueError("预警数量不能为负数")
            signature = hashlib.sha256(
                json.dumps(mapped, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            old = conn.execute(
                "SELECT id FROM inventory_items WHERE item_key=?", (mapped["item_key"],)
            ).fetchone()
            balance = stock.ensure_balance(conn, old["id"]) if old else None
            mapped.update(
                {
                    "row_number": int(index) + 2,
                    **df.attrs["sources"][int(index)],
                    "duplicate_row": signature in seen,
                    "item_id": old["id"] if old else None,
                    "version": balance["version"] if balance else None,
                    "before": balance["ready"] if balance else "0",
                }
            )
            seen.add(signature)
            rows.append(mapped)
        except ValueError as exc:
            errors.append({"row_number": int(index) + 2, "error": str(exc)})
    token = uuid.uuid4().hex
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    normalized = [
        {
            key: value
            for key, value in row.items()
            if key not in ("version", "item_id", "before")
        }
        for row in rows
    ]
    content_hash = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    duplicate = bool(
        conn.execute(
            "SELECT 1 FROM upload_previews WHERE committed=1 AND (file_hash=? OR content_hash=?)",
            (file_hash, content_hash),
        ).fetchone()
    )
    conn.execute(
        "INSERT INTO upload_previews(id,file_hash,content_hash,filename,mode,rows,errors) VALUES (?,?,?,?,?,?,?)",
        (
            token,
            file_hash,
            content_hash,
            filename,
            mode,
            json.dumps(rows, ensure_ascii=False),
            json.dumps(errors, ensure_ascii=False),
        ),
    )
    if commit:
        conn.commit()
    return {
        "preview_id": token,
        "rows": rows,
        "errors": errors,
        "duplicate_file": duplicate,
    }


def commit_import(conn: sqlite3.Connection, data: dict, actor: str) -> dict:
    preview = conn.execute(
        "SELECT * FROM upload_previews WHERE id=?", (data.get("preview_id"),)
    ).fetchone()
    if not preview or preview["committed"]:
        raise ValueError("上传预览不存在或已执行")
    if json.loads(preview["errors"]):
        raise ValueError("请先修正表格中的错误行")
    old = conn.execute(
        "SELECT 1 FROM upload_previews WHERE committed=1 AND (file_hash=? OR content_hash=?)",
        (preview["file_hash"], preview["content_hash"]),
    ).fetchone()
    if old and not (
        data.get("allow_duplicate") and str(data.get("note") or "").strip()
    ):
        raise stock.Conflict("该表格已导入，再次登记需要明确确认及填写原因")
    all_rows = json.loads(preview["rows"])
    rows = [row for row in all_rows if not row["duplicate_row"]]
    if not rows:
        raise ValueError("没有可导入的记录")
    for row in rows:
        if row["item_id"]:
            stock.check_version(
                stock.ensure_balance(conn, row["item_id"]), row["version"]
            )
    for row in rows:
        item_id = row["item_id"] or create_item(conn, row)
        item = stock.ensure_balance(conn, item_id)
        if item["review_reason"]:
            raise ValueError("上传涉及待清点物品，请先完成实物清点")
        stock.operate(
            conn,
            {
                "item_id": item_id,
                "version": item["version"],
                "action": "inbound" if preview["mode"] == "add" else "count",
                "quantity": row["quantity"],
                "unit": row["unit"],
                "note": f"库存表 {preview['filename']} 第 {row['row_number']} 行；{data.get('note') or ''}",
            },
            actor,
        )
        if row["threshold"] is not None:
            stock.operate(
                conn,
                {
                    "item_id": item_id,
                    "version": stock.ensure_balance(conn, item_id)["version"],
                    "action": "threshold",
                    "quantity": row["threshold"],
                },
                actor,
            )
    source_ids = {}
    for row in all_rows:
        identity = (row["item_key"], str(row["quantity"]), str(row.get("threshold")), json.dumps(row["raw"], ensure_ascii=False, sort_keys=True))
        cursor = conn.execute(
            "INSERT INTO source_rows(row_hash,source_file,source_sheet,source_row_number,category,lab,location_code,item_name,normalized_name,spec,brand,quantity,unit,threshold,remark,raw_json,duplicate_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{preview['id']}:{row['row_number']}",
                preview["filename"],
                row.get("source_sheet"),
                row["row_number"],
                row.get("category"), row.get("lab"), row.get("location_code"),
                row["item_name"],
                row["normalized_name"],
                row.get("spec"), row.get("brand"),
                row["quantity"],
                row["unit"],
                row.get("threshold"), row.get("remark"),
                json.dumps(row["raw"], ensure_ascii=False),
                source_ids.get(identity) if row["duplicate_row"] else None,
            ),
        )
        source_ids.setdefault(identity, cursor.lastrowid)
    conn.execute("UPDATE upload_previews SET committed=1 WHERE id=?", (preview["id"],))
    return {"ok": True, "imported_rows": len(rows)}


def capacities(value: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?(?:ml|mm|cm|kg|g|l)", normalize_text(value))


def strict_matches(
    conn: sqlite3.Connection, name: str, spec: str | None, unit: str | None
) -> tuple[list[dict], list[dict]]:
    from .services import search_items

    candidates = search_items(
        conn, " ".join(part for part in (name, spec) if part), limit=500
    )
    matches = []
    for candidate in candidates:
        item = stock.view(conn, candidate["id"])
        names = [
            normalize_text(item["item_name"]),
            *[normalize_text(alias) for alias in candidate.get("aliases", [])],
        ]
        query = normalize_text(name)
        if query not in names:
            continue
        requested = normalize_text(spec)
        if (
            requested
            and requested != normalize_text(item["spec"])
            and requested not in normalize_text(item["item_name"])
        ):
            continue
        if capacities(name) and capacities(name) != capacities(item["item_name"]):
            continue
        try:
            stock.convert(item, 1, unit)
        except ValueError:
            continue
        if item["active"] and not item["review_reason"]:
            matches.append(item)
    variants = {
        (normalize_text(item["spec"]), item["unit"], normalize_text(item["brand"]))
        for item in matches
    }
    if not spec and len(variants) > 1:
        matches = []
    return matches, candidates


def allocate_preparation(
    conn: sqlite3.Connection, rows: list[dict], selections: dict | None = None
) -> list[dict]:
    allocated: dict[int, Decimal] = {}
    purchased: dict[int, Decimal] = {}
    result = []
    for original in rows:
        row = dict(original)
        matches, candidates = strict_matches(
            conn, row["name"], row["spec"], row["unit"]
        )
        chosen = (selections or {}).get(
            str(row["row_number"]), row.get("selected_item_id")
        )
        if chosen:
            item = stock.view(conn, int(chosen))
            if not item["active"] or item["review_reason"]:
                raise ValueError("选定物品需要清点或已经停用")
            if (
                row["spec"]
                and normalize_text(row["spec"]) != normalize_text(item["spec"])
                and normalize_text(row["spec"]) not in normalize_text(item["item_name"])
            ):
                raise ValueError("选定物品与清单规格不一致")
            if capacities(row["name"]) and capacities(row["name"]) != capacities(
                item["item_name"]
            ):
                raise ValueError("选定物品与清单容量或型号不一致")
            stock.convert(item, 1, row["unit"])
            matches = [item]
            row["selected_item_id"] = item["item_id"]
        row["unit"] = row["unit"] or (matches[0]["unit"] if matches else None)
        amount = remaining = stock.number(row["required_quantity"])
        locations = []
        outstanding = Decimal(0)
        for item in matches:
            item_id = item["item_id"]
            factor = stock.convert(item, 1, row["unit"])
            ready = max(
                stock.number(item["ready"])
                - stock.reserved(conn, item_id)
                - allocated.get(item_id, Decimal(0)),
                0,
            )
            take = min(ready, remaining * factor)
            locations.append(
                {
                    "item_id": item_id,
                    "item_name": item["item_name"],
                    "spec": item["spec"],
                    "unit": item["unit"],
                    "quantity": stock.text(ready),
                    "allocated_quantity": stock.text(take),
                    "lab": item["lab"],
                    "location_code": item["location_code"],
                    "version": item["version"],
                }
            )
            allocated[item_id] = allocated.get(item_id, Decimal(0)) + take
            remaining -= take / factor
            ordered = sum(
                (
                    max(stock.number(po["quantity"]) - stock.number(po["received"]), 0)
                    for po in conn.execute(
                        "SELECT quantity,received FROM purchase_orders WHERE item_id=? AND status='ordered'",
                        (item_id,),
                    )
                ),
                Decimal(0),
            )
            use = min(
                max(ordered - purchased.get(item_id, Decimal(0)), 0),
                max(remaining * factor, 0),
            )
            outstanding += use / factor
            purchased[item_id] = purchased.get(item_id, Decimal(0)) + use
        row.update(
            {
                "available_quantity": stock.text(amount - remaining),
                "missing_quantity": stock.text(remaining),
                "purchase_quantity": stock.text(max(remaining - outstanding, 0)),
                "purchase_unit": row["unit"],
                "on_order_quantity": stock.text(outstanding),
                "locations": locations,
                "match_count": len(locations),
                "candidates": candidates[:30],
                "status": "需要确认匹配"
                if not matches
                else "需要采购"
                if remaining
                else "足够",
            }
        )
        result.append(row)
    return result


def remap_preparation(conn: sqlite3.Connection, data: dict) -> dict:
    preview = conn.execute(
        "SELECT * FROM upload_previews WHERE id=? AND mode='prepare'",
        (data.get("preview_id"),),
    ).fetchone()
    if not preview or preview["committed"]:
        raise ValueError("清单预览不存在或已经执行")
    rows = allocate_preparation(
        conn, json.loads(preview["rows"]), data.get("selections")
    )
    token = uuid.uuid4().hex
    conn.execute("UPDATE upload_previews SET committed=1 WHERE id=?", (preview["id"],))
    conn.execute(
        "INSERT INTO upload_previews(id,file_hash,content_hash,filename,mode,rows,errors) VALUES (?,'','',?,'prepare',?,'[]')",
        (token, preview["filename"], json.dumps(rows, ensure_ascii=False)),
    )
    return {"preview_id": token, "rows": rows}


def preparation(
    conn: sqlite3.Connection, path: Path, course: str = "", commit: bool = True
) -> dict:
    from .attachments import uploaded_frame
    from .services import resolve_column

    df = uploaded_frame(path).dropna(how="all")
    df.columns = [str(col).strip() for col in df.columns]
    alternatives = {
        "name": ["耗材名称", "名称", "物品", "耗材"],
        "required_quantity": ["需求数量", "所需数量", "数量", "用量"],
        "unit": ["数量单位", "单位"],
        "spec": ["规格型号", "规格", "型号"],
        "course": ["课程", "课程名称"],
        "experiment": ["实验", "实验名称", "实验项目"],
        "action": ["使用方式", "动作"],
    }
    columns = {
        key: resolve_column(df.columns, names) for key, names in alternatives.items()
    }
    if not columns["name"] or not columns["required_quantity"]:
        raise ValueError("清单需要名称和需求数量列")
    from .normalization import clean_cell

    rows = []
    for index, record in df.iterrows():
        row = {
            key: clean_cell(record.get(col)) if col else None
            for key, col in columns.items()
        }
        if not row["name"]:
            continue
        amount = stock.number(row["required_quantity"])
        if amount <= 0:
            raise ValueError(f"第 {int(index) + 2} 行需求数量必须大于零")
        row.update(
            {
                "row_number": int(index) + 2,
                **df.attrs["sources"][int(index)],
                "course": row["course"] or course or "未命名课程",
                "experiment": row["experiment"] or "未命名实验",
                "required_quantity": stock.text(amount),
            }
        )
        action = {"消耗": "consume", "借用": "borrow", "借出": "borrow"}.get(
            row["action"], row["action"]
        )
        row["action"] = action if action in ("consume", "borrow") else ""
        rows.append(row)
    rows = allocate_preparation(conn, rows)
    token = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO upload_previews(id,file_hash,content_hash,filename,mode,rows,errors) VALUES (?,'','',?,'prepare',?,'[]')",
        (token, path.name, json.dumps(rows, ensure_ascii=False)),
    )
    if commit:
        conn.commit()
    groups = [
        {"course": a, "experiment": b}
        for a, b in sorted({(row["course"], row["experiment"]) for row in rows})
    ]
    return {"preview_id": token, "rows": rows, "groups": groups, "row_count": len(rows)}


def consume_preparation(conn: sqlite3.Connection, data: dict, actor: str) -> dict:
    preview = conn.execute(
        "SELECT * FROM upload_previews WHERE id=? AND mode='prepare'",
        (data.get("preview_id"),),
    ).fetchone()
    if not preview or preview["committed"]:
        raise ValueError("清单预览不存在或已经执行")
    selected = {int(value) for value in data.get("selected_rows", [])}
    rows = [row for row in json.loads(preview["rows"]) if row["row_number"] in selected]
    if not rows:
        raise ValueError("请选择清单条目")
    versions = {
        location["item_id"]: location["version"]
        for row in rows
        for location in row["locations"]
    }
    for item_id, version in versions.items():
        stock.check_version(stock.ensure_balance(conn, item_id), version)
    results = []
    for row in rows:
        action = (data.get("actions") or {}).get(str(row["row_number"]), row["action"])
        if (
            stock.number(row["missing_quantity"]) > 0
            or not row["locations"]
            or action != "consume"
        ):
            raise ValueError(
                "只有匹配明确、库存足够且明确标为消耗的条目才能直接扣减；借用请创建实验安排或借用记录"
            )
        for location in row["locations"]:
            if stock.number(location["allocated_quantity"]) > 0:
                results.append(
                    stock.operate(
                        conn,
                        {
                            "item_id": location["item_id"],
                            "action": "consume",
                            "quantity": location["allocated_quantity"],
                            "unit": location["unit"],
                            "note": f"{row['course']} / {row['experiment']}",
                        },
                        actor,
                        checked=True,
                    )
                )
    conn.execute("UPDATE upload_previews SET committed=1 WHERE id=?", (preview["id"],))
    return {"ok": True, "transaction_count": len(results), "transactions": results}


def template_lines(data: dict) -> list[dict]:
    people = int(data.get("people") or 0)
    group_size = int(data.get("group_size") or 1)
    if people < 0 or group_size <= 0:
        raise ValueError("人数及每组人数无效")
    groups = math.ceil(people / group_size)
    result = []
    for line in data.get("lines", []):
        values = {
            key: stock.number(line.get(key))
            for key in ("per_person", "per_group", "fixed", "spare")
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("需求不能为负数")
        amount = (
            values["per_person"] * people
            + values["per_group"] * groups
            + values["fixed"]
            + values["spare"]
        )
        if line.get("quantity") is not None:
            amount = stock.number(line["quantity"])
        if amount <= 0 or line.get("action") not in ("consume", "borrow"):
            raise ValueError("每项物品必须填写正数需求并选择消耗或借用")
        result.append(
            {**line, "item_id": int(line["item_id"]), "quantity": stock.text(amount)}
        )
    if not result:
        raise ValueError("至少需要一项物品")
    return result


def schedule(conn: sqlite3.Connection, data: dict, actor: str) -> dict:
    start, end = (
        stock.moment(stock.required(data, "starts_at")),
        stock.moment(stock.required(data, "ends_at")),
    )
    if start >= end:
        raise ValueError("结束时间必须晚于开始时间")
    lines = template_lines(data)
    amounts: dict[int, Decimal] = {}
    for line in lines:
        item = stock.ensure_balance(conn, line["item_id"])
        stock.check_version(item, line.get("version"))
        if item["review_reason"] or not item["active"]:
            raise ValueError("安排中包含待核对或停用物品")
        amount = stock.convert(item, line["quantity"], line.get("unit"))
        line["quantity"], line["unit"] = stock.text(amount), item["unit"]
        amounts[line["item_id"]] = amounts.get(line["item_id"], Decimal(0)) + amount
        has_consumption = any(
            entry["item_id"] == line["item_id"] and entry["action"] == "consume"
            for entry in lines
        )
        needed = stock.reserved(
            conn, line["item_id"], None if has_consumption else start, end
        )
        if amounts[line["item_id"]] + needed > stock.number(item["ready"]):
            raise ValueError(f"{item['item_name']} 在所选时间的可用数量不足")
    schedule_id = int(
        conn.execute(
            "INSERT INTO schedules(name,course,starts_at,ends_at,venue,actor,lines) VALUES (?,?,?,?,?,?,?)",
            (
                stock.required(data, "name"),
                stock.required(data, "course"),
                start,
                end,
                stock.required(data, "venue"),
                actor,
                json.dumps(lines, ensure_ascii=False),
            ),
        ).lastrowid
    )
    for line in lines:
        prior = conn.execute(
            "SELECT quantity FROM reservations WHERE schedule_id=? AND item_id=? AND action=?",
            (schedule_id, line["item_id"], line["action"]),
        ).fetchone()
        amount = stock.number(line["quantity"]) + (
            stock.number(prior["quantity"]) if prior else Decimal(0)
        )
        conn.execute(
            "INSERT INTO reservations(schedule_id,item_id,quantity,action) VALUES (?,?,?,?) ON CONFLICT(schedule_id,item_id,action) DO UPDATE SET quantity=excluded.quantity",
            (schedule_id, line["item_id"], stock.text(amount), line["action"]),
        )
        stock.event(
            conn,
            line["item_id"],
            "reserve",
            {},
            actor,
            f"{stock.required(data, 'course')} / {stock.required(data, 'name')}",
            schedule_id,
        )
        stock.alerts_changed(conn, line["item_id"])
    return {"ok": True, "id": schedule_id}


def execute_schedule(conn: sqlite3.Connection, data: dict, actor: str) -> dict:
    entry = conn.execute(
        "SELECT * FROM schedules WHERE id=?", (data.get("schedule_id"),)
    ).fetchone()
    if (
        not entry
        or entry["status"] != "reserved"
        or int(data.get("version", 0)) != entry["version"]
    ):
        raise stock.Conflict("实验安排已变化或已经执行")
    lines = json.loads(entry["lines"])
    for line in lines:
        item = stock.ensure_balance(conn, line["item_id"])
        stock.check_version(
            item, (data.get("item_versions") or {}).get(str(line["item_id"]))
        )
    conn.execute(
        "UPDATE schedules SET status='executed',version=version+1 WHERE id=?",
        (entry["id"],),
    )
    results = []
    for line in lines:
        op = {
            **line,
            "note": f"{entry['course']} / {entry['name']}",
            "schedule_id": entry["id"],
            "reservation_start": entry["starts_at"],
            "reservation_end": entry["ends_at"],
        }
        if line["action"] == "borrow":
            op.update(
                {
                    "borrower": stock.required(data, "borrower"),
                    "purpose": entry["name"],
                    "destination": entry["venue"],
                    "due_at": entry["ends_at"],
                }
            )
        results.append(stock.operate(conn, op, actor, checked=True))
    return {"ok": True, "transactions": results}


def purchase(conn: sqlite3.Connection, data: dict, actor: str) -> dict:
    action = data.get("action", "order")
    if action in {"order", "draft"}:
        item = stock.ensure_balance(conn, int(data["item_id"]))
        stock.check_version(item, data.get("version"))
        amount = stock.convert(item, data.get("quantity"), data.get("unit"))
        if amount <= 0:
            raise ValueError("订购数量必须大于零")
        order_id = int(
            conn.execute(
                "INSERT INTO purchase_orders(item_id,quantity,actor,expected_at,note,status) VALUES (?,?,?,?,?,?)",
                (
                    item["item_id"],
                    stock.text(amount),
                    str(data.get("owner") or actor) if action == "draft" else stock.required(data, "owner"),
                    "" if action == "draft" else stock.moment(stock.required(data, "expected_at")),
                    data.get("note"),
                    "draft" if action == "draft" else "ordered",
                ),
            ).lastrowid
        )
        stock.event(
            conn,
            item["item_id"],
            "purchase_draft" if action == "draft" else "purchase_order",
            {},
            actor,
            str(data.get("note") or ""),
            order_id,
        )
        return {"ok": True, "id": order_id}
    order = conn.execute(
        "SELECT * FROM purchase_orders WHERE id=?", (data.get("order_id"),)
    ).fetchone()
    if (
        not order
            or order["status"] not in {"ordered", "draft"}
        or int(data.get("order_version", 0)) != order["version"]
    ):
        raise stock.Conflict("采购记录已经变化或已完成")
    if action == "place":
        if order["status"] != "draft":
            raise stock.Conflict("此采购草案已经变化")
        conn.execute("UPDATE purchase_orders SET status='ordered',actor=?,expected_at=?,version=version+1 WHERE id=?", (stock.required(data, "owner"), stock.moment(stock.required(data, "expected_at")), order["id"]))
        stock.event(conn, order["item_id"], "purchase_order", {}, actor, str(data.get("note") or ""), order["id"])
    elif action == "cancel":
        conn.execute(
            "UPDATE purchase_orders SET status='cancelled',version=version+1 WHERE id=?",
            (order["id"],),
        )
        stock.event(
            conn,
            order["item_id"],
            "purchase_cancel",
            {},
            actor,
            stock.required(data, "note"),
            order["id"],
        )
    elif action == "receive":
        if order["status"] != "ordered":
            raise ValueError("采购草案需要先登记订购，才能登记到货")
        item = stock.ensure_balance(conn, order["item_id"])
        stock.check_version(item, data.get("version"))
        amount = stock.convert(item, data.get("quantity"), data.get("unit"))
        if amount <= 0 or amount > stock.number(order["quantity"]) - stock.number(
            order["received"]
        ):
            raise ValueError("到货数量超过尚未到货数量")
        receipt = stock.operate(
            conn,
            {
                "item_id": item["item_id"],
                "action": "inbound",
                "quantity": stock.text(amount),
                "note": f"采购到货 {order['id']}",
            },
            actor,
            checked=True,
        )
        conn.execute(
            "UPDATE stock_events SET action='purchase_receive',related_id=? WHERE id=?",
            (order["id"], receipt["id"]),
        )
        total = stock.number(order["received"]) + amount
        conn.execute(
            "UPDATE purchase_orders SET received=?,status=?,version=version+1 WHERE id=?",
            (
                stock.text(total),
                "received" if total == stock.number(order["quantity"]) else "ordered",
                order["id"],
            ),
        )
    else:
        raise ValueError("采购动作无效")
    return {"ok": True, "id": order["id"]}
