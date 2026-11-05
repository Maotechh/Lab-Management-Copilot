from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from backup_database import verify_restore


def restore(snapshot: Path, database: Path) -> None:
    verify_restore(snapshot)
    candidate = database.with_suffix(".restore.tmp")
    with (
        sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as source,
        sqlite3.connect(candidate) as target,
    ):
        source.backup(target)
    candidate.chmod(0o640)
    if database.exists():
        owner = database.stat()
        os.chown(candidate, owner.st_uid, owner.st_gid)
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    os.replace(candidate, database)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--services-stopped", action="store_true", required=True)
    args = parser.parse_args()
    restore(args.snapshot, args.database)
    print("Database restored and verified")
