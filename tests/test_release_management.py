from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import consistent_backup as backup
from scripts.build_dsh_runtime import dependency_closure
from scripts.configure_analysis_user import configure
from scripts.prepare_dsh_profile import prepare
from scripts.release_manifest import describe


def test_runtime_includes_transitive_workspace_peers_without_development_tools(tmp_path):
    for name, manifest in {
        'cli': {'name': 'cli', 'dependencies': {'boot': 'workspace:*'}, 'devDependencies': {'test-only': 'workspace:*'}},
        'boot': {'name': 'boot', 'peerDependencies': {'group': 'workspace:~'}},
        'group': {'name': 'group', 'peerDependencies': {'boot': 'workspace:*'}},
        'test-only': {'name': 'test-only'},
    }.items():
        path = tmp_path / 'apps' / name / 'package.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest))
    assert dependency_closure(tmp_path, {'cli': 'workspace:*'}) == {
        'cli': 'workspace:*', 'boot': 'workspace:*', 'group': 'workspace:~',
    }


def test_backup_failure_resumes_exactly_the_previously_active_writers(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(backup, 'active_writers', lambda: ['consumable-assistant.service'])
    monkeypatch.setattr(backup, 'stop_writers', lambda: calls.append('stop'))
    monkeypatch.setattr(backup, 'capture_configuration', lambda: calls.append('configuration'))
    monkeypatch.setattr(backup, 'resume_writers', lambda names: calls.append(('resume', names)))
    monkeypatch.setattr(backup, 'create', lambda *args: (_ for _ in ()).throw(OSError('full disk')))
    with pytest.raises(OSError, match='full disk'):
        backup.snapshot(tmp_path / 'snapshot')
    assert calls == ['stop', 'configuration', ('resume', ['consumable-assistant.service'])]


def test_backup_stops_all_writers_before_copying_persistent_data(tmp_path, monkeypatch):
    application = tmp_path / 'application'
    application.mkdir()
    with sqlite3.connect(application / 'consumables.db') as conn:
        conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, text TEXT)')
        conn.execute("INSERT INTO messages VALUES (1,'共享会话')")
    active = set(backup.WRITERS)
    stopped = []

    def systemctl(*arguments, **kwargs):
        if arguments[0] == 'show':
            value = 'loaded' if 'LoadState' in arguments[1] else 'active' if arguments[-1] in active else 'inactive'
            return SimpleNamespace(stdout=value)
        assert arguments[0] == 'stop'
        active.discard(arguments[1])
        stopped.append(arguments[1])
        return SimpleNamespace(stdout='')

    monkeypatch.setattr(backup, 'ROOTS', {'application': application})
    monkeypatch.setattr(backup, 'systemctl', systemctl)
    monkeypatch.setattr(backup, 'capture_configuration', lambda: None)
    result = backup.snapshot(tmp_path / 'snapshot', leave_stopped=True)
    assert stopped == list(backup.WRITERS)
    assert not active
    assert result['database_tables']['messages']['rows'] == 1
    assert result['versions']['active_writers'] == list(backup.WRITERS)


def test_subordinate_ids_preserve_existing_ranges_and_do_not_overlap(tmp_path):
    path = tmp_path / 'subuid'
    original = 'teacher:100000:65536\nother:200000:65536\n'
    path.write_text(original)
    configure(path)
    assert path.read_text() == original + 'lab-analysis:265536:65536\n'
    configure(path)
    assert path.read_text().count('lab-analysis:') == 1


def test_volatile_profile_contains_no_persistent_state(tmp_path, monkeypatch):
    release, home = tmp_path / 'release', tmp_path / 'run'
    source = release / 'runtime/profile'
    source.mkdir(parents=True)
    (source / 'cordis.yml').write_text('[]\n')
    (source / 'package.json').write_text(json.dumps({'dsh': {'profile': {'bundles': ['lab']}}}))
    (release / 'runtime/dsh/node_modules').mkdir(parents=True)
    (release / 'scripts').mkdir()
    overlay_source = Path(__file__).resolve().parents[1] / 'scripts/dsh-school.yml'
    (release / 'scripts/dsh-school.yml').write_bytes(overlay_source.read_bytes())
    monkeypatch.delenv('LAB_DSH_RELAY_TOKEN', raising=False)
    with pytest.raises(ValueError, match='relay credential'):
        prepare(release, home)
    monkeypatch.setenv('LAB_DSH_RELAY_TOKEN', 'synthetic-relay-token')
    prepare(release, home)
    prepare(release, home)
    profile = home / 'profiles/lab'
    assert (profile / 'node_modules').resolve() == release / 'runtime/dsh/node_modules'
    assert not (home / 'sessions').exists()
    overlay = (profile / 'cordis.yml').read_text()
    provider = yaml.load(overlay, Loader=yaml.BaseLoader)[0]
    assert provider['id'] == 'school-model'
    school = provider['config']['providers']['school']
    assert '/api/internal/school' in school['baseURL']
    assert 'LAB_DSH_RELAY_TOKEN' in school['headers']['Authorization']
    assert school['models'][0]['maxTokens'] == '1000'
    assert 'synthetic-relay-token' not in overlay
    assert json.loads((profile / 'package.json').read_text())['dsh']['profile']['bundles'] == ['lab']


def test_release_rejects_absolute_or_external_dependency_links(tmp_path):
    (tmp_path / 'package').symlink_to('/etc/passwd')
    with pytest.raises(ValueError, match='outside'):
        describe(tmp_path / 'package', tmp_path)
    (tmp_path / 'package').unlink()
    (tmp_path / 'real').write_text('runtime')
    (tmp_path / 'package').symlink_to('real')
    assert describe(tmp_path / 'package', tmp_path) == {'link': 'real'}
