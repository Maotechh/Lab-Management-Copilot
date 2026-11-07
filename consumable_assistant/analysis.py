"""Persist analysis jobs; send only selected input bytes to the isolated broker."""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import uuid
import zipfile
from pathlib import Path

import httpx

from . import exports
from .analysis_sandbox import unpack


def execute(code: str, attachments: list[dict]) -> tuple[dict, list[tuple[str, bytes]]]:
    if not attachments or len(attachments) > 8 or not code or len(code) > 50000:
        raise ValueError("请选择一至八个附件，并提供有效 Python 程序")
    request = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(request, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('program.py', code)
        for index, attachment in enumerate(attachments):
            content = Path(attachment['path']).read_bytes()
            if len(content) > 20 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != attachment['sha256']:
                raise ValueError("选定附件未通过完整性校验")
            name = str(index) + Path(attachment['filename']).suffix.lower()
            archive.writestr('inputs/' + name, content)
            manifest.append({'path': '/inputs/' + name, 'filename': attachment['filename'], 'sha256': attachment['sha256']})
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
    transport = httpx.HTTPTransport(uds=os.environ.get('LAB_ANALYSIS_SOCKET', '/run/lab-analysis/broker.sock'))
    try:
        with httpx.Client(transport=transport, trust_env=False, timeout=80) as client:
            response = client.post('http://analysis/execute', content=request.getvalue(), headers={'Content-Type': 'application/zip'})
            if response.status_code != 200:
                raise ValueError("分析服务未完成请求，请检查服务状态或稍后继续")
            if len(response.content) > 50 * 1024 * 1024 + 256 * 1024:
                raise ValueError("分析输出超过 50 MB")
            return unpack(response.content)
    except httpx.HTTPError as exc:
        raise ValueError("分析服务暂时不可用，请稍后继续") from exc


def run(conn, conversation_id: str, code: str, attachments: list[dict], *, run_id: str, arguments_hash: str) -> dict:
    job_id = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    old = conn.execute("SELECT * FROM analysis_jobs WHERE run_id=? AND arguments_hash=?", (run_id, arguments_hash)).fetchone()
    if old:
        conn.commit()
        return json.loads(old["result_json"]) if old["result_json"] else {"type": "analysis", "job_id": old["id"], "status": old["status"], "reply": "该分析已登记，请查看原运行结果。"}
    conn.execute("INSERT INTO analysis_jobs(id,conversation_id,status,run_id,arguments_hash) VALUES (?,?,'running',?,?)", (job_id, conversation_id, run_id, arguments_hash))
    conn.commit()
    try:
        metadata, files = execute(code, attachments)
        result = {"type": "analysis", "job_id": job_id, **metadata, "files": []}
        for name, content in files:
            result["files"].append(exports.save(conn, conversation_id, content, name, mimetypes.guess_type(name)[0] or "application/octet-stream", job_id=job_id))
    except (ValueError, OSError) as exc:
        result = {"type": "analysis", "job_id": job_id, "status": "failed", "error": str(exc)}
    conn.execute("UPDATE analysis_jobs SET status=?,result_json=? WHERE id=?", (result["status"], json.dumps(result, ensure_ascii=False), job_id))
    conn.commit()
    return result
