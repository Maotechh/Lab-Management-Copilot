from __future__ import annotations

import io
import math
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook

from .database import row_to_dict, rows_to_dicts
from .genai import model_is_enabled, parse_inventory_message
from .importer import read_inventory_sheet
from .normalization import (
    SPEC_UNITS,
    UNIT_WORDS,
    chinese_number_to_int,
    clean_cell,
    normalize_quantity,
    normalize_text,
    parse_float,
    quantity_to_text,
)


ACTION_LABELS = {
    "inbound": "入库",
    "consume": "消耗",
    "borrow": "借出",
    "return": "归还",
    "adjust": "修正",
    "threshold": "预警设置",
    "needs_review": "需要确认",
    "import": "初始导入",
    "undo": "撤销",
}


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    item_count = conn.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0]
    source_count = conn.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0]
    txn_count = conn.execute("SELECT COUNT(*) FROM inventory_transactions").fetchone()[
        0
    ]
    low_stock_count = conn.execute(
        "SELECT COUNT(*) FROM inventory_items WHERE threshold > 0 AND quantity < threshold"
    ).fetchone()[0]
    categories = rows_to_dicts(
        conn.execute(
            """
            SELECT category, COUNT(*) AS item_count, SUM(quantity) AS total_quantity
            FROM inventory_items
            GROUP BY category
            ORDER BY category
            """
        )
    )
    return {
        "item_count": item_count,
        "source_row_count": source_count,
        "transaction_count": txn_count,
        "low_stock_count": low_stock_count,
        "categories": categories,
    }


