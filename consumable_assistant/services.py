from __future__ import annotations

import io
import json
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
from .normalization import (
    SPEC_UNITS,
    UNIT_WORDS,
    chinese_number_to_int,
    clean_cell,
    normalize_quantity,
    normalize_text,
    parse_float,
    quantity_to_text,
    stable_key,
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
    "batch_import": "批量导入",
    "move": "位置转移",
    "batch_delete": "批量删除",
    "undo": "撤销",
    "count": "实物清点",
    "state": "状态变更",
    "create": "新建物品",
    "metadata": "资料维护",
    "transfer_out": "转移出库",
    "transfer_in": "转移入库",
    "archive": "停用",
    "reserve": "实验预留",
    "cancel_reservation": "取消实验预留",
    "purchase_order": "登记采购",
    "purchase_receive": "采购到货",
    "purchase_cancel": "取消采购余量",
    "alert_acknowledge": "确认预警",
    "alert_snooze": "暂缓提醒",
    "alert_remind": "恢复提醒",
}


UPLOAD_COLUMNS = {
    "category": ["类别", "分类", "category"],
    "lab": ["实验室", "所属实验室", "lab"],
    "location": ["位置", "存放地", "耗材位置", "柜位", "位置编码", "location"],
    "name": ["耗材名称", "产品名", "名称", "物品", "耗材", "item", "product"],
    "spec": ["规格型号", "规格", "型号", "spec"],
    "brand": ["品牌", "brand"],
    "quantity": ["数量", "耗材数量", "库存数量", "qty", "quantity"],
    "unit": ["数量单位", "单位", "unit"],
    "threshold": ["库存预警", "数量预警", "预警数量", "安全库存", "threshold"],
    "remark": ["备注", "说明", "remark", "note"],
}


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    from decimal import Decimal

    from .ledger import number, text

    items = search_items(conn, limit=1_000_000)
    item_count = len(items)
    source_count = conn.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0]
    txn_count = conn.execute("SELECT COUNT(*) FROM inventory_transactions").fetchone()[
        0
    ]
    txn_count += conn.execute("SELECT COUNT(*) FROM stock_events").fetchone()[0]
    low_stock_count = sum(item["low_stock"] for item in items)
    categories = []
    for category in sorted({item["category"] or "" for item in items}):
        rows = [item for item in items if (item["category"] or "") == category]
        categories.append(
            {
                "category": category,
                "item_count": len(rows),
                "total_quantity": text(
                    sum((number(item["ready_quantity"]) for item in rows), Decimal(0))
                ),
            }
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
    starts_at: str | None = None,
    ends_at: str | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where: list[str] = ["1 = 1"]
    if category:
        where.append("category = ?")
        params.append(category)
    if lab:
        where.append("lab = ?")
        params.append(lab)
    rows = rows_to_dicts(
        conn.execute(
            f"""
            SELECT *
            FROM inventory_items
            WHERE {" AND ".join(where)}
            ORDER BY updated_at DESC, id DESC
            """,
            params,
        )
    )
    alias_map = load_alias_map(conn)
    query = normalize_text(q)
    ranked: list[dict[str, Any]] = []
    for row in rows:
        from .ledger import view

        details = view(conn, row["id"], starts_at, ends_at)
        if not details["active"]:
            continue
        row.update(details)
        score = score_item(row, query, alias_map.get(row["id"], []))
        if query and score < 40:
            continue
        row["score"] = round(score, 2)
        row["quantity_text"] = quantity_to_text(row["quantity"])
        row["threshold_text"] = quantity_to_text(row["threshold"])
        from .ledger import number

        row["low_stock"] = bool(
            row["threshold"]
            and number(row["available_quantity"]) < number(row["threshold"])
        )
        if low_stock and not row["low_stock"]:
            continue
        row["aliases"] = alias_map.get(row["id"], [])
        ranked.append(row)
    if query:
        ranked.sort(key=lambda item: (item["score"], item["quantity"]), reverse=True)
    else:
        ranked.sort(
            key=lambda item: (item["low_stock"], item["updated_at"]), reverse=True
        )
    limited = ranked[:limit]
    attach_latest_transactions(conn, limited)
    return limited


def transaction_candidates(
    conn: sqlite3.Connection, item_query: str, limit: int = 8
) -> list[dict[str, Any]]:
    """Prefer exact item names while retaining multi-location rows."""
    candidates = search_items(conn, item_query, limit=limit)
    specifications = set(
        re.findall(r"\d+(?:\.\d+)?(?:ml|mm|cm|kg|g|l)", normalize_text(item_query))
    )
    if specifications:
        candidates = [
            item
            for item in candidates
            if specifications.issubset(
                set(
                    re.findall(
                        r"\d+(?:\.\d+)?(?:ml|mm|cm|kg|g|l)",
                        normalize_text(
                            str(item["item_name"]) + " " + str(item.get("spec") or "")
                        ),
                    )
                )
            )
        ]
    query = normalize_text(item_query)
    if not query:
        return candidates

    exact_name = [
        item for item in candidates if normalize_text(item.get("item_name")) == query
    ]
    if exact_name:
        return exact_name

    exact_alias = [
        item
        for item in candidates
        if any(normalize_text(alias) == query for alias in item.get("aliases") or [])
    ]
    if exact_alias:
        return exact_alias

    return candidates


def attach_latest_transactions(
    conn: sqlite3.Connection, items: list[dict[str, Any]]
) -> None:
    for item in items:
        recent = conn.execute(
            "SELECT * FROM stock_events WHERE item_id=? ORDER BY id DESC LIMIT 1",
            (item["id"],),
        ).fetchone()
        if recent:
            from .ledger import text

            latest = dict(recent)
            snapshot = json.loads(latest["snapshot"])
            latest.update(snapshot)
            latest.update(
                {
                    "delta_quantity": json.loads(latest["changes"]).get("ready", "0"),
                    "undone_by": latest["reversed_by"],
                    "source_type": "ledger",
                    "can_undo": not latest["reversed_by"]
                    and latest["action"]
                    in (
                        "inbound",
                        "consume",
                        "borrow",
                        "threshold",
                        "count",
                        "adjust",
                        "state",
                    ),
                    "version": item["version"],
                }
            )
            latest["action_label"] = ACTION_LABELS.get(
                latest["action"], latest["action"]
            )
            latest["delta_quantity_text"] = text(latest["delta_quantity"])
            latest["quantity_after_text"] = text(latest.get("quantity_after", 0))
            item["latest_transaction"] = latest
            continue
        txn = conn.execute(
            """
            SELECT id, action, delta_quantity, quantity_after, unit, note,
                   source_type, undone_by, created_at
            FROM inventory_transactions
            WHERE item_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (item["id"],),
        ).fetchone()
        if not txn:
            item["latest_transaction"] = None
            continue
        latest = row_to_dict(txn) or {}
        latest["action_label"] = ACTION_LABELS.get(latest["action"], latest["action"])
        latest["delta_quantity_text"] = quantity_to_text(latest["delta_quantity"])
        latest["quantity_after_text"] = quantity_to_text(latest["quantity_after"])
        latest["can_undo"] = False
        item["latest_transaction"] = latest


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


CLAUSE_SPLIT_RE = re.compile(r"[，,；;。.!?！？\n\r]+")
CONSUME_ALL_WORDS = [
    "用光",
    "用完",
    "用尽",
    "耗尽",
    "全部用掉",
    "全用掉",
    "全部用完",
    "全用完",
]


def parse_local_operations(conn: sqlite3.Connection, text: str) -> list[dict[str, Any]]:
    clauses = [
        segment.strip() for segment in CLAUSE_SPLIT_RE.split(text) if segment.strip()
    ]
    if not clauses and text.strip():
        clauses = [text.strip()]
    operations: list[dict[str, Any]] = []
    previous_item_query: str | None = None
    for idx, clause in enumerate(clauses):
        operation = parse_local_clause(conn, clause, idx, previous_item_query)
        if not operation:
            continue
        operations.append(operation)
        if operation.get("item_query"):
            previous_item_query = str(operation["item_query"])
    if not operations and text.strip():
        operation = parse_local_clause(conn, text.strip(), 0, None)
        if operation:
            operations.append(operation)
    return operations


def parse_local_clause(
    conn: sqlite3.Connection,
    clause: str,
    index: int,
    previous_item_query: str | None,
) -> dict[str, Any] | None:
    quantity_match = extract_quantity(clause)
    action = infer_action(clause)
    item_query = clean_item_query(clause, quantity_match.span)
    if not item_query and previous_item_query:
        item_query = previous_item_query
    if (
        item_query
        and previous_item_query
        and item_query in {"盒子", "盒", "箱", "袋", "包", "套", "瓶", "支", "个"}
    ):
        item_query = f"{previous_item_query} {item_query}"

    quantity_value = quantity_match.value
    threshold_value = quantity_value if action == "threshold" else None
    unit_value = quantity_match.unit
    candidates = transaction_candidates(conn, item_query or clause, limit=8)
    quantity_mode = None
    if (
        action == "consume"
        and is_consume_all_phrase(clause)
        and (quantity_value is None or quantity_value <= 0)
    ):
        quantity_mode = "all_available"
        if candidates:
            quantity_value = float(candidates[0].get("quantity") or 0)
            unit_value = unit_value or candidates[0].get("unit")
    needs_review = action == "needs_review" or not candidates
    if quantity_value is None and action not in {"borrow", "return", "threshold"}:
        needs_review = True
    if action in {"borrow", "return"} and quantity_value is None:
        needs_review = True
    if action == "threshold" and threshold_value is None:
        needs_review = True
    if action == "needs_review" and not item_query and quantity_value is None:
        return None

    return {
        "id": f"op-{index}",
        "raw_text": clause,
        "action": action,
        "action_label": ACTION_LABELS.get(action, action),
        "quantity": quantity_value,
        "unit": unit_value,
        "threshold": threshold_value,
        "item_query": item_query or clause,
        "candidates": candidates,
        "needs_review": needs_review,
        "message": build_preview_message(action, quantity_value, candidates),
        "confidence": None,
        "quantity_mode": quantity_mode,
    }


def parse_transaction_preview(conn: sqlite3.Connection, text: str) -> dict[str, Any]:
    from .genai import AI_REQUIRED, AIUnavailable

    if AI_REQUIRED and not model_is_enabled():
        raise AIUnavailable("AI 凭据尚未配置，输入已保留，请联系管理员")
    model_result = None
    model_name = None
    model_error = None
    engine = "institutional"
    if model_is_enabled():
        try:
            model_result = parse_inventory_message(text)
        except Exception as exc:
            model_error = str(exc)[:160]
            model_result = None
            if AI_REQUIRED:
                raise AIUnavailable(
                    "校内 AI 暂时不可用，输入已保留，请重试或使用明确的库存操作表单"
                ) from exc
        if model_result:
            model_name = str(model_result.get("model") or "")
            engine = str(model_result.get("provider") or model_name or "institutional")

    local_operations = parse_local_operations(conn, text)
    local_split_used = False
    if not model_result:
        return {
            "operations": local_operations,
            "engine": engine,
            "model_name": None,
            "model_enabled": model_is_enabled(),
            "model_error": model_error,
            "local_split_used": len(local_operations) > 1,
        }

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
        if not isinstance(op, dict):
            op = {}
        action = normalize_action(
            op.get("action") if "action" in op else infer_action(text)
        )
        intent_review = requires_intent_review(text)
        if intent_review:
            action = "needs_review"
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

        candidates = transaction_candidates(conn, item_query or text, limit=8)
        if len(operations_data) == 1:
            specifications = set(
                re.findall(r"\d+(?:\.\d+)?(?:ml|mm|cm|kg|g|l)", normalize_text(text))
            )
            if specifications:
                candidates = [
                    item
                    for item in candidates
                    if specifications.issubset(
                        set(
                            re.findall(
                                r"\d+(?:\.\d+)?(?:ml|mm|cm|kg|g|l)",
                                normalize_text(
                                    str(item["item_name"])
                                    + " "
                                    + str(item.get("spec") or "")
                                ),
                            )
                        )
                    )
                ]
        quantity_mode = None
        op_text = " ".join(
            clean_cell(part) or ""
            for part in [
                op.get("item_query"),
                op.get("item_name"),
                op.get("note"),
                text,
            ]
        )
        if (
            action == "consume"
            and is_consume_all_phrase(op_text)
            and (quantity_value is None or quantity_value <= 0)
        ):
            quantity_mode = "all_available"
            if candidates:
                quantity_value = float(candidates[0].get("quantity") or 0)
                unit_value = unit_value or candidates[0].get("unit")
        needs_review = bool(op.get("needs_review")) if op else False
        if action == "needs_review" or not candidates:
            needs_review = True
        if quantity_value is None and action not in {"borrow", "return", "threshold"}:
            needs_review = True
        if action in {"borrow", "return"} and quantity_value is None:
            needs_review = True
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
                "quantity_mode": quantity_mode,
            }
        )

    if len(local_operations) > 1 and len(operations) <= 1:
        operations = local_operations
        local_split_used = True
    elif not operations:
        operations = local_operations or []

    return {
        "operations": operations,
        "engine": engine,
        "model_name": model_name or None,
        "model_enabled": model_is_enabled(),
        "model_error": model_error,
        "local_split_used": local_split_used,
    }


def infer_action(text: str) -> str:
    compact = normalize_text(text)
    if requires_intent_review(text):
        return "needs_review"
    if "不放回" in compact:
        return "consume"
    if any(word in compact for word in ["预警", "阈值", "最低库存", "安全库存"]):
        return "threshold"
    if any(word in compact for word in ["修正为", "改成", "调整为", "校正为", "改为"]):
        return "adjust"
    if any(word in compact for word in ["归还", "还回", "放回"]):
        return "return"
    if any(
        word in compact
        for word in ["入库", "买了", "采购", "补充", "新增", "到货", "增加", "添置"]
    ):
        return "inbound"
    if any(
        word in compact
        for word in [
            "不放回",
            "消耗",
            "用了",
            "用掉",
            "用完",
            "用光",
            "用尽",
            "耗尽",
            "使用了",
            "领用",
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


def requires_intent_review(text: str) -> bool:
    return bool(
        re.search(
            r"准备|计划|打算|如果|假如|预计|明天|下周|将要|需要买|要买|尚未|还没|未到货|(?:没有|没|未)(?:用|消耗|借|归还|采购)|不是|改口",
            text,
        )
    )


def is_consume_all_phrase(text: str) -> bool:
    compact = normalize_text(text)
    return any(word in compact for word in CONSUME_ALL_WORDS)


def normalize_action(value: Any) -> str:
    action = normalize_text(value)
    mapping = {
        "入库": "inbound",
        "新增": "inbound",
        "inbound": "inbound",
        "consume": "consume",
        "消耗": "consume",
        "用掉": "consume",
        "用完": "consume",
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
    if isinstance(value, int | float):
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
        if after.startswith((*SPEC_UNITS, "m ")):
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
        "补充了",
        "新增",
        "新增了",
        "到货",
        "增加",
        "增加了",
        "不放回",
        "消耗",
        "消耗了",
        "用了",
        "用掉",
        "用掉了",
        "用光",
        "用光了",
        "用完",
        "用完了",
        "用尽",
        "用尽了",
        "耗尽",
        "耗尽了",
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
        "归还了",
        "还回",
        "放回",
        "放回了",
        "修正为",
        "修正了",
        "调整为",
        "校正为",
        "改成",
        "改成了",
        "改为",
        "预警",
        "阈值",
        "最低库存",
        "安全库存",
        "的",
        "了",
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
    if quantity is None:
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
    source_type: str = "manual",
    commit: bool = True,
    **business: Any,
) -> dict[str, Any]:
    from . import ledger

    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN IMMEDIATE")
    try:
        item = ledger.ensure_balance(conn, item_id)
        result = ledger.operate(
            conn,
            {
                "item_id": item_id,
                "action": action,
                "quantity": quantity,
                "unit": unit,
                "note": note,
                "version": item["version"],
                **business,
            },
            actor or "script",
        )
        result.update(
            {
                "quantity_before": float(item["ready"]),
                "delta_quantity": float(result["item"]["quantity"])
                - float(item["ready"]),
                "threshold_before": item["threshold"],
                "threshold_after": result["item"]["threshold"],
            }
        )
        if commit:
            conn.commit()
        return result
    except Exception:
        if owns:
            conn.rollback()
        raise


def undo_transaction(
    conn: sqlite3.Connection, txn_id: int, actor: str | None = None
) -> dict[str, Any]:
    from . import ledger

    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute(
            "SELECT item_id FROM stock_events WHERE id=?", (txn_id,)
        ).fetchone()
        if not old:
            raise ValueError("历史导入记录不能撤销")
        result = ledger.undo(
            conn,
            txn_id,
            actor or "script",
            ledger.ensure_balance(conn, old["item_id"])["version"],
        )
        result.update(
            {
                "quantity_after": result["item"]["quantity"],
                "threshold_after": result["item"]["threshold"],
            }
        )
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


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
    from decimal import Decimal

    from .ledger import number, text

    rows = search_items(conn, low_stock=True, limit=1_000_000)
    for row in rows:
        row["missing_quantity"] = text(
            max(number(row["threshold"] or 0) - number(row["available_quantity"]), Decimal(0))
        )
        row["missing_quantity_text"] = quantity_to_text(row["missing_quantity"])
    return rows


def update_threshold(
    conn: sqlite3.Connection, item_id: int, threshold: float
) -> dict[str, Any]:
    return apply_transaction(
        conn, item_id, "threshold", threshold, note="页面设置库存预警"
    )


def list_locations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from decimal import Decimal

    from .ledger import number, text

    grouped = {}
    for item in search_items(conn, limit=1_000_000):
        key = (item["lab"] or "", item["location_code"] or "")
        row = grouped.setdefault(
            key,
            {
                "lab": key[0],
                "location_code": key[1],
                "item_count": 0,
                "total_quantity": Decimal(0),
                "zero_count": 0,
                "quantity_by_unit": {},
            },
        )
        row["item_count"] += 1
        row["total_quantity"] += number(item["ready_quantity"])
        row["zero_count"] += number(item["ready_quantity"]) == 0
        unit = item["unit"] or ""
        row["quantity_by_unit"][unit] = number(
            row["quantity_by_unit"].get(unit, 0)
        ) + number(item["ready_quantity"])
    result = []
    for key, row in sorted(grouped.items()):
        row["total_quantity"] = text(row["total_quantity"])
        row["quantity_by_unit"] = {
            unit: text(value) for unit, value in row["quantity_by_unit"].items()
        }
        row["label"] = " · ".join(part for part in key if part) or "未填写位置"
        result.append(row)
    return result


def preview_location(
    conn: sqlite3.Connection, lab: str | None, location_code: str | None
) -> dict[str, Any]:
    from decimal import Decimal

    from .ledger import number, text

    lab_value = clean_cell(lab) or ""
    location_value = clean_cell(location_code) or ""
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT id, category, lab, location_code, item_name, spec, brand,
                   quantity, unit, threshold, remark
            FROM inventory_items
            WHERE COALESCE(lab, '') = ? AND COALESCE(location_code, '') = ?
            ORDER BY item_name, spec, id
            """,
            (lab_value, location_value),
        )
    )
    for row in rows:
        from .ledger import view

        row.update(view(conn, row["id"]))
        row["quantity_text"] = quantity_to_text(row["quantity"])
        row["threshold_text"] = quantity_to_text(row["threshold"])
    rows = [row for row in rows if row["active"]]
    return {
        "lab": lab_value,
        "location_code": location_value,
        "rows": rows,
        "row_count": len(rows),
        "zero_count": sum(number(row["quantity"] or 0) == 0 for row in rows),
        "total_quantity": text(
            sum((number(row["quantity"]) for row in rows), Decimal(0))
        ),
    }


