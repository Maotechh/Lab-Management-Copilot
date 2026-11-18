"""Check preview and a read-only DSH inventory query on the local release."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

import httpx


def verify(client: httpx.Client, database: Path) -> dict:
    stamp = uuid.uuid4().hex
    actor = f"ai-check-{stamp}"
    headers = {"X-Actor": actor, "X-Device-ID": actor}
    preview = client.post("/api/transactions/preview", headers=headers, json={"text": "100mL烧杯入库2个"}).raise_for_status().json()
    if (not preview.get("model_name") or preview.get("model_error") or not any(
        op.get("action") == "inbound" and op.get("quantity") == 2 for op in preview.get("operations", [])
    )):
        raise RuntimeError("Institutional inventory preview failed")
    conversation = client.post("/api/chat/conversations", headers=headers).raise_for_status().json()["conversation_id"]
    run = client.post(f"/api/chat/conversations/{conversation}/messages", headers=headers, json={
        "message_id": stamp, "text": f"请调用 inventory_search 查询验证编号 AI-CHECK-{stamp}，只回答是否查到耗材，不要生成计划或修改库存。",
    }).raise_for_status().json()["run_id"]
    deadline = time.monotonic() + 315
    while time.monotonic() < deadline:
        state = client.get(f"/api/chat/runs/{run}").raise_for_status().json()
        if state["status"] not in {"running", "queued"}:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Read-only DSH query timed out")
    result = state.get("result") or {}
    results = result.get("tool_results", [])
    tools = [item["tool"] for item in results]
    matched = any(item["result"].get("type") == "inventory" and
                  stamp in str(item["result"].get("search", {}).get("q", "")).lower() for item in results)
    if result.get("status") != "answered" or not result.get("reply") or set(tools) != {"inventory_search"} or not matched:
        raise RuntimeError("No verified read-only DSH inventory answer")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        for table in ("inventory_transactions", "stock_events", "operation_requests"):
            if conn.execute(f"SELECT 1 FROM {table} WHERE actor=? LIMIT 1", (actor,)).fetchone():
                raise RuntimeError("Unexpected inventory write by deployment probe")
    return {"preview": "passed", "dsh_run": run, "tools": tools, "inventory_writes_by_probe": 0}


if __name__ == "__main__":
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=305) as client:
        print(json.dumps(verify(client, Path(sys.argv[1]))))
