from __future__ import annotations

from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .database import connect, init_db, inventory_is_empty
from .importer import import_all_sources
from .services import (
    apply_transaction,
    export_inventory_workbook,
    export_prepare_workbook,
    get_stats,
    list_alerts,
    list_transactions,
    parse_prepare_file,
    parse_transaction_preview,
    save_upload_to_temp,
    search_items,
    undo_transaction,
    update_threshold,
)
from .settings import AUTO_SEED, STATIC_DIR


app = FastAPI(title="耗材智能管理助手", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class PreviewRequest(BaseModel):
    text: str


class TransactionCommitRequest(BaseModel):
    item_id: int
    action: str
    quantity: float = 0
    unit: str | None = None
    note: str | None = None
    actor: str | None = None


class BulkTransactionCommitRequest(BaseModel):
    operations: list[TransactionCommitRequest]


class ThresholdRequest(BaseModel):
    threshold: float


class PrepareExportRequest(BaseModel):
    rows: list[dict[str, Any]]


@app.on_event("startup")
def startup() -> None:
    with connect() as conn:
        init_db(conn)
        if AUTO_SEED and inventory_is_empty(conn):
            import_all_sources(conn)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/stats")
def api_stats() -> dict[str, Any]:
    with connect() as conn:
        return get_stats(conn)


@app.post("/api/import/seed")
def api_import_seed() -> dict[str, Any]:
    with connect() as conn:
        init_db(conn)
        summary = import_all_sources(conn)
        return {"ok": True, **summary, "stats": get_stats(conn)}


@app.get("/api/items/search")
def api_search_items(
    q: str = "",
    category: str | None = None,
    lab: str | None = None,
    low_stock: bool = False,
    limit: int = 80,
) -> dict[str, Any]:
    with connect() as conn:
        rows = search_items(
            conn, q=q, category=category, lab=lab, low_stock=low_stock, limit=limit
        )
        return {"rows": rows, "row_count": len(rows)}


@app.get("/api/alerts")
def api_alerts() -> dict[str, Any]:
    with connect() as conn:
        rows = list_alerts(conn)
        return {"rows": rows, "row_count": len(rows)}


@app.post("/api/items/{item_id}/threshold")
def api_update_threshold(item_id: int, payload: ThresholdRequest) -> dict[str, Any]:
    try:
        with connect() as conn:
            return {
                "ok": True,
                "transaction": update_threshold(conn, item_id, payload.threshold),
            }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/transactions/preview")
def api_transaction_preview(payload: PreviewRequest) -> dict[str, Any]:
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="请输入库存变更内容")
    with connect() as conn:
        return parse_transaction_preview(conn, payload.text)


@app.post("/api/transactions/commit")
def api_transaction_commit(payload: TransactionCommitRequest) -> dict[str, Any]:
    try:
        with connect() as conn:
            transaction = apply_transaction(
                conn,
                item_id=payload.item_id,
                action=payload.action,
                quantity=payload.quantity,
                note=payload.note,
                actor=payload.actor,
                unit=payload.unit,
            )
            return {"ok": True, "transaction": transaction}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/transactions/commit_bulk")
def api_transaction_commit_bulk(
    payload: BulkTransactionCommitRequest,
) -> dict[str, Any]:
    try:
        with connect() as conn:
            transactions = []
            for op in payload.operations:
                txn = apply_transaction(
                    conn,
                    item_id=op.item_id,
                    action=op.action,
                    quantity=op.quantity,
                    note=op.note,
                    actor=op.actor,
                    unit=op.unit,
                )
                transactions.append(txn)
            return {"ok": True, "transactions": transactions}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/transactions")
def api_transactions(limit: int = 80) -> dict[str, Any]:
    with connect() as conn:
        rows = list_transactions(conn, limit=limit)
        return {"rows": rows, "row_count": len(rows)}


@app.post("/api/transactions/{txn_id}/undo")
def api_undo_transaction(txn_id: int) -> dict[str, Any]:
    try:
        with connect() as conn:
            transaction = undo_transaction(conn, txn_id)
            return {"ok": True, "transaction": transaction}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/prepare/preview")
async def api_prepare_preview(file: UploadFile = File(...)) -> dict[str, Any]:
    contents = await file.read()
    suffix = "." + (file.filename or "list.xlsx").split(".")[-1]
    temp_path = save_upload_to_temp(contents, suffix)
    try:
        with connect() as conn:
            return parse_prepare_file(conn, temp_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        temp_path.unlink(missing_ok=True)


@app.post("/api/prepare/export")
def api_prepare_export(payload: PrepareExportRequest) -> Response:
    content = export_prepare_workbook(payload.rows)
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="prepare-list.xlsx"'},
    )


@app.get("/api/export/inventory")
def api_export_inventory() -> Response:
    with connect() as conn:
        content = export_inventory_workbook(conn)
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="inventory.xlsx"'},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
