from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP, report TEXT);
CREATE TABLE IF NOT EXISTS stock_balances(
 item_id INTEGER PRIMARY KEY REFERENCES inventory_items(id),
 ready TEXT NOT NULL, cleaning TEXT NOT NULL DEFAULT '0', repair TEXT NOT NULL DEFAULT '0',
 quarantine TEXT NOT NULL DEFAULT '0', version INTEGER NOT NULL DEFAULT 1,
 review_reason TEXT, original_quantity TEXT, active INTEGER NOT NULL DEFAULT 1,
 usage_type TEXT NOT NULL DEFAULT 'unclassified', conversions TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS operation_requests(
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
 actor TEXT NOT NULL, device_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS stock_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL REFERENCES inventory_items(id),
 action TEXT NOT NULL, changes TEXT NOT NULL, snapshot TEXT NOT NULL, note TEXT,
 actor TEXT NOT NULL, device_id TEXT, request_id TEXT, related_id INTEGER,
 legacy_transaction_id INTEGER, reversed_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_events_item ON stock_events(item_id,id);
CREATE TABLE IF NOT EXISTS loans(
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL REFERENCES inventory_items(id),
 quantity TEXT NOT NULL, outstanding TEXT NOT NULL, borrower TEXT NOT NULL, purpose TEXT NOT NULL,
 destination TEXT NOT NULL, due_at TEXT NOT NULL, actor TEXT NOT NULL,
 schedule_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS lesson_templates(
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, course TEXT NOT NULL,
 lines TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS schedules(
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, course TEXT NOT NULL,
 starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, venue TEXT NOT NULL, actor TEXT NOT NULL,
 lines TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'reserved', version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS reservations(
 id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id INTEGER NOT NULL REFERENCES schedules(id),
 item_id INTEGER NOT NULL REFERENCES inventory_items(id), quantity TEXT NOT NULL, action TEXT NOT NULL,
 UNIQUE(schedule_id,item_id,action));
CREATE TABLE IF NOT EXISTS purchase_orders(
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL REFERENCES inventory_items(id),
 quantity TEXT NOT NULL, received TEXT NOT NULL DEFAULT '0', actor TEXT NOT NULL,
 expected_at TEXT NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'ordered',
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS stock_counts(
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL REFERENCES inventory_items(id),
 before_quantity TEXT NOT NULL, actual_quantity TEXT NOT NULL, reason TEXT NOT NULL,
 actor TEXT NOT NULL, event_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS location_details(
 lab TEXT NOT NULL, location_code TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 photo_path TEXT, version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(lab,location_code));
CREATE TABLE IF NOT EXISTS upload_previews(
 id TEXT PRIMARY KEY, file_hash TEXT NOT NULL, content_hash TEXT NOT NULL, filename TEXT NOT NULL,
 mode TEXT NOT NULL, rows TEXT NOT NULL, errors TEXT NOT NULL, committed INTEGER NOT NULL DEFAULT 0,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS alert_states(
 item_id INTEGER PRIMARY KEY REFERENCES inventory_items(id), is_low INTEGER NOT NULL,
 event_id INTEGER, acknowledged INTEGER NOT NULL DEFAULT 0, snooze_until TEXT,
 last_reminded_at TEXT);
CREATE TABLE IF NOT EXISTS notification_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, kind TEXT NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS push_subscriptions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, endpoint TEXT NOT NULL UNIQUE, subscription TEXT NOT NULL,
 actor TEXT NOT NULL, device_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS push_deliveries(
 event_id INTEGER NOT NULL REFERENCES notification_events(id), subscription_id INTEGER NOT NULL REFERENCES push_subscriptions(id),
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt TEXT DEFAULT CURRENT_TIMESTAMP, error TEXT, PRIMARY KEY(event_id,subscription_id));
CREATE TABLE IF NOT EXISTS service_health(
 name TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at TEXT DEFAULT CURRENT_TIMESTAMP, detail TEXT);
"""


def migrate(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
    ).fetchone()
    if (
        exists
        and conn.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone()
    ):
        migrate_history(conn)
        return
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    if database:
        backup = Path(database).with_suffix(".before-v1.db")
        if not backup.exists():
            with sqlite3.connect(backup) as destination:
                conn.backup(destination)
            backup.chmod(0o600)
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
        conn.execute(
            "INSERT INTO schema_migrations(version,report) VALUES (1,?)",
            (
                json.dumps(
                    {
                        "items": conn.execute(
                            "SELECT COUNT(*) FROM inventory_items"
                        ).fetchone()[0],
                        "history_preserved": True,
                    }
                ),
            ),
        )
        conn.commit()
        migrate_history(conn)
    except Exception:
        conn.rollback()
        raise


def migrate_history(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
        return
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    if database:
        backup = Path(database).with_suffix(".before-v2.db")
        if not backup.exists():
            with sqlite3.connect(backup) as destination:
                conn.backup(destination)
            backup.chmod(0o600)
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE legacy_snapshots(transaction_id INTEGER PRIMARY KEY, snapshot TEXT NOT NULL);
        CREATE INDEX idx_loans_item ON loans(item_id);
        CREATE INDEX idx_reservations_item ON reservations(item_id);
        CREATE INDEX idx_purchase_item ON purchase_orders(item_id);
        """)
        for row in conn.execute(
            "SELECT t.id,i.item_name,i.spec,i.unit,i.lab,i.location_code,i.category FROM inventory_transactions t JOIN inventory_items i ON i.id=t.item_id"
        ).fetchall():
            snapshot = dict(row)
            transaction_id = snapshot.pop("id")
            conn.execute(
                "INSERT INTO legacy_snapshots VALUES (?,?)",
                (transaction_id, json.dumps(snapshot, ensure_ascii=False)),
            )
        conn.execute(
            "INSERT INTO schema_migrations(version,report) VALUES (2,?)",
            (json.dumps({"legacy_snapshots": True}),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
