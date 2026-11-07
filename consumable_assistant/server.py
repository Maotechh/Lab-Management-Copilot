from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from pydantic import BaseModel, Field

from . import chat as chat_session
from . import chat_runtime, dsh_client
from . import ledger as stock
from . import workflows as flow
from .database import connect, init_db, inventory_is_empty
from .importer import import_all_sources
from .services import (
    ACTION_LABELS,
    export_inventory_workbook,
    export_prepare_workbook,
    get_stats,
    list_alerts,
    list_locations,
    parse_transaction_preview,
    preview_location,
    save_upload_to_temp,
    search_items,
)
from .settings import (
    AUTO_SEED,
    CHAT_UPLOAD_DIR,
    CONSUMABLE_PROFILE,
    PHOTO_DIR,
    STATIC_DIR,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    with connect() as conn:
        init_db(conn)
        if AUTO_SEED and inventory_is_empty(conn):
            import_all_sources(conn)
        stock.initialize_balances(conn)
        for row in conn.execute("SELECT item_id FROM stock_balances").fetchall():
            stock.alerts_changed(conn, row["item_id"])
        conn.commit()
    with connect() as conn:
        chat_runtime.recover(conn)
    stopping = asyncio.Event()
    worker = asyncio.create_task(chat_worker(stopping))
    try:
        yield
    finally:
        stopping.set()
        await worker


def process_queued_run(run: dict) -> None:
    with connect() as conn:
        try:
            chat_runtime.process_run(conn, run, dispatch, history_rows)
        except Exception:
            conn.rollback()
            logging.exception("DSH run failed: %s", run["id"])
            chat_runtime.finish(conn, run, {"status": "failed", "reply": "本次运行发生错误，消息已保存。请查看任务后发送继续说明。"})


async def chat_worker(stopping: asyncio.Event) -> None:
    active: set[asyncio.Task] = set()
    while not stopping.is_set():
        active = {task for task in active if not task.done()}
        if len(active) < 4:
            with connect() as conn:
                run = chat_runtime.claim(conn)
            if run:
                active.add(asyncio.create_task(asyncio.to_thread(process_queued_run, run)))
                continue
        try:
            await asyncio.wait_for(stopping.wait(), timeout=0.2)
        except TimeoutError:
            pass
    with connect() as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM chat_runs WHERE status='running'")]
        for run_id in ids:
            chat_runtime.cancel_run(conn, run_id)
            try:
                await asyncio.to_thread(dsh_client.cancel_dsh, run_id)
            except dsh_client.DSHUnavailable:
                logging.warning("DSH cancellation remains pending: %s", run_id)
    await asyncio.gather(*active, return_exceptions=True)


app = FastAPI(title="耗材智能管理助手", version="0.2.0", lifespan=lifespan)


class ChatPrepareRequest(BaseModel):
    operations: list[dict] = Field(min_length=1, max_length=200)
    plan_hash: str = Field(min_length=64, max_length=64)


class ChatCancelRequest(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=64)


class ChatConfirmRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=100)
    plan_hash: str = Field(min_length=64, max_length=64)


@app.middleware("http")
async def same_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and origin
        and urlsplit(origin).netloc != request.headers.get("host")
    ):
        return JSONResponse({"detail": "只允许同源网页提交操作"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def validation_error(request: Request, exc: ValueError):
    return JSONResponse(
        {"detail": str(exc), **({"refreshed_plan": exc.response} if isinstance(exc, chat_session.PlanChanged) else {})},
        status_code=409 if isinstance(exc, stock.Conflict) else 400,
    )


@app.exception_handler(sqlite3.OperationalError)
async def database_error(request: Request, exc: sqlite3.OperationalError):
    return JSONResponse(
        {"detail": "数据库暂时忙碌，请查询原请求结果后重试"}, status_code=503
    )


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def app_config():
    notifications_configured = all(
        os.environ.get(key)
        for key in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "VAPID_SUBJECT")
    ) and os.environ.get("PUBLIC_BASE_URL", "").startswith("https://")
    return {
        "profile": CONSUMABLE_PROFILE,
        "simulated": CONSUMABLE_PROFILE == "simulation",
        "write_access": True,
        "inventory_confirmed": os.environ.get("CONSUMABLE_INVENTORY_CONFIRMED", "0") == "1",
        "notifications_configured": bool(notifications_configured),
    }


def chat_authorize(conn, conversation_id: str, request: Request):
    return chat_session.authorize(
        conn,
        conversation_id,
        request.headers.get("x-chat-token", ""),
        request.headers.get("x-device-id", ""),
    )


@app.post("/api/chat/conversations")
def create_chat_conversation(request: Request):
    with connect() as conn:
        return chat_session.new_conversation(
            conn, request.headers.get("x-device-id", "")
        )


@app.get("/api/chat/conversations/{conversation_id}")
def get_chat_conversation(conversation_id: str, request: Request, before: int | None = None, limit: int = 100):
    with connect() as conn:
        conn.execute("BEGIN")
        chat_authorize(conn, conversation_id, request)
        return chat_session.conversation_view(conn, conversation_id, before, limit)


@app.post("/api/chat/conversations/{conversation_id}/messages")
def send_chat_message(
    conversation_id: str, payload: chat_session.ChatMessageRequest, request: Request
):
    with connect() as conn:
        chat_authorize(conn, conversation_id, request)
        return chat_runtime.enqueue(conn, conversation_id, payload, actor_name(request), request.headers.get("x-device-id", ""))


@app.get("/api/chat/conversations")
def list_chat_conversations(before: int | None = None, limit: int = 100):
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.rowid AS cursor,c.id,c.version,c.updated_at,(SELECT content FROM chat_messages m WHERE m.conversation_id=c.id ORDER BY m.id LIMIT 1) AS title "
            "FROM chat_conversations c WHERE (? IS NULL OR c.rowid<?) ORDER BY c.rowid DESC LIMIT ?", (before, before, min(max(limit, 1), 100)),
        )
        values = [dict(row) for row in rows]
        return {"conversations": values, "shared": True, "next_before": values[-1]["cursor"] if len(values) == min(max(limit, 1), 100) else None}


