from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .settings import DB_PATH, ensure_data_dir


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    ensure_data_dir()
    path = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            row_hash TEXT NOT NULL UNIQUE,
            source_file TEXT NOT NULL,
            source_sheet TEXT,
            source_row_number INTEGER,
            category TEXT,
            lab TEXT,
            location_code TEXT,
            item_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            spec TEXT,
            brand TEXT,
            quantity REAL NOT NULL DEFAULT 0,
            unit TEXT,
            threshold REAL,
            remark TEXT,
            raw_json TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS inventory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_key TEXT NOT NULL UNIQUE,
            category TEXT,
            lab TEXT,
            location_code TEXT,
            item_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            spec TEXT,
            brand TEXT,
            unit TEXT,
            quantity REAL NOT NULL DEFAULT 0,
            threshold REAL NOT NULL DEFAULT 0,
            remark TEXT,
            source_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS item_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            UNIQUE(item_id, normalized_alias)
        );

        CREATE TABLE IF NOT EXISTS inventory_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES inventory_items(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            delta_quantity REAL NOT NULL DEFAULT 0,
            quantity_before REAL NOT NULL,
            quantity_after REAL NOT NULL,
            unit TEXT,
            note TEXT,
            actor TEXT,
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_ref TEXT,
            undone_by INTEGER REFERENCES inventory_transactions(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS import_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            imported_rows INTEGER NOT NULL DEFAULT 0,
            skipped_rows INTEGER NOT NULL DEFAULT 0,
            created_items INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'running',
            message TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_inventory_items_name ON inventory_items(normalized_name);
        CREATE INDEX IF NOT EXISTS idx_inventory_items_location ON inventory_items(location_code);
        CREATE INDEX IF NOT EXISTS idx_inventory_items_category ON inventory_items(category);
        CREATE INDEX IF NOT EXISTS idx_transactions_item ON inventory_transactions(item_id, created_at DESC);
        """
    )
    conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(row) or {} for row in rows]


def inventory_is_empty(conn: sqlite3.Connection) -> bool:
    value = conn.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0]
    return int(value) == 0