def move_location(
    conn: sqlite3.Connection,
    source_lab: str | None,
    source_location: str | None,
    target_lab: str | None,
    target_location: str | None,
) -> dict[str, Any]:
    raise ValueError("请使用带请求编号及库存版本的位置转移接口")


def delete_location(
    conn: sqlite3.Connection, lab: str | None, location_code: str | None
) -> dict[str, Any]:
    raise ValueError("请先转移数量，再通过停用接口保留历史记录")


def resolve_upload_column(columns: list[str] | pd.Index, key: str) -> str | None:
    return resolve_column(columns, UPLOAD_COLUMNS[key])


def map_upload_row(
    row: dict[str, Any],
    columns: list[str] | pd.Index,
    default_category: str | None,
) -> dict[str, Any] | None:
    name_col = resolve_upload_column(columns, "name")
    quantity_col = resolve_upload_column(columns, "quantity")
    if not name_col or not quantity_col:
        raise ValueError("批量上传需要包含耗材名称和数量列")
    item_name = clean_cell(row.get(name_col))
    if not item_name:
        return None
    from .ledger import number, text

    raw_quantity = clean_cell(row.get(quantity_col))
    quantity = number(raw_quantity) if raw_quantity is not None else None
    if quantity is None or quantity < 0:
        raise ValueError(f"耗材“{item_name}”的数量无效")

    def cell(key: str) -> str | None:
        column = resolve_upload_column(columns, key)
        return clean_cell(row.get(column)) if column else None

    threshold_col = resolve_upload_column(columns, "threshold")
    mapped = {
        "category": cell("category") or clean_cell(default_category) or "未分类",
        "lab": cell("lab"),
        "location_code": cell("location"),
        "item_name": item_name,
        "normalized_name": normalize_text(item_name),
        "spec": cell("spec"),
        "brand": cell("brand"),
        "quantity": text(quantity),
        "unit": cell("unit"),
        "threshold": text(number(clean_cell(row.get(threshold_col))))
        if threshold_col and clean_cell(row.get(threshold_col)) is not None
        else None,
        "remark": cell("remark"),
        "raw": {
            str(key): clean_cell(value)
            for key, value in row.items()
            if clean_cell(value) is not None
        },
    }
    mapped["item_key"] = stable_key(
        [
            mapped["category"],
            mapped["lab"],
            mapped["location_code"],
            mapped["item_name"],
            mapped["spec"],
            mapped["brand"],
            mapped["unit"],
        ]
    )
    return mapped