@app.get("/api/chat/runs/{run_id}")
def get_chat_run(run_id: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM chat_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "运行不存在")
        return chat_runtime.run_view(row)


@app.post("/api/chat/runs/{run_id}/cancel")
async def cancel_chat_run(run_id: str):
    with connect() as conn:
        result = chat_runtime.cancel_run(conn, run_id)
    if result["status"] == "cancelling":
        try:
            await asyncio.to_thread(dsh_client.cancel_dsh, run_id)
        except dsh_client.DSHUnavailable:
            result["notice"] = "取消请求已保存，后续工具调用已禁止。"
    return result


@app.get("/api/chat/conversations/{conversation_id}/events")
async def chat_events(conversation_id: str, request: Request, after: int = 0):
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError as exc:
        raise HTTPException(400, "事件编号无效") from exc
    with connect() as conn:
        chat_authorize(conn, conversation_id, request)

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            with connect() as conn:
                rows = conn.execute("SELECT id,kind,payload FROM chat_events WHERE conversation_id=? AND id>? ORDER BY id LIMIT 100", (conversation_id, cursor)).fetchall()
            for row in rows:
                cursor = row["id"]
                yield f"id: {cursor}\nevent: {row['kind']}\ndata: {row['payload']}\n\n"
            if not rows:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


class DSHCallbackRequest(BaseModel):
    name: str = Field(max_length=100)
    arguments: dict


@app.post("/api/internal/dsh/{run_id}/tools")
def dsh_tool_callback(run_id: str, payload: DSHCallbackRequest, request: Request):
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "此接口仅允许本机对话服务访问")
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    with connect() as conn:
        try:
            return chat_runtime.execute_tool(conn, run_id, token, payload.name, payload.arguments, dispatch, history_rows)
        except PermissionError as exc:
            raise HTTPException(403, "运行凭据无效") from exc


@app.post("/api/voice/transcribe")
async def transcribe_voice(request: Request):
    audio = bytearray()
    async for chunk in request.stream():
        audio.extend(chunk)
        if len(audio) > 4 * 1024 * 1024:
            raise HTTPException(413, "录音不能超过两分钟")
    if not audio or bytes(audio[:4]) != b"RIFF":
        raise HTTPException(400, "录音格式必须是 WAV")
    try:
        return await asyncio.to_thread(dsh_client.transcribe, bytes(audio))
    except dsh_client.DSHUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    finally:
        audio.clear()



@app.post("/api/chat/conversations/{conversation_id}/attachments")
async def upload_chat_attachment(
    conversation_id: str, request: Request, file: Annotated[UploadFile, File()]
):
    with connect() as conn:
        chat_authorize(conn, conversation_id, request)
        filename = Path(file.filename or "attachment").name
        suffix = Path(filename).suffix.lower()
        if suffix not in {".xlsx", ".xls", ".csv", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}:
            raise ValueError("聊天附件支持 XLS、XLSX、CSV 表格和清单照片")
        content = await file.read(20 * 1024 * 1024 + 1)
        if not content or len(content) > 20 * 1024 * 1024:
            raise ValueError("附件不能为空，且大小不能超过 20 MB")
        digest = hashlib.sha256(content).hexdigest()
        existing = conn.execute(
            "SELECT id,filename,content_type,size,sha256 FROM chat_attachments WHERE conversation_id=? AND sha256=? ORDER BY created_at DESC LIMIT 1",
            (conversation_id, digest),
        ).fetchone()
        if existing:
            return {**dict(existing), "reused": True}
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}:
            if suffix in {".heic", ".heif"}:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            from PIL import Image

            try:
                image = Image.open(io.BytesIO(content))
                if image.width * image.height > 20_000_000:
                    raise ValueError("照片尺寸过大")
                image.verify()
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError("图片文件无法读取") from exc
        attachment_id = uuid.uuid4().hex
        CHAT_UPLOAD_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = CHAT_UPLOAD_DIR / f"{attachment_id}{suffix}"
        destination.write_bytes(content)
        content_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif"}.get(suffix, "text/csv" if suffix == ".csv" else "application/vnd.ms-excel")
        conn.execute(
            "INSERT INTO chat_attachments(id,conversation_id,filename,content_type,size,sha256,path) VALUES (?,?,?,?,?,?,?)",
            (
                attachment_id,
                conversation_id,
                filename[:250],
                content_type,
                len(content),
                digest,
                str(destination),
            ),
        )
        conn.commit()
        return {
            "id": attachment_id,
            "filename": filename[:250],
            "content_type": content_type,
            "size": len(content),
            "sha256": digest,
        }


