from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.full_backup import create, restore, verify


def fixture(tmp_path):
    state = tmp_path / 'application'
    state.mkdir()
    with sqlite3.connect(state / 'consumables.db') as conn:
        conn.execute('CREATE TABLE inventory(id INTEGER PRIMARY KEY, quantity TEXT)')
        conn.execute("INSERT INTO inventory VALUES(1,'9.94')")
        conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, text TEXT)')
        conn.execute("INSERT INTO messages VALUES(1,'保留共享消息')")
    for name in ('photos', 'consumables-uploads', 'exports'):
        (state / name).mkdir()
        (state / name / 'original').write_bytes(b'original-' + name.encode())
    dsh = tmp_path / 'dsh'
    dsh.mkdir()
    (dsh / 'session.jsonl').write_text('{"message":1}\n')
    config = tmp_path / 'configuration'
    config.mkdir()
    (config / 'environment').write_text('KEY=fixture-only\n')
    snapshot = tmp_path / 'backups' / 'release'
    create(snapshot, {'application': state, 'dsh': dsh, 'configuration': config},
           'application/consumables.db', {'application_commit': 'fixture'})
    return state, dsh, config, snapshot


def test_full_restoration_preserves_rows_and_all_associated_files(tmp_path):
    state, dsh, config, snapshot = fixture(tmp_path)
    result = verify(snapshot)
    assert result['database_tables']['messages']['rows'] == 1
    target = tmp_path / 'replacement-machine'
    restore(snapshot, target)
    restored = target / state.relative_to('/')
    with sqlite3.connect(restored / 'consumables.db') as conn:
        assert conn.execute('SELECT quantity FROM inventory').fetchone()[0] == '9.94'
        assert conn.execute('SELECT text FROM messages').fetchone()[0] == '保留共享消息'
    for name in ('photos', 'consumables-uploads', 'exports'):
        assert (restored / name / 'original').read_bytes() == (state / name / 'original').read_bytes()
    assert (target / dsh.relative_to('/') / 'session.jsonl').read_bytes() == (dsh / 'session.jsonl').read_bytes()
    assert (target / config.relative_to('/') / 'environment').read_bytes() == (config / 'environment').read_bytes()


def test_corrupt_snapshot_cannot_replace_any_existing_data(tmp_path):
    state, _, _, snapshot = fixture(tmp_path)
    path = snapshot / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['files']['application/consumables-uploads/original']['sha256'] = '0' * 64
    path.write_text(json.dumps(manifest))
    target = tmp_path / 'replacement-machine'
    existing = target / state.relative_to('/')
    existing.mkdir(parents=True)
    (existing / 'preserve').write_text('existing')
    with pytest.raises(ValueError, match='checksums differ'):
        restore(snapshot, target)
    assert (existing / 'preserve').read_text() == 'existing'


def test_snapshot_rejects_external_symbolic_links(tmp_path):
    root = tmp_path / 'unsafe'
    root.mkdir()
    (root / 'credentials').symlink_to('/etc/passwd')
    with pytest.raises(ValueError):
        create(tmp_path / 'backup', {'application': root}, 'application/consumables.db', {})
