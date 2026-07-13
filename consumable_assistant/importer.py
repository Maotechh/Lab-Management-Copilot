from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .normalization import clean_cell, extract_aliases, normalize_text, parse_float, stable_key
from .settings import ATTACHMENT_DIR


@dataclass(frozen=True)
class SourceConfig:
    filename: str
    category: str
    lab: str | None
    name_col: str
    location_col: str
    quantity_col: str
    unit_col: str
    threshold_col: str
    spec_col: str | None = None
    brand_col: str | None = None
    remark_col: str | None = None
    lab_col: str | None = None


SOURCE_CONFIGS = [
    SourceConfig(
        filename="普通化学实验室耗材（不单列规格、带库存预警）.xlsx",
        category="普通化学",
        lab=None,
        lab_col="实验室",
        name_col="耗材名称",
        location_col="耗材位置",
        quantity_col="耗材数量",
        unit_col="数量单位",
        threshold_col="库存预警",
        remark_col="备注",
    ),
    SourceConfig(
        filename="307+310有机化学部分.xls",
        category="有机化学",
        lab="307/310",
        name_col="产品名",
        location_col="存放地",
        quantity_col="数量",
        unit_col="数量单位",
        threshold_col="数量预警",
        spec_col="规格型号",
        remark_col="备注",
    ),
    SourceConfig(
        filename="314物理化学.xls",
        category="物理化学",
        lab="314",
        name_col="产品名",
        location_col="存放地",
        quantity_col="数量",
        unit_col="数量单位",
        threshold_col="数量预警",
        spec_col="规格型号",
        brand_col="品牌",
        remark_col="备注",
    ),
]