@app.post("/api/chat/tasks/{task_id}/prepare")
def prepare_chat_task(
    task_id: str, payload: ChatPrepareRequest, request: Request
):
    with connect() as conn:
        task = conn.execute("SELECT * FROM chat_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError("聊天计划不存在")
        chat_authorize(conn, task["conversation_id"], request)
        result = chat_session.prepare_transaction_task(
            conn,
            task["conversation_id"],
            task_id,
            payload.operations,
            actor_name(request),
            dispatch,
            expected_plan_hash=payload.plan_hash,
        )
        conn.commit()
        return result


@app.post("/api/chat/tasks/{task_id}/confirm")
def confirm_chat_task(
    task_id: str, payload: ChatConfirmRequest, request: Request
):
    with connect() as conn:
        task = conn.execute("SELECT * FROM chat_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError("聊天计划不存在")
        chat_authorize(conn, task["conversation_id"], request)
        actor = actor_name(request)

        result = chat_session.confirm_task(
            conn,
            task,
            actor,
            request.headers.get("x-device-id", ""),
            payload.request_id,
            dispatch,
            expected_plan_hash=payload.plan_hash,
            record_message=True,
        )
        return {"status": "completed", "task_id": task_id, "result": result}


@app.post("/api/chat/tasks/{task_id}/cancel")
def cancel_chat_task(task_id: str, payload: ChatCancelRequest, request: Request):
    with connect() as conn:
        task = conn.execute("SELECT * FROM chat_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError("聊天计划不存在")
        chat_authorize(conn, task["conversation_id"], request)
        result = chat_session.cancel_task(
            conn,
            task_id,
            request.headers.get("x-request-id"),
            expected_plan_hash=payload.plan_hash,
        )
        conn.commit()
        return result


@app.get("/sw.js")
def worker_script():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


def mutation(request: Request, conn: sqlite3.Connection, payload: dict, fn):
    return stock.write(
        conn,
        request.headers.get("x-request-id", ""),
        actor_name(request),
        request.headers.get("x-device-id", ""),
        {"path": request.url.path, "payload": payload},
        fn,
    )


def actor_name(request: Request) -> str:
    return unquote(request.headers.get("x-actor", ""))


@app.get("/api/stats")
def stats():
    with connect() as conn:
        result = get_stats(conn)
        result["needs_review_count"] = conn.execute(
            "SELECT COUNT(*) FROM stock_balances WHERE review_reason IS NOT NULL"
        ).fetchone()[0]
        result["unset_threshold_count"] = conn.execute(
            "SELECT COUNT(*) FROM inventory_items i JOIN stock_balances b ON b.item_id=i.id WHERE i.threshold<=0 AND b.active=1"
        ).fetchone()[0]
        return result


@app.get("/api/items/search")
def search(
    q: str = "",
    category: str | None = None,
    lab: str | None = None,
    low_stock: bool = False,
    limit: int = 80,
    starts_at: str | None = None,
    ends_at: str | None = None,
):
    with connect() as conn:
        start = stock.moment(starts_at) if starts_at else None
        end = stock.moment(ends_at) if ends_at else None
        if bool(start) != bool(end) or start and start >= end:
            raise ValueError("请填写有效的开始和结束时间")
        rows = search_items(
            conn, q, category, lab, low_stock, min(max(limit, 1), 2000), start, end
        )
        units = {}
        for row in rows:
            unit = row.get("unit") or ""
            units[unit] = units.get(unit, Decimal(0)) + stock.number(row["available_quantity"])
        return {
            "rows": rows,
            "row_count": len(rows),
            "location_count": len({(row["lab"], row["location_code"]) for row in rows}),
            "quantity_by_unit": {unit: stock.text(value) for unit, value in units.items()},
        }


@app.get("/api/items/{item_id}")
def item(item_id: int, starts_at: str | None = None, ends_at: str | None = None):
    with connect() as conn:
        start = stock.moment(starts_at) if starts_at else None
        end = stock.moment(ends_at) if ends_at else None
        if bool(start) != bool(end) or start and start >= end:
            raise ValueError("请填写有效的开始和结束时间")
        return stock.view(conn, item_id, start, end)


@app.get("/api/alerts")
def alerts():
    with connect() as conn:
        rows = list_alerts(conn)
        for row in rows:
            state = conn.execute(
                "SELECT * FROM alert_states WHERE item_id=?", (row["id"],)
            ).fetchone()
            row["alert_state"] = dict(state) if state else None
        unset = search_items(conn, limit=2000)
        return {
            "rows": rows,
            "row_count": len(rows),
            "unset_rows": [row for row in unset if not row["threshold"]],
        }


@app.get("/api/locations")
def locations():
    with connect() as conn:
        rows = list_locations(conn)
        for row in rows:
            details = conn.execute(
                "SELECT * FROM location_details WHERE lab=? AND location_code=?",
                (row["lab"] or "", row["location_code"] or ""),
            ).fetchone()
            row["details"] = dict(details) if details else None
        return {"rows": rows}


@app.post("/api/locations/preview")
def location_preview(payload: dict):
    with connect() as conn:
        result = preview_location(
            conn, payload.get("lab"), payload.get("location_code")
        )
        result["rows"] = [
            {**row, **stock.view(conn, row["id"])}
            for row in result["rows"]
            if stock.ensure_balance(conn, row["id"])["active"]
        ]
        result["row_count"] = len(result["rows"])
        result["versions"] = {str(row["id"]): row["version"] for row in result["rows"]}
        return result


@app.post("/api/transactions/preview")
def transaction_preview(payload: dict):
    from .genai import AIUnavailable

    message = str(payload.get("text") or "").strip()
    if not message or len(message) > 8000:
        raise ValueError("请输入不超过八千字的库存变更内容")
    try:
        with connect() as conn:
            return parse_transaction_preview(conn, message)
    except AIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def dispatch(conn: sqlite3.Connection, kind: str, data: dict, actor: str) -> dict:
    if kind == "mixed":
        operations = data["operations"]
        for step in operations:
            current = chat_session._fill_versions(conn, step["kind"], step["payload"])
            if current != step["payload"]:
                raise stock.Conflict("计划所引用的记录已经变化，请重新预览")
        results = []
        for step in operations:
            payload = chat_session._fill_versions(conn, step["kind"], step["payload"])
            results.append(dispatch(conn, step["kind"], payload, actor))
        return {"ok": True, "results": results}
    if kind == "preparation":
        from .preparations import create

        return create(conn, data, actor)
    if kind == "preparation_update":
        from .preparations import update

        return update(conn, data, actor)
    if kind == "bulk":
        operations = data.get("operations") or []
        if not operations or len(operations) > 200:
            raise ValueError("一次需要提交一至二百项操作")
        for op in operations:
            stock.check_version(
                stock.ensure_balance(conn, int(op["item_id"])), op.get("version")
            )
        return {
            "ok": True,
            "transactions": [
                stock.operate(conn, op, actor, checked=True) for op in operations
            ],
        }
    if kind == "stock":
        return stock.operate(conn, data, actor)
    if kind == "undo":
        return stock.undo(
            conn, int(data["event_id"]), actor, int(data.get("version", 0))
        )
    if kind == "metadata":
        return flow.update_item(conn, data, actor)
    if kind == "create_item":
        item_id = flow.create_item(conn, data)
        stock.event(conn, item_id, "create", {}, actor)
        return {"item": stock.view(conn, item_id)}
    if kind == "transfer":
        return flow.transfer(conn, data, actor)
    if kind == "archive_location":
        return flow.archive_location(conn, data, actor)
    if kind == "import":
        return flow.commit_import(conn, data, actor)
    if kind == "prepare":
        return flow.consume_preparation(conn, data, actor)
    if kind == "prepare_remap":
        selections = data.get("selections")
        if selections is None:
            selections = {str(data["row_number"]): data["item_id"]}
        return flow.remap_preparation(conn, {**data, "selections": selections})
    if kind == "purchase":
        return flow.purchase(conn, data, actor)
    if kind == "schedule":
        return flow.schedule(conn, data, actor)
    if kind == "execute_schedule":
        return flow.execute_schedule(conn, data, actor)
    if kind == "cancel_schedule":
        row = conn.execute(
            "SELECT * FROM schedules WHERE id=?", (data.get("schedule_id"),)
        ).fetchone()
        if (
            not row
            or row["status"] != "reserved"
            or row["version"] != int(data.get("version", 0))
        ):
            raise stock.Conflict("安排已经变化")
        conn.execute(
            "UPDATE schedules SET status='cancelled',version=version+1 WHERE id=?",
            (row["id"],),
        )
        for line in json.loads(row["lines"]):
            stock.event(
                conn,
                line["item_id"],
                "cancel_reservation",
                {},
                actor,
                stock.required(data, "note"),
                row["id"],
            )
            stock.alerts_changed(conn, line["item_id"])
        return {"ok": True}
    if kind == "template":
        flow.template_lines({**data, "people": 1, "group_size": 1})
        for line in data["lines"]:
            stock.ensure_balance(conn, int(line["item_id"]))
        template_id = data.get("template_id")
        if template_id:
            changed = conn.execute(
                "UPDATE lesson_templates SET name=?,course=?,lines=?,version=version+1 WHERE id=? AND version=?",
                (
                    stock.required(data, "name"),
                    stock.required(data, "course"),
                    json.dumps(data["lines"]),
                    template_id,
                    data.get("version"),
                ),
            ).rowcount
            if not changed:
                raise stock.Conflict("模板已经变化")
        else:
            template_id = conn.execute(
                "INSERT INTO lesson_templates(name,course,lines) VALUES (?,?,?)",
                (
                    stock.required(data, "name"),
                    stock.required(data, "course"),
                    json.dumps(data["lines"]),
                ),
            ).lastrowid
        return {"ok": True, "id": template_id}
    if kind == "location_details":
        lab, location = (
            stock.required(data, "lab"),
            stock.required(data, "location_code"),
        )
        old = conn.execute(
            "SELECT * FROM location_details WHERE lab=? AND location_code=?",
            (lab, location),
        ).fetchone()
        if old and old["version"] != int(data.get("version", 0)):
            raise stock.Conflict("位置说明已经变化")
        conn.execute(
            "INSERT INTO location_details(lab,location_code,description,photo_path) VALUES (?,?,?,?) ON CONFLICT(lab,location_code) DO UPDATE SET description=excluded.description,photo_path=COALESCE(excluded.photo_path,location_details.photo_path),version=location_details.version+1",
            (
                lab,
                location,
                str(data.get("description", old["description"] if old else "") or ""),
                "attachment:" + data["photo_attachment_id"] if data.get("photo_attachment_id") else data.get("photo_path"),
            ),
        )
        for item in conn.execute(
            "SELECT id FROM inventory_items WHERE lab=? AND location_code=?",
            (lab, location),
        ).fetchall():
            stock.event(
                conn,
                item["id"],
                "metadata",
                {},
                actor,
                f"位置资料更新：{data.get('description') or ''}",
            )
        return {"ok": True}
    if kind == "alert":
        item_id = int(data["item_id"])
        state = conn.execute(
            "SELECT event_id FROM alert_states WHERE item_id=?", (item_id,)
        ).fetchone()
        if not state or state["event_id"] != data.get("event_id"):
            raise stock.Conflict("此物品的预警状态已经变化，请刷新")
        if data.get("action") == "snooze":
            until = stock.moment(stock.required(data, "until"))
            conn.execute(
                "UPDATE alert_states SET snooze_until=?,acknowledged=0 WHERE item_id=?",
                (until, item_id),
            )
        elif data.get("action") == "remind":
            conn.execute(
                "UPDATE alert_states SET acknowledged=0,snooze_until=NULL,last_reminded_at=NULL WHERE item_id=?",
                (item_id,),
            )
        else:
            conn.execute(
                "UPDATE alert_states SET acknowledged=1 WHERE item_id=?", (item_id,)
            )
        stock.event(conn, item_id, "alert_" + str(data.get("action")), {}, actor)
        return {"ok": True}
    raise ValueError("未知操作")


def form_preview(conn, kind: str, payload: dict, request: Request, conversation_id: str | None = None) -> dict:
    request_id = request.headers.get("x-request-id", "")
    if not 8 <= len(request_id) <= 100:
        raise ValueError("请使用有效请求编号生成预览")
    digest = hashlib.sha256(chat_session.json_text({"kind": kind, "payload": payload}).encode()).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute("SELECT * FROM chat_messages WHERE request_id=? AND role='user' AND device_id=?", (request_id, request.headers.get("x-device-id", ""))).fetchone()
        if old:
            if old["request_hash"] != digest:
                raise stock.Conflict("此请求编号已用于不同内容")
            saved = conn.execute("SELECT result_json FROM chat_messages WHERE conversation_id=? AND request_id=? AND role='assistant'", (old["conversation_id"], request_id)).fetchone()
            result = json.loads(saved[0])
            task = conn.execute("SELECT * FROM chat_tasks WHERE id=?", (result.get("task_id"),)).fetchone()
            if task and task["status"] != "pending_confirmation":
                result = {**result, "status": task["status"], "result": json.loads(task["result_json"] or "null")}
            conn.commit()
            return result
        conversation_id = conversation_id or request.headers.get("x-conversation-id")
        if conversation_id:
            chat_authorize(conn, conversation_id, request)
        else:
            conn.commit()
            conversation_id = chat_session.new_conversation(conn, request.headers.get("x-device-id", ""))["conversation_id"]
            conn.execute("BEGIN IMMEDIATE")
        actor = actor_name(request)
        chat_runtime.expire_plans(conn, conversation_id, request_id)
        cursor = conn.execute("INSERT INTO chat_messages(conversation_id,request_id,role,content,request_hash,actor,device_id) VALUES (?,?,'user',?,?,?,?)", (conversation_id, request_id, "表单操作：" + kind, digest, actor, request.headers.get("x-device-id", "")))
        attachments = [dict(row) for row in conn.execute("SELECT * FROM chat_attachments WHERE conversation_id=?", (conversation_id,))]
        result = chat_session._business_task(conn, conversation_id, cursor.lastrowid, request_id, {"kind": kind, "payload": payload}, attachments, dispatch, actor, validate_references=False)
        result["conversation_id"] = conversation_id
        chat_session._assistant_message(conn, conversation_id, request_id, result)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


@app.post("/api/actions/{kind}")
def action(kind: str, payload: dict, request: Request):
    with connect() as conn:
        return form_preview(conn, kind, payload, request)


@app.get("/api/operations/{request_id}")
def operation_result(request_id: str, request: Request):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM operation_requests WHERE request_id=? AND device_id=?",
            (request_id, request.headers.get("x-device-id", "")),
        ).fetchone()
        if not row:
            task = conn.execute("SELECT t.*,m.conversation_id FROM chat_messages m JOIN chat_tasks t ON t.message_id=m.id WHERE m.request_id=? AND m.role='user' AND m.device_id=?", (request_id, request.headers.get("x-device-id", ""))).fetchone()
            if task:
                return {"status": task["status"], "task_id": task["id"], "conversation_id": task["conversation_id"], "plan_hash": task["plan_hash"], "preview": json.loads(task["preview_json"]), "result": json.loads(task["result_json"] or "null")}
        return (
            {"status": "completed", "result": json.loads(row["result"])}
            if row
            else {"status": "unknown"}
        )


@app.post("/api/transactions/commit_bulk")
def commit_bulk(payload: dict, request: Request):
    return action("bulk", payload, request)


@app.post("/api/transactions/commit")
def commit(payload: dict, request: Request):
    return action("stock", payload, request)


@app.post("/api/transactions/{event_id}/undo")
def undo(event_id: int, payload: dict, request: Request):
    return action("undo", {**payload, "event_id": event_id}, request)


@app.post("/api/items/{item_id}/threshold")
def threshold(item_id: int, payload: dict, request: Request):
    return action(
        "stock",
        {
            **{key: value for key, value in payload.items() if key != "threshold"},
            "item_id": item_id,
            "action": "threshold",
            "quantity": payload.get("threshold"),
        },
        request,
    )


@app.post("/api/prepare/consume")
def consume(payload: dict, request: Request):
    return action("prepare", payload, request)


@app.post("/api/import/seed")
def seed():
    raise ValueError("已有库存请使用上传预览及确认，不能直接重新导入原始附件")


@app.post("/api/locations/delete")
def archive(payload: dict, request: Request):
    if not payload.get("confirmed"):
        raise ValueError("请确认停用位置")
    return action(
        "archive_location",
        {
            "lab": payload.get("source_lab"),
            "location_code": payload.get("source_location"),
            "note": payload.get("note") or "停用位置",
        },
        request,
    )


@app.post("/api/locations/move")
def move(payload: dict, request: Request):
    with connect() as conn:
        rows = preview_location(conn, payload.get("source_lab"), payload.get("source_location"))["rows"]
        operations = []
        for row in rows:
            item = stock.view(conn, row["id"])
            if not item["active"]:
                continue
            if stock.number(item["borrowed_quantity"]) or item["review_reason"]:
                raise ValueError("位置包含未归还或待核对物品，请先处理")
            for state in stock.STATES:
                if stock.number(item[state]):
                    operations.append({"kind": "transfer", "payload": {"item_id": item["id"], "state": state, "quantity": item[state], "target_lab": payload.get("target_lab"), "target_location": payload.get("target_location"), "note": payload.get("note")}})
        operations.append({"kind": "archive_location", "payload": {"lab": payload.get("source_lab"), "location_code": payload.get("source_location"), "note": payload.get("note") or "位置转移后停用"}})
        return form_preview(conn, "mixed", {"operations": operations}, request)


async def uploaded(file: UploadFile) -> Path:
    if Path(file.filename or "").suffix.lower() not in (".xls", ".xlsx", ".csv"):
        raise ValueError("请使用 XLS、XLSX 或 CSV 文件")
    contents = await file.read(20 * 1024 * 1024 + 1)
    if len(contents) > 20 * 1024 * 1024:
        raise ValueError("文件不能超过二十兆字节")
    return save_upload_to_temp(contents, Path(file.filename).suffix.lower())


@app.post("/api/import/upload")
@app.post("/api/import/preview")
async def upload_inventory(
    file: Annotated[UploadFile, File()],
    mode: str = Form("add"),
    category: str = Form(""),
):
    path = await uploaded(file)
    try:
        with connect() as conn:
            return flow.preview_import(conn, path, file.filename, mode, category)
    finally:
        path.unlink(missing_ok=True)


@app.post("/api/prepare/preview")
async def prepare(file: Annotated[UploadFile, File()], course: str = Form("")):
    path = await uploaded(file)
    try:
        with connect() as conn:
            return flow.preparation(conn, path, course)
    finally:
        path.unlink(missing_ok=True)


@app.get("/api/workflows")
def workflows():
    with connect() as conn:
        result = {
            name: [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1000"
                )
            ]
            for name, table in [
                ("loans", "loans"),
                ("templates", "lesson_templates"),
                ("schedules", "schedules"),
                ("purchases", "purchase_orders"),
                ("counts", "stock_counts"),
            ]
        }
        items = {
            row["item_id"]: stock.view(conn, row["item_id"])
            for row in conn.execute("SELECT item_id FROM stock_balances WHERE active=1")
        }
        for row in result["loans"] + result["purchases"] + result["counts"]:
            row["item"] = items.get(row["item_id"]) or stock.view(conn, row["item_id"])
            if "due_at" in row:
                row["overdue"] = (
                    stock.number(row["outstanding"]) > 0
                    and row["due_at"] < datetime.now(UTC).isoformat()
                )
        for row in result["templates"] + result["schedules"]:
            row["lines"] = json.loads(row["lines"])
            row["item_versions"] = {
                str(line["item_id"]): items.get(line["item_id"], {}).get("version")
                for line in row["lines"]
            }
        return result


def history_rows(conn: sqlite3.Connection, query: dict) -> list[dict]:
    result = []
    for row in conn.execute("SELECT * FROM stock_events ORDER BY id DESC"):
        entry = dict(row)
        snapshot = json.loads(entry.pop("snapshot"))
        changes = json.loads(entry["changes"])
        entry.update(snapshot)
        entry["delta_quantity"] = stock.text(changes.get("ready", 0))
        entry["action_label"] = ACTION_LABELS.get(entry["action"], entry["action"])
        entry["can_undo"] = not entry["reversed_by"] and entry["action"] in (
            "inbound",
            "consume",
            "borrow",
            "threshold",
            "adjust",
            "count",
            "state",
        )
        entry["version"] = stock.ensure_balance(conn, entry["item_id"])["version"]
        result.append(entry)
    for row in conn.execute(
        "SELECT t.*,s.snapshot FROM inventory_transactions t LEFT JOIN legacy_snapshots s ON s.transaction_id=t.id ORDER BY t.id DESC"
    ):
        entry = dict(row)
        snapshot = json.loads(entry.pop("snapshot") or "{}")
        result.append(
            {
                **entry,
                **snapshot,
                "id": f"legacy-{row['id']}",
                "action_label": ACTION_LABELS.get(row["action"], row["action"]),
                "can_undo": False,
                "legacy": True,
            }
        )

    def include(row):
        return (
            (not query.get("actor") or query["actor"] in str(row.get("actor") or ""))
            and (
                not query.get("q")
                or query["q"]
                in " ".join(
                    str(row.get(key) or "")
                    for key in ("item_name", "note", "lab", "location_code")
                )
            )
            and (not query.get("action") or query["action"] == row["action"])
            and (
                not query.get("from_date")
                or row["created_at"][:10] >= query["from_date"]
            )
            and (not query.get("to_date") or row["created_at"][:10] <= query["to_date"])
            and (not query.get("item_id") or str(row["item_id"]) == query["item_id"])
        )

    from zoneinfo import ZoneInfo

    def local_date(row):
        return (
            datetime.fromisoformat(row["created_at"])
            .replace(tzinfo=UTC)
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .date()
            .isoformat()
        )

    for row in result:
        row["local_date"] = local_date(row)
    result.sort(key=lambda row: row["created_at"], reverse=True)
    return [row for row in result if include({**row, "created_at": row["local_date"]})][
        : min(int(query.get("limit") or 200), 10000)
    ]


@app.get("/api/transactions")
def history(request: Request):
    with connect() as conn:
        return {"rows": history_rows(conn, dict(request.query_params))}


@app.get("/api/export/history")
def export_history(request: Request):
    with connect() as conn:
        rows = history_rows(conn, {**dict(request.query_params), "limit": 10000})
    workbook = Workbook()
    sheet = workbook.active
    fields = [
        "created_at",
        "actor",
        "action_label",
        "item_name",
        "spec",
        "lab",
        "location_code",
        "delta_quantity",
        "quantity_after",
        "note",
    ]
    sheet.append(fields)
    for row in rows:
        sheet.append(
            [
                str(row.get(key) or "").replace("=", "\uff1d", 1)
                if isinstance(row.get(key), str) and row[key].startswith("=")
                else row.get(key)
                for key in fields
            ]
        )
    output = io.BytesIO()
    workbook.save(output)
    return excel(output.getvalue(), "history.xlsx")


def excel(content: bytes, name: str):
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/export/inventory")
def export_inventory(q: str = "", category: str = "", lab: str = ""):
    with connect() as conn:
        return excel(
            export_inventory_workbook(conn, q=q, category=category or None, lab=lab or None),
            "inventory.xlsx",
        )


@app.post("/api/prepare/export")
def export_prepare(payload: dict):
    return excel(export_prepare_workbook(payload.get("rows") or []), "prepare.xlsx")


@app.get("/api/events")
async def events(request: Request):
    async def stream():
        while not await request.is_disconnected():
            with connect() as conn:
                current = conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM stock_events"
                ).fetchone()[0]
                alert = conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM notification_events"
                ).fetchone()[0]
                chat_messages = conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM chat_messages"
                ).fetchone()[0]
                chat_tasks = conn.execute(
                    "SELECT COALESCE(MAX(updated_at),'') FROM chat_tasks"
                ).fetchone()[0]
            marker = f"{current}:{alert}:{chat_messages}:{chat_tasks}"
            if marker != getattr(stream, "previous", None):
                stream.previous = marker
                yield f"id: {current}\ndata: {json.dumps({'revision': marker})}\n\n"
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