def import_uploaded_inventory(
    conn: sqlite3.Connection,
    path: Path,
    filename: str,
    mode: str,
    default_category: str | None = None,
) -> dict[str, Any]:
    from .workflows import preview_import

    return preview_import(conn, path, filename, mode, default_category or "")


def parse_prepare_file(
    conn: sqlite3.Connection, path: Path, default_course: str | None = None
) -> dict[str, Any]:
    from .workflows import preparation

    return preparation(conn, path, default_course or "")


def consume_prepare_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    actor: str | None = None,
) -> dict[str, Any]:
    raise ValueError("请使用服务器保存的清单预览编号及请求编号确认操作")


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
                        "purchase_quantity": math.ceil(missing / per_package),
                        "purchase_unit": match.group(2),
                    }
    return {"purchase_quantity": normalize_quantity(missing), "purchase_unit": unit}


def export_inventory_workbook(
    conn: sqlite3.Connection,
    q: str = "",
    category: str | None = None,
    lab: str | None = None,
) -> bytes:
    matches = search_items(conn, q=q, category=category, lab=lab, limit=2000)
    item_ids = [item["id"] for item in matches]
    if item_ids:
        placeholders = ",".join("?" for _ in item_ids)
        items = rows_to_dicts(
            conn.execute(
                f"""
                SELECT id, category, lab, item_name, spec, brand, location_code, quantity, unit, threshold, remark, source_count
                FROM inventory_items WHERE id IN ({placeholders})
                ORDER BY category, lab, item_name, location_code
                """,
                item_ids,
            )
        )
    else:
        items = []
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
        "总拥有量",
        "借出数量",
        "待清洗数量",
        "维修数量",
        "待核对数量",
        "预留数量",
        "可分配数量",
        "待核对说明",
    ]
    sheet.append(headers)
    for item in items:
        from .ledger import view

        item.update(view(conn, item["id"]))
        if not item["active"]:
            continue
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
                item["owned_quantity"],
                item["borrowed_quantity"],
                item["cleaning"],
                item["repair"],
                item["quarantine"],
                item["reserved_quantity"],
                item["available_quantity"],
                item["review_reason"],
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
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def save_upload_to_temp(contents: bytes, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
        handle.write(contents)
        return Path(handle.name)
