from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


def backup(database: Path, directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(UTC)
    destination = directory / f"hourly-{now:%Y%m%dT%H%M%SZ}.db"
    with (
        sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source,
        sqlite3.connect(destination) as target,
    ):
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed")
    destination.chmod(0o600)
    verify_restore(destination)
    daily = directory / f"daily-{now:%Y%m%d}.db"
    if not daily.exists():
        with sqlite3.connect(destination) as source, sqlite3.connect(daily) as target:
            source.backup(target)
        daily.chmod(0o600)
        photos = database.parent / "photos"
        if photos.is_dir():
            photo_backup = directory / f"photos-{now:%Y%m%d}.tar.gz"
            with tarfile.open(photo_backup, "w:gz") as archive:
                archive.add(photos, arcname="photos")
            photo_backup.chmod(0o600)
    for pattern, days in [
        ("hourly-*.db", 7),
        ("daily-*.db", 30),
        ("photos-*.tar.gz", 30),
    ]:
        for path in directory.glob(pattern):
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) < now - timedelta(
                days=days
            ):
                path.unlink()
    return destination


def verify_restore(snapshot: Path) -> dict:
    with tempfile.TemporaryDirectory(dir=snapshot.parent) as temporary:
        restored = Path(temporary) / "restored.db"
        with (
            sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as source,
            sqlite3.connect(restored) as target,
        ):
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Restore integrity check failed")
            tables = [
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            counts = {}
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                expected = source.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[
                    0
                ]
                if (
                    target.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                    != expected
                ):
                    raise RuntimeError("Restored row count differs")
                counts[table] = expected
            return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(
            os.environ.get(
                "CONSUMABLE_DB_PATH", "/var/lib/lab-management/consumables.db"
            )
        ),
    )
    parser.add_argument(
        "--directory", type=Path, default=Path("/var/backups/lab-management")
    )
    args = parser.parse_args()
    print(
        json.dumps(
            {"backup": str(backup(args.database, args.directory)), "integrity": "ok"}
        )
    )