def read_inventory_sheet(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".xls":
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["XDG_CACHE_HOME"] = os.path.join(tmp, "cache")
            os.makedirs(env["XDG_CACHE_HOME"], exist_ok=True)
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "xlsx", "--outdir", tmp, str(path)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            converted = list(Path(tmp).glob("*.xlsx"))
            if not converted:
                raise RuntimeError(f"无法读取 {path.name}，没有得到转换后的 xlsx 文件")
            return pd.read_excel(converted[0], dtype=object)
    return pd.read_excel(path, dtype=object)


def import_all_sources(
    conn: sqlite3.Connection, attachment_dir: Path = ATTACHMENT_DIR
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    for config in SOURCE_CONFIGS:
        path = attachment_dir / config.filename
        if not path.exists():
            summaries.append({"source_file": config.filename, "status": "missing"})
            continue
        summaries.append(import_source(conn, path, config))
    return {
        "sources": summaries,
        "total_imported_rows": sum(item.get("imported_rows", 0) for item in summaries),
        "total_skipped_rows": sum(item.get("skipped_rows", 0) for item in summaries),
        "total_created_items": sum(item.get("created_items", 0) for item in summaries),
    }


def import_source(conn: sqlite3.Connection, path: Path, config: SourceConfig) -> dict[str, Any]:
    job_id = conn.execute(
        "INSERT INTO import_jobs(source_file) VALUES (?)", (path.name,)
    ).lastrowid
    imported_rows = 0
    skipped_rows = 0
    created_items = 0
    try:
        df = read_inventory_sheet(path)
        df.columns = [str(col).strip() for col in df.columns]
        df = df.dropna(how="all")
        for index, row in df.iterrows():
            mapped = map_source_row(row.to_dict(), config, path.name, int(index) + 2)
            if mapped is None:
                skipped_rows += 1
                continue
            if source_row_exists(conn, mapped["row_hash"]):
                skipped_rows += 1
                continue
            conn.execute(
                """
                INSERT INTO source_rows(
                    row_hash, source_file, source_sheet, source_row_number, category,
                    lab, location_code, item_name, normalized_name, spec, brand,
                    quantity, unit, threshold, remark, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mapped["row_hash"],
                    mapped["source_file"],
                    mapped["source_sheet"],
                    mapped["source_row_number"],
                    mapped["category"],
                    mapped["lab"],
                    mapped["location_code"],
                    mapped["item_name"],
                    mapped["normalized_name"],
                    mapped["spec"],
                    mapped["brand"],
                    mapped["quantity"],
                    mapped["unit"],
                    mapped["threshold"],
                    mapped["remark"],
                    mapped["raw_json"],
                ),
            )
            item_id, was_created = upsert_inventory_item(conn, mapped)
            if was_created:
                created_items += 1
            insert_import_transaction(conn, item_id, mapped)
            imported_rows += 1
        conn.execute(
            """
            UPDATE import_jobs
            SET imported_rows = ?, skipped_rows = ?, created_items = ?,
                status = 'finished', finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (imported_rows, skipped_rows, created_items, job_id),
        )
        conn.commit()
        return {
            "source_file": path.name,
            "status": "finished",
            "imported_rows": imported_rows,
            "skipped_rows": skipped_rows,
            "created_items": created_items,
        }
    except Exception as exc:
        conn.rollback()
        conn.execute(
            """
            UPDATE import_jobs
            SET status = 'failed', message = ?, finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(exc), job_id),
        )
        conn.commit()
        raise


def map_source_row(
    row: dict[str, Any], config: SourceConfig, source_file: str, row_number: int
) -> dict[str, Any] | None:
    item_name = clean_cell(row.get(config.name_col))
    if not item_name:
        return None
    location = clean_cell(row.get(config.location_col))
    lab = clean_cell(row.get(config.lab_col)) if config.lab_col else config.lab
    quantity = parse_float(row.get(config.quantity_col)) or 0.0
    threshold = parse_float(row.get(config.threshold_col))
    mapped = {
        "source_file": source_file,
        "source_sheet": "Sheet1",
        "source_row_number": row_number,
        "category": config.category,
        "lab": lab,
        "location_code": location,
        "item_name": item_name,
        "normalized_name": normalize_text(item_name),
        "spec": clean_cell(row.get(config.spec_col)) if config.spec_col else None,
        "brand": clean_cell(row.get(config.brand_col)) if config.brand_col else None,
        "quantity": float(quantity),
        "unit": clean_cell(row.get(config.unit_col)),
        "threshold": float(threshold) if threshold is not None else 0.0,
        "remark": clean_cell(row.get(config.remark_col)) if config.remark_col else None,
        "raw": {str(key): clean_cell(value) for key, value in row.items() if clean_cell(value) is not None},
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
    raw_for_hash = {
        key: mapped[key]
        for key in [
            "source_file",
            "category",
            "lab",
            "location_code",
            "item_name",
            "spec",
            "brand",
            "quantity",
            "unit",
            "threshold",
            "remark",
        ]
    }
    mapped["row_hash"] = hashlib.sha256(
        json.dumps(raw_for_hash, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    mapped["raw_json"] = json.dumps(mapped["raw"], ensure_ascii=False, sort_keys=True)
    return mapped


def source_row_exists(conn: sqlite3.Connection, row_hash: str) -> bool:
    return conn.execute("SELECT 1 FROM source_rows WHERE row_hash = ?", (row_hash,)).fetchone() is not None


def upsert_inventory_item(conn: sqlite3.Connection, mapped: dict[str, Any]) -> tuple[int, bool]:
    existing = conn.execute(
        "SELECT id, quantity, threshold, remark FROM inventory_items WHERE item_key = ?",
        (mapped["item_key"],),
    ).fetchone()
    if existing:
        item_id = int(existing["id"])
        threshold = max(float(existing["threshold"] or 0), float(mapped["threshold"] or 0))
        remark = existing["remark"] or mapped["remark"]
        conn.execute(
            """
            UPDATE inventory_items
            SET quantity = quantity + ?, threshold = ?, remark = ?,
                source_count = source_count + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (mapped["quantity"], threshold, remark, item_id),
        )
        add_aliases(conn, item_id, mapped["item_name"])
        return item_id, False
    item_id = conn.execute(
        """
        INSERT INTO inventory_items(
            item_key, category, lab, location_code, item_name, normalized_name,
            spec, brand, unit, quantity, threshold, remark, source_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            mapped["item_key"],
            mapped["category"],
            mapped["lab"],
            mapped["location_code"],
            mapped["item_name"],
            mapped["normalized_name"],
            mapped["spec"],
            mapped["brand"],
            mapped["unit"],
            mapped["quantity"],
            mapped["threshold"] or 0.0,
            mapped["remark"],
        ),
    ).lastrowid
    add_aliases(conn, int(item_id), mapped["item_name"])
    return int(item_id), True


def add_aliases(conn: sqlite3.Connection, item_id: int, name: str) -> None:
    for alias in extract_aliases(name):
        conn.execute(
            """
            INSERT OR IGNORE INTO item_aliases(item_id, alias, normalized_alias)
            VALUES (?, ?, ?)
            """,
            (item_id, alias, normalize_text(alias)),
        )


def insert_import_transaction(
    conn: sqlite3.Connection, item_id: int, mapped: dict[str, Any]
) -> None:
    item = conn.execute("SELECT quantity, unit FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
    quantity_after = float(item["quantity"])
    quantity_before = quantity_after - float(mapped["quantity"])
    conn.execute(
        """
        INSERT INTO inventory_transactions(
            item_id, action, delta_quantity, quantity_before, quantity_after,
            unit, note, actor, source_type, source_ref
        )
        VALUES (?, 'import', ?, ?, ?, ?, ?, 'system', 'import', ?)
        """,
        (
            item_id,
            mapped["quantity"],
            quantity_before,
            quantity_after,
            mapped["unit"],
            f"初始导入：{mapped['source_file']} 第 {mapped['source_row_number']} 行",
            f"{mapped['source_file']}:{mapped['source_row_number']}",
        ),
    )
