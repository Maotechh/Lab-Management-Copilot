"""Persistent shared queue and per-run capabilities for the local DSH service.

SQLite owns ordering and deduplication. Model requests never hold a database
transaction. Callbacks use independent connections and can create previews only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from . import chat
from . import ledger as stock
from .dsh_client import DSHUnavailable, run_dsh

TOOL_NAMES = frozenset({
    "inventory_search", "workflow_query", "history_search", "prepare_attachment",
    "inventory_import_preview", "business_preview", "export_inventory",
    "read_attachment", "analyze_attachments", "export_task",
})


def event(conn, conversation_id: str, kind: str, payload: dict) -> int:
    cursor = conn.execute(
        "INSERT INTO chat_events(conversation_id,kind,payload) VALUES (?,?,?)",
        (conversation_id, kind, chat.json_text(payload)),
    )
    return int(cursor.lastrowid)


def run_view(row) -> dict:
    value = {key: row[key] for key in ("id", "conversation_id", "message_id", "status", "actor", "device_id", "error", "started_at", "completed_at", "created_at", "cancel_requested")}
    value["run_id"] = value.pop("id")
    value["result"] = json.loads(row["result_json"] or "null")
    return value


def enqueue(conn, conversation_id: str, request: chat.ChatMessageRequest, actor: str, device_id: str) -> dict:
    if not request.text.strip() and not request.attachments:
        raise ValueError("请输入内容或添加附件")
    if not device_id or len(device_id) > 100:
        raise ValueError("设备编号无效")
    payload = request.model_dump()
    digest = hashlib.sha256(chat.json_text({key: value for key, value in payload.items() if key != "message_id"}).encode()).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    try:
        chat.authorize(conn, conversation_id, "", device_id)
        attachments = chat._attachments(conn, conversation_id, request.attachments)
        old = conn.execute("SELECT * FROM chat_messages WHERE conversation_id=? AND request_id=? AND role='user'", (conversation_id, request.message_id)).fetchone()
        if old:
            if old["request_hash"] and old["request_hash"] != digest:
                raise stock.Conflict("此消息编号已用于不同内容，请使用新的消息编号")
            run = conn.execute("SELECT * FROM chat_runs WHERE message_id=?", (old["id"],)).fetchone()
            if run:
                conn.commit()
                return run_view(run)
            reply = conn.execute("SELECT result_json FROM chat_messages WHERE conversation_id=? AND request_id=? AND role='assistant'", (conversation_id, request.message_id)).fetchone()
            if reply:
                conn.commit()
                return {"status": "completed", "result": json.loads(reply[0])}
            raise stock.Conflict("旧消息缺少运行记录，请发送新的继续说明")
        label = request.text.strip()
        if attachments:
            label += ("\n" if label else "") + "附件：" + "、".join(row["filename"] for row in attachments)
        cursor = conn.execute(
            "INSERT INTO chat_messages(conversation_id,request_id,role,content,request_hash,actor,device_id) VALUES (?,?,?,?,?,?,?)",
            (conversation_id, request.message_id, "user", label, digest, actor, device_id),
        )
        run_id = uuid.uuid4().hex
        conn.execute("INSERT INTO chat_runs(id,conversation_id,message_id,status,request_json,actor,device_id) VALUES (?,?,?,'queued',?,?,?)", (run_id, conversation_id, cursor.lastrowid, chat.json_text(payload), actor, device_id))
        if not chat._is_confirm(request.text) and not chat._is_cancel(request.text):
            expire_plans(conn, conversation_id, request.message_id)
        conn.execute("UPDATE chat_conversations SET version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (conversation_id,))
        event(conn, conversation_id, "message", {"message_id": cursor.lastrowid, "run_id": run_id})
        event(conn, conversation_id, "run", {"run_id": run_id, "status": "queued"})
        row = conn.execute("SELECT * FROM chat_runs WHERE id=?", (run_id,)).fetchone()
        conn.commit()
        return run_view(row)
    except Exception:
        conn.rollback()
        raise


def expire_plans(conn, conversation_id: str, request_id: str) -> None:
    for task in conn.execute("SELECT id,status FROM chat_tasks WHERE conversation_id=? AND status IN ('pending_confirmation','awaiting_details')", (conversation_id,)).fetchall():
        conn.execute("UPDATE chat_tasks SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE id=?", (task["id"],))
        chat._record_task_event(conn, task["id"], task["status"], "expired", "superseded_by_message", request_id)


def claim(conn, run_id: str | None = None) -> dict | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT r.* FROM chat_runs r WHERE r.status='queued' AND (? IS NULL OR r.id=?) "
            "AND NOT EXISTS (SELECT 1 FROM chat_runs a WHERE a.conversation_id=r.conversation_id AND a.status='running') "
            "AND NOT EXISTS (SELECT 1 FROM chat_runs q WHERE q.conversation_id=r.conversation_id AND q.status='queued' AND q.message_id<r.message_id) "
            "ORDER BY r.message_id LIMIT 1", (run_id, run_id),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        token = secrets.token_urlsafe(32)
        conn.execute("UPDATE chat_runs SET status='running',started_at=CURRENT_TIMESTAMP,callback_hash=? WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(), row["id"]))
        event(conn, row["conversation_id"], "run", {"run_id": row["id"], "status": "running"})
        result = dict(conn.execute("SELECT * FROM chat_runs WHERE id=?", (row["id"],)).fetchone())
        result["callback_token"] = token
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def execute_tool(conn, run_id: str, token: str, name: str, arguments: dict, dispatch_fn, history_fn) -> dict:
    """Authenticate the run and persist tool effects and results atomically."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM chat_runs WHERE id=?", (run_id,)).fetchone()
        if not row or not row["callback_hash"] or not hmac.compare_digest(row["callback_hash"], hashlib.sha256(token.encode()).hexdigest()):
            raise PermissionError("运行凭据无效")
        if row["status"] != "running" or row["cancel_requested"]:
            raise stock.Conflict("该运行已经结束或正在取消")
        if name not in TOOL_NAMES:
            raise ValueError("此工具未获允许")
        if row["tool_calls"] >= 40:
            raise ValueError("运行已达到四十次工具调用限制，请发送继续说明")
        conn.execute("UPDATE chat_runs SET tool_calls=tool_calls+1 WHERE id=?", (run_id,))
        try:
            args = chat.ToolArguments.model_validate(arguments).model_dump(exclude_none=True)
            attachments = [dict(value) for value in conn.execute("SELECT * FROM chat_attachments WHERE conversation_id=?", (row["conversation_id"],))]
            result = chat._cached_tool_result(conn, row["message_id"], name, args)
            if result is None:
                if name == "business_preview":
                    newer = conn.execute("SELECT 1 FROM chat_runs WHERE conversation_id=? AND message_id>? AND status IN ('queued','running')", (row["conversation_id"], row["message_id"])).fetchone()
                    if newer:
                        raise stock.Conflict("有更新的消息等待处理，本条消息的计划已失效")
                    result = chat._business_task(conn, row["conversation_id"], row["message_id"], json.loads(row["request_json"])["message_id"], args, attachments, dispatch_fn, row["actor"])
                elif name == "analyze_attachments":
                    from . import analysis

                    selected = chat._attachments(conn, row["conversation_id"], args.get("attachment_ids") or [])
                    digest = hashlib.sha256(chat.json_text(args).encode()).hexdigest()
                    # Reserve the call first. No write transaction spans execution.
                    conn.commit()
                    result = analysis.run(conn, row["conversation_id"], args.get("code") or "", selected, run_id=run_id, arguments_hash=digest)
                    conn.execute("BEGIN IMMEDIATE")
                    live = conn.execute("SELECT status,cancel_requested FROM chat_runs WHERE id=?", (run_id,)).fetchone()
                    if live["status"] != "running" or live["cancel_requested"]:
                        result = {"status": "cancelled", "reply": "运行已取消，分析记录保留，未修改业务数据。", "job_id": result["job_id"]}
                else:
                    result = chat._run_read_tool(conn, row["conversation_id"], name, args, attachments, history_fn)
                chat._record_tool_call(conn, row["conversation_id"], row["message_id"], name, args, result, "school/deepseek-pro", run_id=run_id)
        except (ValueError, ValidationError, KeyError, TypeError) as exc:
            result = {"status": "needs_details", "error": str(exc), "reply": str(exc)}
            chat._record_tool_call(conn, row["conversation_id"], row["message_id"], name, arguments, result, "school/deepseek-pro", run_id=run_id)
        event(conn, row["conversation_id"], "tool", {"run_id": run_id, "name": name, "result": result})
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def process_run(conn, run: dict, dispatch_fn, history_fn) -> dict:
    request = chat.ChatMessageRequest.model_validate_json(run["request_json"])
    conversation_id = run["conversation_id"]
    pending = conn.execute("SELECT * FROM chat_tasks WHERE conversation_id=? AND status='pending_confirmation' ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone()
    try:
        if chat._is_confirm(request.text):
            if not pending or request.task_id != pending["id"] or request.plan_hash != pending["plan_hash"]:
                response = {"status": "needs_details", "reply": "请先查看当前完整预览，再确认执行。"}
            else:
                result = chat.confirm_task(conn, pending, run["actor"], run["device_id"], request.message_id, dispatch_fn, expected_plan_hash=request.plan_hash, run_id=run["id"])
                response = {"status": "completed", "task_id": pending["id"], "result": result, "reply": "已完成确认的操作。以下结果来自实际业务记录。"}
        elif chat._is_cancel(request.text):
            if not pending or request.task_id != pending["id"] or request.plan_hash != pending["plan_hash"]:
                response = {"status": "needs_details", "reply": "请查看要取消的计划，再使用该计划的取消按钮。"}
            else:
                response = chat.cancel_task(conn, pending["id"], request.message_id, request.plan_hash)
                conn.commit()
                response["reply"] = "已取消待确认计划，没有修改库存。"
        else:
            conn.commit()
            payload = {
                "runId": run["id"], "conversationId": conversation_id,
                "callbackToken": run["callback_token"], "systemPrompt": chat.CHAT_SYSTEM,
                "text": chat.json_text({
                    "current_message": request.text,
                    "history": chat._recent_history(conn, conversation_id),
                    "attachments": [{key: value for key, value in dict(item).items() if key != "path"} for item in conn.execute("SELECT * FROM chat_attachments WHERE conversation_id=?", (conversation_id,))],
                    "current_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                }),
            }
            from .attachments import image_payload

            selected = chat._attachments(conn, conversation_id, request.attachments)
            payload["images"] = [image_payload(Path(item["path"]), item["filename"]) for item in selected if item["content_type"].startswith("image/")]
            result = run_dsh(payload)
            # The preview is the business server's result, never a model claim.
            saved = conn.execute("SELECT result_json FROM chat_tool_calls WHERE run_id=? AND name='business_preview' ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
            preview = json.loads(saved[0]) if saved else None
            if preview and preview.get("status") == "pending_confirmation":
                response = preview
            elif result.get("status") == "completed":
                tools = [{"tool": item["name"], "result": json.loads(item["result_json"])} for item in conn.execute("SELECT name,result_json FROM chat_tool_calls WHERE run_id=? ORDER BY id", (run["id"],))]
                response = {"status": "answered", "reply": verified_reply(result.get("reply") or "", tools), "tool_results": tools}
            else:
                response = {"status": "failed", "reply": "本次对话处理未完成，输入和任务已保存。可以发送继续说明。"}
        return finish(conn, run, response)
    except chat.PlanChanged as exc:
        return finish(conn, run, exc.response)
    except (DSHUnavailable, ValueError) as exc:
        conn.rollback()
        return finish(conn, run, {"status": "failed", "reply": str(exc)})


CLARIFICATION_FIELDS = {
    "item_name": "耗材名称", "spec": "规格", "unit": "单位",
    "quantity": "数量", "lab": "实验室", "location_code": "具体位置",
    "action": "操作方式（消耗、借用、入库或其他操作）",
    "borrower": "借用人", "purpose": "用途", "destination": "存放地点",
    "due_at": "预计归还时间", "return_state": "归还状态",
    "starts_at": "实验开始时间", "ends_at": "实验结束时间",
    "people": "人数", "group_size": "每组人数", "course": "课程",
    "venue": "实验地点", "name": "任务名称", "owner": "负责人",
    "expected_at": "预计到货时间", "note": "操作原因",
    "attachment": "清单附件", "scope": "需要处理的条目和范围",
    "conversion": "包装单位与基本单位的换算关系",
    "record": "要处理的原始业务记录",
}


def verified_reply(model_text: str, tools: list[dict]) -> str:
    """Only server-owned wording can assert a business outcome or quantity.

    The model can select missing-field identifiers; free-form assertions are not
    displayed. Query facts remain available in the persisted tool result cards.
    """
    try:
        value = json.loads(model_text.removeprefix("```json").removesuffix("```").strip())
        fields = value.get("missing_fields", []) if isinstance(value, dict) else []
        fields = list(dict.fromkeys(field for field in fields if isinstance(field, str) and field in CLARIFICATION_FIELDS)) if isinstance(fields, list) else []
    except (ValueError, TypeError):
        fields = []
    if fields:
        return "请补充以下资料：" + "、".join(CLARIFICATION_FIELDS[field] for field in fields) + "。当前没有执行新的业务变更。"
    if tools:
        if any(entry["result"].get("status") in {"needs_details", "failed"} for entry in tools):
            return "请查看服务器返回的资料核对要求。当前没有执行新的业务变更。"
        return "以下内容来自服务器保存的查询或分析结果。业务变更需要另行生成完整预览并确认。"
    return "请说明要查询或处理的耗材、规格、位置和操作内容，也可以添加清单附件。当前没有执行新的业务变更。"


def finish(conn, run: dict, response: dict) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        live = conn.execute("SELECT * FROM chat_runs WHERE id=?", (run["id"],)).fetchone()
        executed = response.get("status") == "completed" and "result" in response
        if live["cancel_requested"] and not executed:
            response = {"status": "cancelled", "reply": "该运行已取消。已确认的业务记录保留，未确认的计划不执行。"}
        response["run_id"] = run["id"]
        status = "cancelled" if response["status"] == "cancelled" else "failed" if response["status"] == "failed" else "completed"
        if status in {"cancelled", "failed"}:
            for task in conn.execute("SELECT id,status FROM chat_tasks WHERE message_id=? AND status IN ('pending_confirmation','awaiting_details')", (run["message_id"],)).fetchall():
                conn.execute("UPDATE chat_tasks SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE id=?", (task["id"],))
                chat._record_task_event(conn, task["id"], task["status"], "expired", "run_interrupted")
        request = json.loads(run["request_json"])
        chat._assistant_message(conn, run["conversation_id"], request["message_id"], response)
        conn.execute("UPDATE chat_runs SET status=?,result_json=?,callback_hash=NULL,error=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (status, chat.json_text(response), response.get("reply") if status == "failed" else None, run["id"]))
        event(conn, run["conversation_id"], "run", {"run_id": run["id"], "status": status})
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise


def cancel_run(conn, run_id: str) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM chat_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("运行不存在")
        if row["status"] in {"queued", "running"}:
            conn.execute("UPDATE chat_runs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=?", (run_id,))
            for task in conn.execute("SELECT * FROM chat_tasks WHERE message_id=? AND status='pending_confirmation'", (row["message_id"],)).fetchall():
                chat.cancel_task(conn, task["id"], expected_plan_hash=task["plan_hash"])
            event(conn, row["conversation_id"], "run", {"run_id": run_id, "status": "cancelling" if row["status"] == "running" else "cancelled"})
        conn.commit()
        return {"run_id": run_id, "status": "cancelling" if row["status"] == "running" else row["status"] if row["status"] != "queued" else "cancelled"}
    except Exception:
        conn.rollback()
        raise


def recover(conn) -> None:
    """Keep queued inputs; mark interrupted runs visible, without replaying them."""
    rows = conn.execute("SELECT * FROM chat_runs WHERE status='running'").fetchall()
    for row in rows:
        finish(conn, dict(row), {"status": "failed", "reply": "服务器重新启动前的运行已中断，消息和任务仍保留。请查看现有结果后发送继续说明。"})
    conn.execute("UPDATE analysis_jobs SET status='interrupted',result_json=? WHERE status='running'", (chat.json_text({"status": "interrupted", "reply": "分析因服务器重新启动而中断，请发送新的分析请求。"}),))
    conn.commit()