@app.get("/api/ai/status")
def ai_status():
    from .genai import status

    return status()


@app.post("/api/ai/check")
def ai_check():
    from .genai import check_health

    return check_health()


@app.get("/api/notifications/config")
def notification_config():
    configured = all(
        os.environ.get(key)
        for key in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "VAPID_SUBJECT")
    ) and os.environ.get("PUBLIC_BASE_URL", "").startswith("https://")
    return {
        "public_key": os.environ.get("VAPID_PUBLIC_KEY", ""),
        "configured": bool(configured),
        "public_url": os.environ.get("PUBLIC_BASE_URL", ""),
    }


@app.post("/api/notifications/subscribe")
def subscribe(payload: dict, request: Request):
    from .notifications import validate_subscription

    validate_subscription(payload)

    def execute():
        conn.execute(
            "INSERT INTO push_subscriptions(endpoint,subscription,actor,device_id) VALUES (?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET subscription=excluded.subscription,actor=excluded.actor,device_id=excluded.device_id,active=1",
            (
                payload["endpoint"],
                json.dumps(payload),
                actor_name(request),
                request.headers.get("x-device-id", ""),
            ),
        )
        subscription_id = conn.execute(
            "SELECT id FROM push_subscriptions WHERE endpoint=?", (payload["endpoint"],)
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO push_deliveries(event_id,subscription_id) SELECT event_id,? FROM alert_states WHERE is_low=1 AND acknowledged=0 AND event_id IS NOT NULL",
            (subscription_id,),
        )
        return {"ok": True}

    with connect() as conn:
        return mutation(request, conn, payload, execute)


