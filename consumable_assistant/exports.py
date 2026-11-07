"""Persisted, content-addressed download records for chat and analysis results."""
from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook

from .settings import DB_PATH


def history_workbook(rows: list[dict], filters: dict) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "业务历史"
    fields = ["created_at", "actor", "action_label", "item_name", "spec",
              "lab", "location_code", "delta_quantity", "quantity_after", "note",
              "id", "item_id", "unit", "related_id", "reversed_by", "changes"]
    sheet.append(fields)
    for row in rows:
        sheet.append([row.get(key) for key in fields])
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    source = workbook.create_sheet("导出说明")
    source.append(["字段", "内容"])
    for key, value in {"generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                       "row_count": len(rows), **filters}.items():
        source.append([key, value])
        if isinstance(value, str):
            source.cell(source.max_row, 2).data_type = "s"
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def directory() -> Path:
    path = Path(os.environ.get("CONSUMABLE_EXPORT_DIR", DB_PATH.parent / "exports"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def save(conn, conversation_id: str, content: bytes, filename: str, media_type: str,
         *, task_id: str | None = None, job_id: str | None = None) -> dict:
    if len(content) > 50 * 1024 * 1024:
        raise ValueError("导出文件超过 50 MB")
    file_id = uuid.uuid4().hex
    path = directory() / file_id
    path.write_bytes(content)
    path.chmod(0o600)
    digest = hashlib.sha256(content).hexdigest()
    conn.execute("INSERT INTO exported_files(id,conversation_id,task_id,job_id,filename,media_type,size,sha256,path) VALUES (?,?,?,?,?,?,?,?,?)",
                 (file_id, conversation_id, task_id, job_id, Path(filename).name[:200], media_type, len(content), digest, str(path)))
    return {"id": file_id, "filename": Path(filename).name[:200], "size": len(content),
            "sha256": digest, "url": f"/api/downloads/{file_id}"}


def task_report(conn, conversation_id: str, task_id: str) -> dict:
    row = conn.execute("SELECT * FROM chat_tasks WHERE id=? AND conversation_id=?", (task_id, conversation_id)).fetchone()
    if not row:
        raise ValueError("此对话中没有该任务")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "任务与实际结果"
    sheet.append(["字段", "服务器保存内容"])
    values = dict(row)
    for key in ("id", "status", "kind", "initiator", "confirmer", "confirm_device", "expires_at", "plan_hash", "plan_json", "preview_json", "result_json"):
        value = values.get(key) or ""
        if key.endswith("_json") and value:
            value = json.dumps(json.loads(value), ensure_ascii=False, indent=2)
        # An explicit string cell never becomes an Excel formula.
        for offset in range(0, max(1, len(str(value))), 30000):
            sheet.append([key, str(value)[offset:offset + 30000]])
            sheet.cell(sheet.max_row, 2).data_type = "s"
    output = io.BytesIO()
    workbook.save(output)
    item = save(conn, conversation_id, output.getvalue(), f"task-{task_id}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", task_id=task_id)
    return {"type": "export", **item}
