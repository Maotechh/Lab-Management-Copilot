"""Consistent release snapshots and restoration, including shared chat files."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def inventory(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f'Snapshot contains a symbolic link: {relative}')
        metadata = path.stat()
        if path.is_file():
            result[relative] = {'sha256': checksum(path), 'size': metadata.st_size}
        elif not path.is_dir():
            raise ValueError(f'Snapshot contains a special file: {relative}')
    return result


def permissions(root: Path) -> dict:
    """Preserve file-specific ownership and modes, including credential files."""
    result = {}
    for path in [root, *sorted(root.rglob('*'))]:
        metadata = path.stat(follow_symlinks=False)
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('Snapshot permissions require ordinary files and directories')
        result[path.relative_to(root).as_posix()] = {
            'uid': metadata.st_uid, 'gid': metadata.st_gid, 'mode': metadata.st_mode & 0o777,
        }
    return result


def database_digest(path: Path) -> dict:
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed')
        result = {}
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            quoted = '"' + table.replace('"', '""') + '"'
            rows = sorted(repr(tuple(row)) for row in conn.execute(f'SELECT * FROM {quoted}'))
            result[table] = {'rows': len(rows), 'sha256': hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
        return result


def create(destination: Path, roots: dict[str, Path], database_relative: str, versions: dict) -> Path:
    """The caller must stop every writer before invoking this function."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise ValueError('Backup destination already exists')
    for name, path in roots.items():
        if not name.isidentifier() or not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise ValueError('Backup roots must be named absolute ordinary directories')
    name, relative = database_relative.split('/', 1)
    if name not in roots or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Database must be within a named backup root')
    # Explicit checkpoint also detects a writer that failed to stop. Never copy
    # a live WAL alongside a database whose pages can change independently.
    source_database = roots[name] / relative
    with sqlite3.connect(source_database, timeout=1) as conn:
        if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
            raise ValueError('Database still has active writers or readers')
    source_counts = database_digest(source_database)
    with tempfile.TemporaryDirectory(prefix='lab-snapshot-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'snapshot'
        payload = staging / 'payload'
        payload.mkdir(parents=True, mode=0o700)
        metadata = {}
        modes = {}
        for name, root in roots.items():
            modes[name] = permissions(root)
            shutil.copytree(root, payload / name, symlinks=True)
            metadata[name] = {'path': str(root), 'uid': root.stat().st_uid, 'gid': root.stat().st_gid, 'mode': root.stat().st_mode & 0o777}
        database = payload / database_relative
        if not database.is_file():
            raise ValueError('Snapshot database is missing')
        counts = database_digest(database)
        name, relative = database_relative.split('/', 1)
        if counts != source_counts or counts != database_digest(roots[name] / relative):
            raise ValueError('Source database changed while copying')
        manifest = {'format': 1, 'created_at': datetime.now(UTC).isoformat(), 'roots': metadata,
                    'database': database_relative, 'database_tables': counts, 'versions': versions,
                    'files': inventory(payload), 'permissions': modes}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        with tarfile.open(staging / 'payload.tar.gz', 'w:gz') as archive:
            archive.add(payload, arcname='payload', recursive=True)
        shutil.rmtree(payload)
        verify(staging)
        staging.rename(destination)
        destination.chmod(0o700)
    return destination


def extract_verified(snapshot: Path, target: Path) -> dict:
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    if manifest.get('format') != 1:
        raise ValueError('Unsupported snapshot format')
    with tarfile.open(snapshot / 'payload.tar.gz', 'r:gz') as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'payload' or not (member.isfile() or member.isdir()):
                raise ValueError('Invalid backup archive member')
        archive.extractall(target, filter='data')
    if inventory(target / 'payload') != manifest['files']:
        raise ValueError('Restored file checksums differ')
    if database_digest(target / 'payload' / manifest['database']) != manifest['database_tables']:
        raise ValueError('Restored database rows differ')
    return manifest


def verify(snapshot: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix='lab-restore-check-', dir=snapshot.parent) as temporary:
        return extract_verified(snapshot, Path(temporary))


def restore(snapshot: Path, destination_root: Path) -> dict:
    """Restore all verified roots beneath a target root; writers must remain stopped."""
    with tempfile.TemporaryDirectory(prefix='lab-restore-', dir=snapshot.parent) as temporary:
        staging = Path(temporary)
        manifest = extract_verified(snapshot, staging)
        targets = {}
        for name, metadata in manifest['roots'].items():
            path = Path(metadata['path'])
            if not name.isidentifier() or not path.is_absolute() or '..' in path.parts or len(path.parts) < 3:
                raise ValueError('Invalid restoration root')
            target = destination_root / path.relative_to('/')
            if target.is_symlink():
                raise ValueError('Restoration root must not be a symbolic link')
            targets[name] = target
        values = list(targets.values())
        if any(a != b and a in b.parents for a in values for b in values):
            raise ValueError('Restoration roots overlap')
        for target in values:
            for suffix in ('.restore-new', '.restore-previous'):
                if target.with_name(target.name + suffix).exists():
                    raise ValueError('Preserve or move previous restoration files before retrying')
        for name, target in targets.items():
            replacement = target.with_name(target.name + '.restore-new')
            if replacement.exists():
                raise ValueError('A previous restoration candidate exists')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging / 'payload' / name, replacement)
            metadata = manifest['roots'][name]
            for path in [replacement, *replacement.rglob('*')]:
                mode = manifest.get('permissions', {}).get(name, {}).get(path.relative_to(replacement).as_posix(), metadata)
                if destination_root == Path('/'):
                    os.chown(path, mode['uid'], mode['gid'])
                path.chmod(mode['mode'])
            replacement.chmod(metadata['mode'])
            previous = target.with_name(target.name + '.restore-previous')
            if previous.exists():
                raise ValueError('Preserve or move the previous restoration before retrying')
            if target.exists():
                target.rename(previous)
            replacement.rename(target)
        return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['create', 'verify', 'restore'])
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--root', action='append', default=[], help='NAME=/absolute/path')
    parser.add_argument('--database', default='application/consumables.db')
    parser.add_argument('--versions', type=Path)
    parser.add_argument('--destination-root', type=Path, default=Path('/'))
    parser.add_argument('--services-stopped', action='store_true')
    args = parser.parse_args()
    if args.action in {'create', 'restore'} and not args.services_stopped:
        raise ValueError('Stop all application, DSH and notification writers first')
    if args.action == 'create':
        pairs = [value.split('=', 1) for value in args.root]
        roots = {name: Path(path) for name, path in pairs}
        if len(roots) != len(pairs) or not roots:
            raise ValueError('Provide distinct backup roots')
        versions = json.loads(args.versions.read_text()) if args.versions else {}
        create(args.snapshot, roots, args.database, versions)
    elif args.action == 'verify':
        verify(args.snapshot)
    else:
        restore(args.snapshot, args.destination_root)
    print(json.dumps({'action': args.action, 'snapshot': str(args.snapshot), 'verified': True}))


if __name__ == '__main__':
    main()
