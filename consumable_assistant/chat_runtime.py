"""Persistent shared queue and per-run capabilities for the local DSH service.

SQLite owns ordering and deduplication. Model requests never hold a database
transaction. Callbacks use independent connections and can create previews only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from . import chat, settings
from . import ledger as stock
from .dsh_client import DSHUnavailable, result_error_code, run_dsh
from .genai import parse_model_json

TOOL_NAMES = frozenset({
    "inventory_search", "workflow_query", "history_search", "prepare_attachment",
    "inventory_import_preview", "business_preview", "export_inventory",
    "read_attachment", "analyze_attachments", "export_task", "export_history",
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
        if chat._edits_plan(request.text):
            expire_plans(conn, conversation_id, request.message_id, through=cursor.lastrowid)
        conn.execute("UPDATE chat_conversations SET version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (conversation_id,))
        event(conn, conversation_id, "message", {"message_id": cursor.lastrowid, "run_id": run_id})
        event(conn, conversation_id, "run", {"run_id": run_id, "status": "queued"})
        row = conn.execute("SELECT * FROM chat_runs WHERE id=?", (run_id,)).fetchone()
        conn.commit()
        return run_view(row)
    except Exception:
        conn.rollback()
        raise


def expire_plans(conn, conversation_id: str, request_id: str, *, through: int | None = None) -> None:
    tasks = conn.execute(
        "SELECT id,status FROM chat_tasks WHERE conversation_id=? AND status IN ('pending_confirmation','awaiting_details') "
        "AND (? IS NULL OR message_id<=?)", (conversation_id, through, through),
    ).fetchall()
    for task in tasks:
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
            request = chat.ChatMessageRequest.model_validate_json(row["request_json"])
            if name in {"read_attachment", "prepare_attachment"}:
                attachment = chat._find_attachment(attachments, args.get("attachment_id") or "")
                if attachment and attachment["content_type"].startswith("image/") and attachment["id"] not in request.attachments:
                    raise ValueError("请把要识别的照片随当前消息一起发送")
            if name == "inventory_import_preview" and args.get("mode") == "count_full":
                if not re.search(r"整柜|全柜|整个柜|全部清点|表外.*(?:归零|为零|没有)|未列.*(?:归零|为零)", request.text):
                    raise ValueError("整柜实盘会把表外同科目库存归零，请在消息中明确说明已经清点整个柜位")
                if re.search(r"不要|不能|暂不|只清点部分|部分实盘", request.text):
                    raise ValueError("这条消息未明确要求整柜实盘，请说明本次清点范围")
            if name == "prepare_attachment" or (name == "inventory_import_preview" and args.get("mode") in chat.flow.COUNT_MODES):
                for field, value in chat.checklist_chat_scope(conn, request.text).items():
                    if args.get(field) and str(args[field]).strip().casefold() != str(value).casefold():
                        raise ValueError("清单参数与消息中指定的取用科目、房间或柜位不一致，请重新核对")
                    args[field] = value
            result = chat._cached_tool_result(conn, row["message_id"], name, args)
            if result is None:
                if name == "business_preview":
                    newer = conn.execute(
                        "SELECT request_json FROM chat_runs WHERE conversation_id=? AND message_id>? "
                        "AND status IN ('queued','running')", (row["conversation_id"], row["message_id"]),
                    )
                    if any(chat._edits_plan(json.loads(other["request_json"])["text"]) for other in newer):
                        raise stock.Conflict("已有更新的修改说明，本条消息的计划已失效")
                    if conn.execute(
                        "SELECT 1 FROM chat_tasks WHERE conversation_id=? AND message_id>? AND status='pending_confirmation'",
                        (row["conversation_id"], row["message_id"]),
                    ).fetchone():
                        raise stock.Conflict("已有较新的确认计划，请查看当前计划")
                    steps = (args.get("payload") or {}).get("operations", []) if args.get("kind") == "mixed" else [args]
                    selected = chat._attachments(conn, row["conversation_id"], request.attachments)
                    if selected and any(
                        step.get("kind") == "stock" and (step.get("payload") or {}).get("action") == "consume"
                        or step.get("kind") == "bulk" and any(
                            operation.get("action") == "consume"
                            for operation in (step.get("payload") or {}).get("operations", [])
                        )
                        for step in steps
                    ):
                        raise ValueError("附件中的课堂消耗须先核对清单，再由 prepare 登记原表条目")
                    for step in steps:
                        if step.get("kind") == "prepare":
                            reason = str((step.get("payload") or {}).get("repeat_reason") or "").strip()
                            if reason and reason not in request.text:
                                raise ValueError("请在本条消息中说明再次登记的实际原因")
                    conn.execute("SAVEPOINT replace_plan")
                    try:
                        expire_plans(conn, row["conversation_id"], request.message_id, through=row["message_id"])
                        result = chat._business_task(conn, row["conversation_id"], row["message_id"], request.message_id, args, attachments, dispatch_fn, row["actor"])
                    except Exception:
                        conn.execute("ROLLBACK TO replace_plan")
                        raise
                    finally:
                        conn.execute("RELEASE replace_plan")
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
                chat._record_tool_call(conn, row["conversation_id"], row["message_id"], name, args, result, f"school/{settings.GENAI_MODEL}", run_id=run_id)
        except (ValueError, ValidationError, KeyError, TypeError) as exc:
            result = {"status": "needs_details", "error": str(exc), "reply": str(exc)}
            chat._record_tool_call(conn, row["conversation_id"], row["message_id"], name, arguments, result, f"school/{settings.GENAI_MODEL}", run_id=run_id)
        event(conn, row["conversation_id"], "tool", {"run_id": run_id, "name": name, "result": result})
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _registration_lookup(text: str, history: list[dict]) -> dict | None:
    if not re.search(
        r"(?:(?:帮我|请|需要|我要|现在|继续|确认要|直接)(?:把这笔|给我)?(?:登记|办理|生成预览)|"
        r"帮我记录|(?:就|先)?从这里(?:拿|取|借|领|消耗)|帮我(?:借|领|入库|出库|消耗))",
        text,
    ):
        return None
    if re.search(r"如何|怎样|怎么|不要|不用|先不|暂不", text):
        return None
    for turn in reversed(history):
        if turn["status"] in {"completed", "pending_confirmation", "cancelled"}:
            break
        searches = [entry["result"].get("search") for entry in turn["tool_results"]
                    if entry["tool"] == "inventory_search" and entry["result"].get("rows")]
        if searches:
            if len(searches) != 1 or not searches[0] or not searches[0].get("q"):
                return None
            search = searches[0]
            return {"query": search["q"], **{field: search[field] for field in
                    ("category", "lab", "location_code", "starts_at", "ends_at") if search.get(field)}}
        if turn["tool_results"]:
            break
    return None


def process_run(conn, run: dict, dispatch_fn, history_fn) -> dict:
    request = chat.ChatMessageRequest.model_validate_json(run["request_json"])
    conversation_id = run["conversation_id"]
    pending = conn.execute(
        "SELECT * FROM chat_tasks WHERE conversation_id=? AND id=?",
        (conversation_id, request.task_id),
    ).fetchone()
    try:
        if chat._is_confirm(request.text):
            if request.attachments or not pending or request.plan_hash != pending["plan_hash"]:
                response = {"status": "needs_details", "reply": "请先查看当前完整预览，再单独发送确认文字或使用确认按钮。"}
            else:
                result = chat.confirm_task(conn, pending, run["actor"], run["device_id"], request.message_id, dispatch_fn, expected_plan_hash=request.plan_hash, run_id=run["id"])
                response = {"status": "completed", "task_id": pending["id"], "result": result, "reply": "已完成确认的操作。以下结果来自实际业务记录。"}
        elif chat._is_cancel(request.text):
            if request.attachments or not pending or request.plan_hash != pending["plan_hash"]:
                response = {"status": "needs_details", "reply": "请查看要取消的计划，再使用该计划的取消按钮。"}
            else:
                response = chat.cancel_task(conn, pending["id"], request.message_id, request.plan_hash)
                conn.commit()
                response["reply"] = "已取消待确认计划，没有修改库存。"
        else:
            conn.commit()
            selected = chat._attachments(conn, conversation_id, request.attachments)
            history = chat._recent_history(conn, conversation_id, run["message_id"])
            payload = {
                "runId": run["id"], "conversationId": conversation_id,
                "callbackToken": run["callback_token"], "systemPrompt": chat.CHAT_SYSTEM,
                "text": chat.json_text({
                    "current_attachment_ids": request.attachments,
                    "history": history,
                    "current_plan": json.loads(pending["plan_json"]) if pending and pending["status"] == "pending_confirmation" else None,
                    "plan_to_revise": json.loads(pending["plan_json"]) if pending and pending["status"] == "expired" and chat._edits_plan(request.text) else None,
                    "attachments": [{key: value for key, value in dict(item).items() if key != "path"} for item in conn.execute("SELECT * FROM chat_attachments WHERE conversation_id=?", (conversation_id,))],
                    "current_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                    "current_message": request.text,
                }),
            }
            from .attachments import image_payload

            payload["images"] = [image_payload(Path(item["path"]), item["filename"]) for item in selected if item["content_type"].startswith("image/")]
            result = run_dsh(payload)
            lookup = _registration_lookup(request.text, history)
            refreshed = False
            if history and result.get("status") == "completed":
                calls, cancelled = conn.execute("SELECT tool_calls,cancel_requested FROM chat_runs WHERE id=?",
                                                (run["id"],)).fetchone()
                if not calls and not cancelled and (lookup or not reply_guidance(result.get("reply", ""))):
                    retry_context = json.loads(payload["text"])
                    if lookup:
                        fresh = execute_tool(conn, run["id"], run["callback_token"], "inventory_search",
                                             lookup, dispatch_fn, history_fn)
                        refreshed = True
                        retry_context["previous_search_rechecked"] = {
                            "search": fresh.get("search"), "total_count": fresh.get("total_count"),
                            "rows": chat.context_stock_rows(fresh.get("rows", [])),
                        }
                    result = run_dsh({
                        **payload, "conversationId": run["id"], "text": chat.json_text(retry_context),
                        "systemPrompt": payload["systemPrompt"] + "\n上次处理没有产生业务工具结果。"
                        "请结合 history 和 current_message 接续任务，使用与本次业务对应的工具。对取用登记先核实各来源，"
                        "已有 previous_search_rechecked 时可直接使用与本次要求相符的记录。"
                        "生成 business_preview；资料不足时返回只含实际缺项的 missing_fields。",
                    })
            # The preview is the business server's result, never a model claim.
            saved = conn.execute(
                "SELECT * FROM chat_tasks WHERE message_id=? AND status='pending_confirmation' ORDER BY rowid DESC LIMIT 1",
                (run["message_id"],),
            ).fetchone()
            tools = [{"tool": item["name"], "result": json.loads(item["result_json"])}
                     for item in conn.execute("SELECT name,result_json FROM chat_tool_calls WHERE run_id=? ORDER BY id", (run["id"],))]
            if saved:
                preview_data = json.loads(saved["preview_json"])
                response = {
                    "status": "pending_confirmation", "task_id": saved["id"], "plan_hash": saved["plan_hash"],
                    "expires_at": saved["expires_at"], "preview": preview_data,
                    "tool_results": [tool for tool in tools if tool["tool"] in {"prepare_attachment", "inventory_import_preview"}],
                    "reply": _preview_reply(preview_data, result.get("reply") or ""),
                }
            elif result.get("status") == "completed":
                reply = result.get("reply") or ""
                if refreshed and not reply_guidance(reply, tools) and len(tools) == 1:
                    response = {"status": "needs_details", "tool_results": tools,
                                "reply": "已复核上次查询的库存。请补充具体取用柜位，或说明是消耗还是借用（借用需借用人、用途、使用地点及预计归还时间）后继续登记。"}
                elif history and not tools and not reply_guidance(reply):
                    response = {"status": "needs_details", "tool_results": [],
                                "reply": "本轮暂未生成新的查询或变更预览。请补充具体耗材名称、规格、位置或要办理的操作，我会继续为您处理。"}
                else:
                    response = {"status": "answered", "reply": verified_reply(reply, tools), "tool_results": tools}
            else:
                response = {"status": "failed", "reply": "本次对话处理未完成，输入和任务已保存。可以发送继续说明。"}
                response["error_code"] = result_error_code(result)
                if refreshed:
                    response["tool_results"] = tools
        return finish(conn, run, response)
    except chat.PlanChanged as exc:
        return finish(conn, run, exc.response)
    except (DSHUnavailable, ValueError) as exc:
        conn.rollback()
        response = {"status": "failed", "reply": str(exc)}
        if isinstance(exc, DSHUnavailable):
            response["error_code"] = exc.code
        return finish(conn, run, response)


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


EXPLANATIONS = {
    "availability": "总拥有数量包括可用状态、借出、待清洗、维修和待核对的物品。可用数量从可用状态数量中扣除相关预留；借出、待清洗、维修和待核对的物品不能直接分配。指定实验时间后，服务器按该时间段检查预留。清单的可用数量表示全部可用库存，本次分配数量表示本次需求实际占用的部分，两者可能不同。",
    "consume_borrow": "消耗会减少总拥有数量。借用只把数量从可用状态转为借出，总拥有数量不变。归还必须关联原借用记录，可以部分归还，并注明可用、待清洗、破损或遗失。待清洗物品完成清洗并确认状态变更后才重新可用。",
    "preparation": "上传实验清单，或者说明人数、每组人数及每人、每组、固定和备用用量。核对名称、规格、单位、消耗或借用方式及实验时间后，系统显示各位置的可用数量和分配，生成一次确认的完整计划。不足时保留已有预留、未满足需求和采购草案。预留不等于实际取用，实验执行时需要另外确认消耗或借用。",
    "procurement": "采购草案仅记录需求，不表示已经订购，也不增加库存。确认订购后可以登记部分到货，每次到货只增加实际数量。新耗材需要填写实际存放房间和位置，实际到货确认后才建立库存记录。到货不会自动预留，需要重新预览并确认补充预留。",
    "confirmation": "查询直接执行。库存、预留、采购和资料变更均先显示完整预览，确认后才执行。每个完整计划确认一次，重复确认返回原结果。预览有效期为三十分钟，内容或相关数据变化后需要重新预览和确认。",
    "attachments": "可以上传表格或清单照片。系统保留原文件和条目来源，照片识别结果先形成可修改的清单。请核对名称、规格、数量、单位和使用方式，模糊内容需要补充。表格只读取数据和有限的加减乘除计算，不执行宏或外部代码。清单核对本身不会修改库存。",
    "rooms": "房间和柜子分别筛选。来源表将多个房间合并时，只有位置编码明确注明房间的记录才属于其中的具体房间；无法确定的记录保留合并名称，等待核对。查询结果和导出保留原始来源记录。",
    "notifications": "在使用设置中允许通知，并检查当前设备是否已订阅。iPhone 需要将网站添加至主屏幕并从主屏幕进入；Android 可以通过 Chrome 安装网站应用。使用测试通知检查关闭页面和锁定屏幕后是否仍能收到。通知只显示通用提醒，详情需要校园网或学校 VPN。",
    "correction": "修改人数、数量、时间、位置或处理范围后，原计划不能继续确认，需要生成新的完整预览。已经执行的操作保留历史；允许撤销的库存流水可预览撤销，已有后续依赖时服务器会说明原因。已经实际取用的实验准备不能直接取消或改写原需求。",
}


_INTERNAL_TERMS = (
    ("inventory_search", "库存查询"),
    ("workflow_query", "事务查询"),
    ("history_search", "历史记录查询"),
    ("prepare_attachment", "清单核对"),
    ("inventory_import_preview", "表格导入预览"),
    ("business_preview", "业务预览"),
    ("analyze_attachments", "附件分析"),
    ("export_inventory", "导出库存"),
    ("export_history", "导出历史"),
    ("export_task", "导出任务"),
    ("read_attachment", "读取附件"),
    ("location_code", "柜位"),
    ("source_quote", "来源说明"),
    ("missing_fields", "待补充信息"),
    ("explanation_topics", "说明主题"),
    ("item_id", "物品编号"),
)

_FALSE_EXECUTION_RE = re.compile(
    r"(?:已为您|已经为您|现已|本次已)?(?:成功)?(?:执行|完成|办好|扣除|扣减)(?:了)?(?:本次)?(?:消耗|入库|出库|借用|借出|归还|清点|转移|停用|撤销|库存变更|登记)"
)


def _sanitize_model_reply(raw: str, tools: list[dict] | None = None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    for term, label in _INTERNAL_TERMS:
        text = re.sub(rf"`?\b{re.escape(term)}\b`?", label, text)
    if _FALSE_EXECUTION_RE.search(text):
        return ""
    if tools is not None:
        if not tools and re.search(r"库存(?:现在|目前|当前|还剩|共有|为|是)\s*\d+", text):
            return ""
        inv_calls = [t["result"] for t in tools if t.get("tool") == "inventory_search" and isinstance(t.get("result"), dict)]
        if inv_calls and all(
            t.get("type") != "inventory_summary" and not (t.get("rows") or t.get("items"))
            for t in inv_calls
        ):
            if re.search(r"(?:库存|现有|可用|剩余)[^\n。；]{0,12}[1-9]\d*", text):
                return ""
    return text.strip()


def _preview_reply(preview: dict, model_text: str = "") -> str:
    changes = preview.get("changes") or []
    lines = []
    for item in changes[:8]:
        title = item.get("item_name") or "耗材"
        if item.get("spec"):
            title += f"（{item['spec']}）"
        place = " · ".join(part for part in (item.get("lab"), item.get("location_code")) if part)
        unit = item.get("unit") or ""
        before = (item.get("before") or {}).get("available_quantity", "新增")
        after = (item.get("after") or {}).get("available_quantity", "停用")
        prefix = f"[{place}] " if place else ""
        lines.append(f"- {prefix}**{title}**：可用数量 `{before}` → `{after}` {unit}".rstrip())
    if len(changes) > 8:
        lines.append(f"- 其余 {len(changes) - 8} 项变更详见预览卡片")
    if lines:
        return (
            "已为您生成**待确认预览**（当前尚未修改库存）：\n"
            + "\n".join(lines)
            + "\n\n请核对预览卡片中的物品、位置和数量，确认无误后点击**确认执行**。"
        )
    parsed = parse_model_json(model_text) or {}
    note = _sanitize_model_reply(parsed.get("reply") if isinstance(parsed.get("reply"), str) else "")
    if note:
        return note + "\n\n请核对预览卡片内容，确认无误后点击**确认执行**（当前尚未修改库存）。"
    return "已按当前数据生成**待确认预览**（当前尚未修改库存）。请核对物品、位置、数量和状态，确认无误后点击**确认执行**。"


def reply_guidance(model_text: str, tools: list[dict] | None = None) -> str:
    parsed = parse_model_json(model_text)
    if isinstance(parsed, dict):
        topics = parsed.get("explanation_topics", [])
        fields = parsed.get("missing_fields", [])
        fields = list(dict.fromkeys(field for field in fields if isinstance(field, str) and field in CLARIFICATION_FIELDS)) if isinstance(fields, list) else []
        explanations = [EXPLANATIONS[topic] for topic in dict.fromkeys(topic for topic in topics if isinstance(topic, str)) if topic in EXPLANATIONS] if isinstance(topics, list) else []
        raw_reply = parsed.get("reply") if isinstance(parsed.get("reply"), str) else ""
        clean_reply = _sanitize_model_reply(raw_reply, tools)
        if explanations:
            base = clean_reply if clean_reply else "\n\n".join(explanations)
            if fields:
                missing_labels = [CLARIFICATION_FIELDS[f] for f in fields if CLARIFICATION_FIELDS[f] not in base]
                if missing_labels:
                    base += "\n\n请补充：" + "、".join(missing_labels) + "。"
            return base
        if fields:
            labels = [CLARIFICATION_FIELDS[field] for field in fields]
            if clean_reply:
                missing_labels = [label for label in labels if label not in clean_reply]
                if missing_labels:
                    return clean_reply + "\n\n请补充以下资料：" + "、".join(missing_labels) + "。"
                return clean_reply
            return "请补充以下资料：" + "、".join(labels) + "。当前没有执行新的业务变更。"
        if clean_reply:
            return clean_reply
        return ""
    prose = (model_text or "").strip()
    if len(prose) >= 12:
        return _sanitize_model_reply(prose, tools)
    return ""


def verified_reply(model_text: str, tools: list[dict]) -> str:
    if tools:
        failed_msgs = [
            str(entry["result"].get("reply") or entry["result"].get("error") or "").strip()
            for entry in tools
            if isinstance(entry.get("result"), dict) and entry["result"].get("status") in {"needs_details", "failed"}
        ]
        failed_msgs = [msg for msg in dict.fromkeys(failed_msgs) if msg]
    else:
        failed_msgs = []
    guidance = reply_guidance(model_text, tools)
    if guidance:
        if failed_msgs and not any(msg in guidance for msg in failed_msgs):
            return guidance + "\n\n提示：" + "；".join(failed_msgs)
        return guidance
    if tools:
        if failed_msgs:
            return "需要核对以下信息：" + "；".join(failed_msgs) + "。当前没有执行新的业务变更。"
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
        error = (response.get("error_code") or response.get("reply")) if status == "failed" else None
        conn.execute("UPDATE chat_runs SET status=?,result_json=?,callback_hash=NULL,error=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (status, chat.json_text(response), error, run["id"]))
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