@app.post("/api/locations/photo")
async def location_photo(
    request: Request,
    file: Annotated[UploadFile, File()],
    lab: str = Form(...),
    location_code: str = Form(...),
    version: int = Form(0),
):
    with connect() as conn:
        conversation_id = request.headers.get("x-conversation-id")
        if not conversation_id:
            conversation_id = chat_session.new_conversation(conn, request.headers.get("x-device-id", ""))["conversation_id"]
    attachment = await upload_chat_attachment(conversation_id, request, file)
    with connect() as conn:
        return form_preview(conn, "location_details", {"lab": lab, "location_code": location_code, "photo_attachment_id": attachment["id"]}, request, conversation_id)


@app.get("/api/media/{name}")
def media(name: str):
    if __import__("re").fullmatch(r"attachment:[0-9a-f]{32}", name):
        from .attachments import image_payload

        with connect() as conn:
            row = conn.execute("SELECT path,filename FROM chat_attachments WHERE id=?", (name[11:],)).fetchone()
            if not row:
                raise HTTPException(404, "照片不存在")
            payload = image_payload(Path(row["path"]), row["filename"])
            return Response(base64.b64decode(payload["data"]), media_type="image/jpeg")
    if not __import__("re").fullmatch(r"(?:[0-9a-f]{32}|chat-[0-9a-f]{32})\.jpg", name):
        raise HTTPException(status_code=404)
    return FileResponse(PHOTO_DIR / name, media_type="image/jpeg")


@app.get("/api/downloads/{file_id}")
def download(file_id: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM exported_files WHERE id=?", (file_id,)).fetchone()
        if not row:
            raise HTTPException(404, "导出文件不存在")
        path = Path(row["path"])
        if not path.is_file():
            raise HTTPException(404, "导出文件不存在")
        content = path.read_bytes()
        if len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise HTTPException(409, "导出文件校验失败，请重新导出")
        from urllib.parse import quote

        return Response(content, media_type="application/octet-stream", headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["filename"]), "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
