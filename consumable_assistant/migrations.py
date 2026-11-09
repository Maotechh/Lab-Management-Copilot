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
        migrate_chat(conn)
        migrate_runtime(conn)
        migrate_lab(conn)
        migrate_shared_runtime(conn)
        migrate_exports(conn)
        migrate_preparation_history(conn)
        migrate_procurement(conn)
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
        migrate_chat(conn)
        migrate_runtime(conn)
        migrate_lab(conn)
        migrate_shared_runtime(conn)
        migrate_exports(conn)
        migrate_preparation_history(conn)
        migrate_procurement(conn)
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


def migrate_chat(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone():
        return
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    if database:
        backup = Path(database).with_suffix(".before-v3.db")
        if not backup.exists():
            with sqlite3.connect(backup) as destination:
                conn.backup(destination)
            backup.chmod(0o600)
    try:
        conn.executescript("""BEGIN IMMEDIATE;
        CREATE TABLE chat_conversations(
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL,
            device_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '新对话',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_chat_conversations_updated ON chat_conversations(updated_at DESC);
        CREATE TABLE chat_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(conversation_id,request_id,role)
        );
        CREATE INDEX idx_chat_messages_conversation ON chat_messages(conversation_id,id);
        CREATE TABLE chat_tasks(
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            message_id INTEGER NOT NULL REFERENCES chat_messages(id),
            status TEXT NOT NULL,
            kind TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            preview_json TEXT NOT NULL,
            request_id TEXT,
            result_json TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_chat_tasks_conversation ON chat_tasks(conversation_id,created_at DESC);
        CREATE TABLE chat_tool_calls(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            message_id INTEGER NOT NULL REFERENCES chat_messages(id),
            name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            provider TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE chat_attachments(
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_chat_attachments_conversation ON chat_attachments(conversation_id,created_at DESC);
        CREATE TABLE ai_provider_checks(
            provider TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            elapsed_ms INTEGER,
            error_type TEXT
        );
        INSERT INTO schema_migrations(version,report)
        VALUES (3,'{"chat":"persistent conversations and confirmation tasks","providers":"health checks"}');
        COMMIT;
        """)
    except Exception:
        conn.rollback()
        raise


def migrate_runtime(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone():
        return
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    if database:
        backup = Path(database).with_suffix(".before-v4.db")
        if not backup.exists():
            with sqlite3.connect(backup) as destination:
                conn.backup(destination)
            backup.chmod(0o600)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_column(conn, "chat_messages", "request_hash", "TEXT")
        _ensure_column(conn, "chat_conversations", "active_message_id", "TEXT")
        _ensure_column(conn, "chat_conversations", "active_until", "TEXT")
        _ensure_column(conn, "source_rows", "duplicate_of", "INTEGER")
        indexes = {
            row["name"]: row
            for row in conn.execute("PRAGMA index_list(source_rows)")
        }
        if any(index["unique"] for index in indexes.values()):
            conn.execute(
                "CREATE TABLE source_rows_v4 ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, row_hash TEXT NOT NULL, "
                "source_file TEXT NOT NULL, source_sheet TEXT, source_row_number INTEGER, "
                "category TEXT, lab TEXT, location_code TEXT, item_name TEXT NOT NULL, "
                "normalized_name TEXT NOT NULL, spec TEXT, brand TEXT, quantity REAL NOT NULL DEFAULT 0, "
                "unit TEXT, threshold REAL, remark TEXT, raw_json TEXT NOT NULL, "
                "duplicate_of INTEGER, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.execute(
                "INSERT INTO source_rows_v4(id,row_hash,source_file,source_sheet,source_row_number,"
                "category,lab,location_code,item_name,normalized_name,spec,brand,quantity,unit,"
                "threshold,remark,raw_json,duplicate_of,imported_at) "
                "SELECT id,row_hash,source_file,source_sheet,source_row_number,category,lab,"
                "location_code,item_name,normalized_name,spec,brand,quantity,unit,threshold,"
                "remark,raw_json,duplicate_of,imported_at FROM source_rows"
            )
            conn.execute("DROP TABLE source_rows")
            conn.execute("ALTER TABLE source_rows_v4 RENAME TO source_rows")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_source_rows_hash ON source_rows(row_hash)")
        active_tasks = conn.execute(
            "SELECT id,conversation_id FROM chat_tasks "
            "WHERE status IN ('awaiting_details','pending_confirmation','executing') "
            "ORDER BY conversation_id,created_at DESC,id DESC"
        ).fetchall()
        seen = set()
        for task in active_tasks:
            if task["conversation_id"] in seen:
                conn.execute(
                    "UPDATE chat_tasks SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (task["id"],),
                )
            else:
                seen.add(task["conversation_id"])
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_one_active_task "
            "ON chat_tasks(conversation_id) "
            "WHERE status IN ('awaiting_details','pending_confirmation','executing')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chat_task_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES chat_tasks(id), "
            "from_status TEXT, to_status TEXT NOT NULL, event_type TEXT NOT NULL, "
            "request_id TEXT, detail_json TEXT NOT NULL DEFAULT '{}', "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_task_events_task ON chat_task_events(task_id,id)"
        )
        conn.execute(
            "INSERT INTO chat_task_events(task_id,from_status,to_status,event_type) "
            "SELECT id,NULL,status,'migration_snapshot' FROM chat_tasks "
            "WHERE NOT EXISTS (SELECT 1 FROM chat_task_events e WHERE e.task_id=chat_tasks.id)"
        )
        conn.execute(
            "INSERT INTO schema_migrations(version,report) VALUES (4,?)",
            (json.dumps({"chat_claims": True, "task_events": True, "source_duplicates": True}),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ensure_column(
    conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def migrate_lab(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=5").fetchone():
        return
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    if database:
        backup = Path(database).with_suffix(".before-v5.db")
        if not backup.exists():
            with sqlite3.connect(backup) as destination:
                conn.backup(destination)
            backup.chmod(0o600)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, column, definition in (
            ("chat_messages", "actor", "TEXT NOT NULL DEFAULT ''"),
            ("chat_messages", "device_id", "TEXT NOT NULL DEFAULT ''"),
            ("chat_tasks", "initiator", "TEXT NOT NULL DEFAULT ''"),
            ("chat_tasks", "confirmer", "TEXT NOT NULL DEFAULT ''"),
            ("chat_tasks", "confirm_device", "TEXT NOT NULL DEFAULT ''"),
        ):
            _ensure_column(conn, table, column, definition)
        conn.execute("CREATE TABLE preparations (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, course TEXT NOT NULL, venue TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, people INTEGER NOT NULL, group_size INTEGER NOT NULL, lines TEXT NOT NULL, schedule_id INTEGER REFERENCES schedules(id), actor TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE preparation_schedules (preparation_id INTEGER NOT NULL REFERENCES preparations(id), schedule_id INTEGER NOT NULL REFERENCES schedules(id), PRIMARY KEY(preparation_id,schedule_id))")
        conn.execute("CREATE TABLE chat_runs (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES chat_conversations(id), message_id INTEGER NOT NULL UNIQUE REFERENCES chat_messages(id), status TEXT NOT NULL, request_json TEXT NOT NULL, actor TEXT NOT NULL, device_id TEXT NOT NULL, error TEXT, started_at TEXT, completed_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE chat_events (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES chat_conversations(id), kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE INDEX idx_chat_events_conversation ON chat_events(conversation_id,id)")
        conn.execute("CREATE TABLE analysis_jobs (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES chat_conversations(id), status TEXT NOT NULL, result_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("UPDATE chat_tasks SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE status IN ('awaiting_details','pending_confirmation','executing')")
        conn.execute("UPDATE chat_conversations SET active_message_id=NULL,active_until=NULL")
        conn.execute("INSERT INTO schema_migrations(version,report) VALUES (5,?)", (json.dumps({"shared_conversations": True, "dsh_runs": True, "preparations": True, "old_plans_require_preview": True}),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_shared_runtime(conn: sqlite3.Connection) -> None:
    """Extend persistent runs without changing inventory or prior history."""
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=6").fetchone():
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, column, definition in (
            ("chat_runs", "callback_hash", "TEXT"),
            ("chat_runs", "tool_calls", "INTEGER NOT NULL DEFAULT 0"),
            ("chat_runs", "result_json", "TEXT"),
            ("chat_runs", "cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
            ("chat_tool_calls", "run_id", "TEXT REFERENCES chat_runs(id)"),
        ):
            _ensure_column(conn, table, column, definition)
        conn.execute("CREATE INDEX idx_chat_runs_queue ON chat_runs(status,message_id)")
        conn.execute("CREATE UNIQUE INDEX idx_chat_runs_active ON chat_runs(conversation_id) WHERE status='running'")
        conn.execute("INSERT INTO schema_migrations(version,report) VALUES (6,?)", (json.dumps({"durable_queue": True, "callback_capabilities": True}),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_exports(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=7").fetchone():
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE exported_files (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES chat_conversations(id), task_id TEXT REFERENCES chat_tasks(id), job_id TEXT REFERENCES analysis_jobs(id), filename TEXT NOT NULL, media_type TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        _ensure_column(conn, "analysis_jobs", "run_id", "TEXT REFERENCES chat_runs(id)")
        _ensure_column(conn, "analysis_jobs", "arguments_hash", "TEXT")
        conn.execute("CREATE UNIQUE INDEX idx_analysis_run_args ON analysis_jobs(run_id,arguments_hash)")
        conn.execute("INSERT INTO schema_migrations(version,report) VALUES (7,?)", (json.dumps({"controlled_exports": True, "analysis_idempotency": True}),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_preparation_history(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=8").fetchone():
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_column(conn, "preparation_schedules", "retired", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE TABLE preparation_purchases (preparation_id INTEGER NOT NULL REFERENCES preparations(id), order_id INTEGER NOT NULL REFERENCES purchase_orders(id), PRIMARY KEY(preparation_id,order_id))")
        conn.execute("CREATE TABLE preparation_revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, preparation_id INTEGER NOT NULL REFERENCES preparations(id), version INTEGER NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        for row in conn.execute("SELECT id,lines FROM preparations").fetchall():
            for line in json.loads(row["lines"]):
                if line.get("purchase_draft_id"):
                    conn.execute("INSERT OR IGNORE INTO preparation_purchases VALUES (?,?)", (row["id"], line["purchase_draft_id"]))
        conn.execute("INSERT INTO schema_migrations(version,report) VALUES (8,?)", (json.dumps({"preparation_revision_history": True}),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_procurement(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=9").fetchone():
        return
    with conn:
        conn.execute("CREATE TABLE procurement_demands (id INTEGER PRIMARY KEY AUTOINCREMENT, preparation_id INTEGER REFERENCES preparations(id), item_name TEXT NOT NULL, spec TEXT NOT NULL, unit TEXT NOT NULL, quantity TEXT NOT NULL, received TEXT NOT NULL DEFAULT '0', actor TEXT NOT NULL, expected_at TEXT, note TEXT, status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE procurement_receipts (id INTEGER PRIMARY KEY AUTOINCREMENT, demand_id INTEGER NOT NULL REFERENCES procurement_demands(id), item_id INTEGER NOT NULL REFERENCES inventory_items(id), quantity TEXT NOT NULL, event_id INTEGER NOT NULL REFERENCES stock_events(id), created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO schema_migrations(version,report) VALUES (9,?)", (json.dumps({"unregistered_material_procurement": True}),))