def search_items(
    conn: sqlite3.Connection,
    q: str = "",
    category: str | None = None,
    lab: str | None = None,
    low_stock: bool = False,
    limit: int = 80,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where: list[str] = ["1 = 1"]
    if category:
        where.append("category = ?")
        params.append(category)
    if lab:
        where.append("lab = ?")
        params.append(lab)
    if low_stock:
        where.append("threshold > 0 AND quantity < threshold")
    rows = rows_to_dicts(
        conn.execute(
            f"""
            SELECT *
            FROM inventory_items
            WHERE {" AND ".join(where)}
            ORDER BY updated_at DESC, id DESC
            LIMIT 1500
            """,
            params,
        )
    )
    alias_map = load_alias_map(conn)
    query = normalize_text(q)
    ranked: list[dict[str, Any]] = []
    for row in rows:
        score = score_item(row, query, alias_map.get(row["id"], []))
        if query and score <= 10:
            continue
        row["score"] = round(score, 2)
        row["quantity_text"] = quantity_to_text(row["quantity"])
        row["threshold_text"] = quantity_to_text(row["threshold"])
        row["low_stock"] = bool(row["threshold"] and row["quantity"] < row["threshold"])
        row["aliases"] = alias_map.get(row["id"], [])
        ranked.append(row)
    if query:
        ranked.sort(key=lambda item: (item["score"], item["quantity"]), reverse=True)
    else:
        ranked.sort(
            key=lambda item: (item["low_stock"], item["updated_at"]), reverse=True
        )
    return ranked[:limit]


def load_alias_map(conn: sqlite3.Connection) -> dict[int, list[str]]:
    rows = conn.execute(
        "SELECT item_id, alias FROM item_aliases ORDER BY alias"
    ).fetchall()
    alias_map: dict[int, list[str]] = {}
    for row in rows:
        alias_map.setdefault(int(row["item_id"]), []).append(str(row["alias"]))
    return alias_map


def score_item(row: dict[str, Any], query: str, aliases: list[str]) -> float:
    if not query:
        return 1.0
    fields = [
        row.get("normalized_name") or "",
        normalize_text(row.get("spec")),
        normalize_text(row.get("brand")),
        normalize_text(row.get("location_code")),
        normalize_text(row.get("lab")),
        normalize_text(row.get("category")),
        *[normalize_text(alias) for alias in aliases],
    ]
    best = 0.0
    for field in fields:
        if not field:
            continue
        if field == query:
            best = max(best, 100.0)
        elif query in field:
            best = max(best, 86.0 + min(10.0, len(query) / max(len(field), 1) * 10))
        elif field in query:
            best = max(best, 72.0 + min(10.0, len(field) / max(len(query), 1) * 10))
        else:
            best = max(best, SequenceMatcher(None, field, query).ratio() * 70)
    return best


@dataclass
class QuantityMatch:
    value: float | None
    unit: str | None
    span: tuple[int, int] | None


def parse_transaction_preview(conn: sqlite3.Connection, text: str) -> dict[str, Any]:
    model_result = None
    model_name = None
    engine = "heuristic"
    if model_is_enabled():
        try:
            model_result = parse_inventory_message(text)
        except Exception:
            model_result = None
        if model_result:
            model_name = str(model_result.get("model") or "")
            engine = "deepseek-pro"

    operations_data = []
    if (
        model_result
        and isinstance(model_result.get("operations"), list)
        and model_result["operations"]
    ):
        operations_data = model_result["operations"]
    else:
        operations_data = [model_result or {}]

    operations = []
    for idx, op in enumerate(operations_data):
        action = normalize_action(
            op.get("action") if "action" in op else infer_action(text)
        )
        raw_item_query = (
            clean_cell(op.get("item_query")) or clean_cell(op.get("item_name")) or text
        )
        item_query = clean_item_query(raw_item_query, None)
        quantity_value = parse_number_value(op.get("quantity")) if op else None
        threshold_value = parse_number_value(op.get("threshold")) if op else None

        local_quantity_match = (
            extract_quantity(text) if not op else QuantityMatch(None, None, None)
        )
        if quantity_value is None and not op:
            quantity_value = local_quantity_match.value
            if not item_query:
                item_query = clean_item_query(text, local_quantity_match.span)

        unit_value = normalize_unit(op.get("unit")) if op else None
        if not unit_value and not op:
            unit_value = local_quantity_match.unit

        candidates = search_items(conn, item_query or text, limit=8)
        needs_review = bool(op.get("needs_review")) if op else False
        if action == "needs_review" or not candidates:
            needs_review = True
        if quantity_value is None and action not in {"borrow", "return", "threshold"}:
            needs_review = True
        if action in {"borrow", "return"} and quantity_value is None:
            quantity_value = 0.0
        if action == "threshold" and threshold_value is not None:
            quantity_value = threshold_value

        operations.append(
            {
                "id": f"op-{idx}",
                "raw_text": text
                if len(operations_data) == 1
                else f"解析部分: {op.get('item_query', '')} {op.get('action', '')} {op.get('quantity', '')}",
                "action": action,
                "action_label": ACTION_LABELS.get(action, action),
                "quantity": quantity_value,
                "unit": unit_value,
                "threshold": threshold_value,
                "item_query": item_query or text,
                "candidates": candidates,
                "needs_review": needs_review,
                "message": build_preview_message(action, quantity_value, candidates),
                "confidence": op.get("confidence") if op else None,
            }
        )

    return {
        "operations": operations,
        "engine": engine,
        "model_name": model_name or None,
        "model_enabled": model_is_enabled(),
    }


def infer_action(text: str) -> str:
    compact = normalize_text(text)
    if any(word in compact for word in ["预警", "阈值", "最低库存", "安全库存"]):
        return "threshold"
    if any(word in compact for word in ["修正为", "改成", "调整为", "校正为", "改为"]):
        return "adjust"
    if any(word in compact for word in ["归还", "还回", "放回"]):
        return "return"
    if any(
        word in compact
        for word in ["入库", "买了", "采购", "补充", "新增", "到货", "增加"]
    ):
        return "inbound"
    if any(
        word in compact
        for word in [
            "不放回",
            "消耗",
            "用了",
            "使用了",
            "报废",
            "破损",
            "碎了",
            "出库",
            "减少",
        ]
    ):
        return "consume"
    if any(word in compact for word in ["借出", "借用", "借给", "暂借"]):
        return "borrow"
    if any(word in compact for word in ["取出", "拿走"]) and any(
        word in compact for word in ["回头", "之后", "稍后"]
    ):
        return "borrow"
    return "needs_review"


def normalize_action(value: Any) -> str:
    action = normalize_text(value)
    mapping = {
        "入库": "inbound",
        "inbound": "inbound",
        "consume": "consume",
        "消耗": "consume",
        "borrow": "borrow",
        "借出": "borrow",
        "return": "return",
        "归还": "return",
        "adjust": "adjust",
        "修正": "adjust",
        "threshold": "threshold",
        "预警": "threshold",
        "needs_review": "needs_review",
        "需要确认": "needs_review",
    }
    return mapping.get(action, "needs_review")


def parse_number_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    return parse_float(value)


def normalize_unit(value: Any) -> str | None:
    unit = clean_cell(value)
    return unit or None


def extract_quantity(text: str) -> QuantityMatch:
    unit_pattern = "|".join(
        re.escape(unit) for unit in sorted(UNIT_WORDS, key=len, reverse=True)
    )
    numeric_matches: list[QuantityMatch] = []
    for match in re.finditer(r"\d+(?:\.\d+)?", text):
        after = text[match.end() : match.end() + 8].strip().lower()
        if after.startswith(SPEC_UNITS) or after.startswith("m "):
            continue
        unit_match = re.match(rf"\s*({unit_pattern})", text[match.end() :])
        unit = unit_match.group(1) if unit_match else None
        end = match.end() + (unit_match.end() if unit_match else 0)
        numeric_matches.append(
            QuantityMatch(float(match.group(0)), unit, (match.start(), end))
        )
    if numeric_matches:
        with_unit = [item for item in numeric_matches if item.unit]
        return (with_unit or numeric_matches)[-1]
    chinese_match = re.search(
        rf"([零一二两三四五六七八九十百]+)\s*({unit_pattern})", text
    )
    if chinese_match:
        value = chinese_number_to_int(chinese_match.group(1))
        if value is not None:
            return QuantityMatch(
                float(value), chinese_match.group(2), chinese_match.span()
            )
    return QuantityMatch(None, None, None)


def clean_item_query(text: str, quantity_span: tuple[int, int] | None) -> str:
    working = text
    if quantity_span:
        start, end = quantity_span
        working = working[:start] + " " + working[end:]
    remove_words = [
        "我",
        "我们",
        "老师",
        "已经",
        "需要",
        "帮忙",
        "请",
        "入库",
        "买了",
        "采购",
        "补充",
        "新增",
        "到货",
        "增加",
        "不放回",
        "消耗",
        "用了",
        "使用了",
        "报废",
        "破损",
        "碎了",
        "出库",
        "减少",
        "借出",
        "借用",
        "借给",
        "暂借",
        "归还",
        "还回",
        "放回",
        "修正为",
        "调整为",
        "校正为",
        "改成",
        "改为",
        "预警",
        "阈值",
        "最低库存",
        "安全库存",
    ]
    for word in remove_words:
        working = working.replace(word, " ")
    working = re.sub(r"[，。,.；;：:\n\r]+", " ", working)
    return re.sub(r"\s+", " ", working).strip()


def build_preview_message(
    action: str, quantity: float | None, candidates: list[dict[str, Any]]
) -> str:
    if action == "needs_review":
        return "没有识别到明确的库存动作。"
    if quantity is None and action not in {"borrow", "return"}:
        return "没有识别到明确的数量。"
    if not candidates:
        return "没有找到匹配耗材。"
    return "已生成可确认的库存变更。"


def apply_transaction(
    conn: sqlite3.Connection,
    item_id: int,
    action: str,
    quantity: float,
    note: str | None = None,
    actor: str | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    item = conn.execute(
        "SELECT * FROM inventory_items WHERE id = ?", (item_id,)
    ).fetchone()
    if not item:
        raise ValueError("耗材不存在")
    before = float(item["quantity"])
    quantity = float(quantity or 0)
    if action == "threshold":
        after = before
        delta = 0.0
        conn.execute(
            "UPDATE inventory_items SET threshold = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (quantity, item_id),
        )
    elif action == "adjust":
        after = quantity
        delta = after - before
        conn.execute(
            "UPDATE inventory_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (after, item_id),
        )
    elif action == "inbound":
        delta = quantity
        after = before + delta
        conn.execute(
            "UPDATE inventory_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (after, item_id),
        )
    elif action == "consume":
        delta = -quantity
        after = before + delta
        if after < -1e-9:
            raise ValueError("库存数量不足")
        conn.execute(
            "UPDATE inventory_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (after, item_id),
        )
    elif action in {"borrow", "return"}:
        delta = 0.0
        after = before
        conn.execute(
            "UPDATE inventory_items SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (item_id,),
        )
    else:
        raise ValueError("不支持的库存动作")
    txn_id = conn.execute(
        """
        INSERT INTO inventory_transactions(
            item_id, action, delta_quantity, quantity_before, quantity_after,
            unit, note, actor, source_type
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual')
        """,
        (
            item_id,
            action,
            delta,
            before,
            after,
            unit or item["unit"],
            note,
            actor or "user",
        ),
    ).lastrowid
    conn.commit()
    return get_transaction(conn, int(txn_id))


def undo_transaction(
    conn: sqlite3.Connection, txn_id: int, actor: str | None = None
) -> dict[str, Any]:
    txn = conn.execute(
        "SELECT * FROM inventory_transactions WHERE id = ?", (txn_id,)
    ).fetchone()
    if not txn:
        raise ValueError("操作记录不存在")
    if txn["source_type"] == "import":
        raise ValueError("初始导入记录不能撤销")
    if txn["undone_by"]:
        raise ValueError("该记录已经撤销")
    item = conn.execute(
        "SELECT * FROM inventory_items WHERE id = ?", (txn["item_id"],)
    ).fetchone()
    if not item:
        raise ValueError("耗材不存在")
    before = float(item["quantity"])
    after = float(txn["quantity_before"])
    conn.execute(
        "UPDATE inventory_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (after, txn["item_id"]),
    )
    reverse_id = conn.execute(
        """
        INSERT INTO inventory_transactions(
            item_id, action, delta_quantity, quantity_before, quantity_after,
            unit, note, actor, source_type, source_ref
        )
        VALUES (?, 'undo', ?, ?, ?, ?, ?, ?, 'manual', ?)
        """,
        (
            txn["item_id"],
            after - before,
            before,
            after,
            txn["unit"],
            f"撤销操作 #{txn_id}",
            actor or "user",
            str(txn_id),
        ),
    ).lastrowid
    conn.execute(
        "UPDATE inventory_transactions SET undone_by = ? WHERE id = ?",
        (reverse_id, txn_id),
    )
    conn.commit()
    return get_transaction(conn, int(reverse_id))


def get_transaction(conn: sqlite3.Connection, txn_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT t.*, i.item_name, i.location_code, i.category, i.lab
        FROM inventory_transactions t
        JOIN inventory_items i ON i.id = t.item_id
        WHERE t.id = ?
        """,
        (txn_id,),
    ).fetchone()
    result = row_to_dict(row)
    if not result:
        raise ValueError("操作记录不存在")
    result["action_label"] = ACTION_LABELS.get(result["action"], result["action"])
    return result


def list_transactions(
    conn: sqlite3.Connection, limit: int = 80
) -> list[dict[str, Any]]:
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT t.*, i.item_name, i.location_code, i.category, i.lab
            FROM inventory_transactions t
            JOIN inventory_items i ON i.id = t.item_id
            ORDER BY t.created_at DESC, t.id DESC
            LIMIT ?
            """,
            (limit,),
        )
    )
    for row in rows:
        row["action_label"] = ACTION_LABELS.get(row["action"], row["action"])
        row["quantity_before_text"] = quantity_to_text(row["quantity_before"])
        row["quantity_after_text"] = quantity_to_text(row["quantity_after"])
        row["delta_quantity_text"] = quantity_to_text(row["delta_quantity"])
    return rows


def list_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = search_items(conn, low_stock=True, limit=500)
    for row in rows:
        row["missing_quantity"] = max(
            float(row["threshold"] or 0) - float(row["quantity"] or 0), 0.0
        )
        row["missing_quantity_text"] = quantity_to_text(row["missing_quantity"])
    return rows


def update_threshold(
    conn: sqlite3.Connection, item_id: int, threshold: float
) -> dict[str, Any]:
    return apply_transaction(
        conn, item_id, "threshold", threshold, note="页面设置库存预警"
    )


def parse_prepare_file(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    df = read_inventory_sheet(path).dropna(how="all")
    df.columns = [str(col).strip() for col in df.columns]
    name_col = resolve_column(
        df.columns, ["耗材名称", "产品名", "名称", "物品", "耗材", "item", "product"]
    )
    quantity_col = resolve_column(
        df.columns, ["需求数量", "所需数量", "数量", "用量", "需要数量", "qty"]
    )
    unit_col = resolve_column(df.columns, ["数量单位", "单位", "unit"])
    spec_col = resolve_column(df.columns, ["规格型号", "规格", "型号", "spec"])
    if not name_col or not quantity_col:
        raise ValueError("实验准备清单需要包含名称和数量列")
    rows: list[dict[str, Any]] = []
    for index, record in df.iterrows():
        name = clean_cell(record.get(name_col))
        if not name:
            continue
        required = parse_float(record.get(quantity_col)) or 0.0
        unit = clean_cell(record.get(unit_col)) if unit_col else None
        spec = clean_cell(record.get(spec_col)) if spec_col else None
        query = " ".join(part for part in [name, spec] if part)
        candidates = search_items(conn, query, limit=12)
        matched = choose_prepare_matches(query, candidates)
        available = sum(float(item["quantity"] or 0) for item in matched)
        missing = max(required - available, 0.0)
        locations = [
            {
                "item_id": item["id"],
                "item_name": item["item_name"],
                "lab": item["lab"],
                "location_code": item["location_code"],
                "quantity": item["quantity"],
                "unit": item["unit"],
                "spec": item["spec"],
            }
            for item in matched[:5]
        ]
        package = infer_package_suggestion(missing, unit, matched)
        rows.append(
            {
                "row_number": int(index) + 2,
                "name": name,
                "spec": spec,
                "required_quantity": normalize_quantity(required),
                "unit": unit,
                "available_quantity": normalize_quantity(available),
                "missing_quantity": normalize_quantity(missing),
                "locations": locations,
                "purchase_quantity": package["purchase_quantity"],
                "purchase_unit": package["purchase_unit"],
                "match_count": len(matched),
                "status": "足够" if missing <= 1e-9 else "需要采购",
            }
        )
    return {"rows": rows, "row_count": len(rows)}


def resolve_column(columns: list[str] | pd.Index, names: list[str]) -> str | None:
    normalized = {normalize_text(col): col for col in columns}
    for name in names:
        key = normalize_text(name)
        if key in normalized:
            return normalized[key]
    for col in columns:
        col_key = normalize_text(col)
        if any(normalize_text(name) in col_key for name in names):
            return str(col)
    return None


def choose_prepare_matches(
    query: str, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    q = normalize_text(query)
    exact = [
        item for item in candidates if q and q == normalize_text(item.get("item_name"))
    ]
    if exact:
        return exact
    if not candidates:
        return []
    top_score = float(candidates[0].get("score") or 0)
    if top_score >= 90:
        return [
            item
            for item in candidates
            if float(item.get("score") or 0) >= top_score - 2
        ]
    return [item for item in candidates if float(item.get("score") or 0) >= 75][:3]


def infer_package_suggestion(
    missing: float, unit: str | None, matches: list[dict[str, Any]]
) -> dict[str, Any]:
    if missing <= 0:
        return {"purchase_quantity": 0, "purchase_unit": unit}
    if unit:
        for item in matches:
            spec = item.get("spec") or ""
            pattern = rf"(\d+(?:\.\d+)?)\s*{re.escape(unit)}\s*/\s*([包盒袋箱筒])"
            match = re.search(pattern, spec)
            if match:
                per_package = float(match.group(1))
                if per_package > 0:
                    return {
                        "purchase_quantity": int(math.ceil(missing / per_package)),
                        "purchase_unit": match.group(2),
                    }
    return {"purchase_quantity": normalize_quantity(missing), "purchase_unit": unit}


def export_inventory_workbook(conn: sqlite3.Connection) -> bytes:
    items = rows_to_dicts(
        conn.execute(
            """
            SELECT category, lab, item_name, spec, brand, location_code, quantity, unit, threshold, remark, source_count
            FROM inventory_items
            ORDER BY category, lab, item_name, location_code
            """
        )
    )
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "当前库存"
    headers = [
        "类别",
        "实验室",
        "耗材名称",
        "规格型号",
        "品牌",
        "位置",
        "数量",
        "单位",
        "库存预警",
        "备注",
        "来源行数",
    ]
    sheet.append(headers)
    for item in items:
        sheet.append(
            [
                item["category"],
                item["lab"],
                item["item_name"],
                item["spec"],
                item["brand"],
                item["location_code"],
                item["quantity"],
                item["unit"],
                item["threshold"],
                item["remark"],
                item["source_count"],
            ]
        )
    return workbook_to_bytes(workbook)


def export_prepare_workbook(rows: list[dict[str, Any]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "实验准备核对"
    sheet.append(
        [
            "耗材名称",
            "规格",
            "需求数量",
            "单位",
            "可用数量",
            "还需采购",
            "采购数量",
            "采购单位",
            "位置",
            "状态",
        ]
    )
    for row in rows:
        locations = "；".join(
            f"{item.get('lab') or ''} {item.get('location_code') or ''} {quantity_to_text(item.get('quantity'))}{item.get('unit') or ''}".strip()
            for item in row.get("locations", [])
        )
        sheet.append(
            [
                row.get("name"),
                row.get("spec"),
                row.get("required_quantity"),
                row.get("unit"),
                row.get("available_quantity"),
                row.get("missing_quantity"),
                row.get("purchase_quantity"),
                row.get("purchase_unit"),
                locations,
                row.get("status"),
            ]
        )
    return workbook_to_bytes(workbook)


def workbook_to_bytes(workbook: Workbook) -> bytes:
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def save_upload_to_temp(contents: bytes, suffix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.write(contents)
    handle.flush()
    handle.close()
    return Path(handle.name)
