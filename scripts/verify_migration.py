from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from backup_database import backup, verify_restore
from migrate_database import migrate
from restore_database import restore


def verify(source: Path, directory: Path) -> dict:
    snapshot = backup(source, directory / "snapshots")
    candidate = directory / "migration-test.db"
    with sqlite3.connect(snapshot) as old, sqlite3.connect(candidate) as new:
        old.backup(new)
    report = migrate(candidate)
    for version in (1, 2, 3):
        before = candidate.with_suffix(f".before-v{version}.db")
        if before.exists():
            verify_restore(before)
    restored = directory / "restored-test.db"
    restore(snapshot, restored)
    with sqlite3.connect(snapshot) as old, sqlite3.connect(restored) as new:
        if list(old.iterdump()) != list(new.iterdump()):
            raise RuntimeError("Restored data differs from the release snapshot")
    report["restore"] = "verified, all SQL records identical"
    (directory / "migration-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / "migration-report.json").chmod(0o600)
    return {key: value for key, value in report.items() if key != "item_differences"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    print(json.dumps(verify(args.source, args.directory), ensure_ascii=False))
