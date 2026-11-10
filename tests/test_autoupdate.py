from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import autoupdate as auto

COMMIT = 'b' * 40
PREVIOUS = 'a' * 40


@pytest.fixture
def updater(tmp_path, monkeypatch):
    result = auto.Updater({'repository': 'Maotechh/Lab-Management-Copilot',
                           'branch': 'codex/dsh-lab-delivery', 'workflow': '.github/workflows/ci.yml'})
    for name in ('cache', 'state', 'releases', 'backups'):
        path = tmp_path / name
        path.mkdir()
        setattr(result, name, path)
    result.current = tmp_path / 'current'
    result.current.mkdir()
    auto.write_json(result.current / 'release-manifest.json', {'application_commit': PREVIOUS})
    monkeypatch.setattr(result, 'fetch_head', lambda: COMMIT)
    return result


def authorization(updater):
    return {'format': 1, 'commit': COMMIT, 'repository': updater.repository,
            'branch': updater.branch, 'workflow': updater.workflow,
            'run_url': 'https://github.com/Maotechh/Lab-Management-Copilot/actions/runs/123'}


def forbid_deployment(monkeypatch, updater):
    def unexpected(*args, **kwargs):
        pytest.fail('Production deployment must not run')
    monkeypatch.setattr(updater, 'stage', unexpected)
    monkeypatch.setattr(updater, 'install', unexpected)


def test_no_ci_authorization_never_stages_or_changes_production(updater, monkeypatch):
    monkeypatch.setattr(updater, 'authorization', lambda commit: None)
    forbid_deployment(monkeypatch, updater)
    assert updater.update()['status'] == 'waiting_for_ci'
    assert auto.read_json(updater.current / 'release-manifest.json')['application_commit'] == PREVIOUS


def test_current_release_does_not_require_another_ci_or_install(updater, monkeypatch):
    monkeypatch.setattr(updater, 'fetch_head', lambda: PREVIOUS)
    monkeypatch.setattr(updater, 'authorization', lambda commit: pytest.fail('Already deployed'))
    forbid_deployment(monkeypatch, updater)
    assert updater.update()['status'] == 'current'


def test_check_only_reports_authorization_without_deploying(updater, monkeypatch):
    monkeypatch.setattr(updater, 'authorization', lambda commit: authorization(updater))
    forbid_deployment(monkeypatch, updater)
    assert updater.update(check_only=True)['status'] == 'ready'


@pytest.mark.parametrize('fault', ['commit', 'branch', 'repository', 'workflow', 'run_url', 'format', 'lightweight'])
def test_unrelated_or_forged_tag_metadata_is_rejected(updater, monkeypatch, fault):
    metadata = authorization(updater)
    metadata[fault] = 'wrong'

    def git(*args):
        if args[0] == 'ls-remote':
            return 'tag-exists'
        if args[0] == 'fetch':
            return ''
        if args[0] == 'cat-file':
            return 'commit' if fault == 'lightweight' else 'tag'
        if args[0] == 'rev-parse':
            return COMMIT
        return json.dumps(metadata)

    monkeypatch.setattr(updater, 'git', git)
    with pytest.raises(ValueError):
        updater.authorization(COMMIT)


def test_valid_ci_tag_requires_annotated_tag_and_exact_commit(updater, monkeypatch):
    metadata = authorization(updater)
    def git(*args):
        return {'ls-remote': 'exists', 'fetch': '', 'cat-file': 'tag',
                'rev-parse': COMMIT, 'for-each-ref': json.dumps(metadata)}[args[0]]
    monkeypatch.setattr(updater, 'git', git)
    assert updater.authorization(COMMIT) == metadata
    monkeypatch.setattr(updater, 'git', lambda *args: PREVIOUS if args[0] == 'rev-parse' else git(*args))
    with pytest.raises(ValueError, match='candidate commit'):
        updater.authorization(COMMIT)


def test_superseded_commit_does_not_pause_or_install_production(updater, monkeypatch):
    heads = iter((COMMIT, 'c' * 40))
    monkeypatch.setattr(updater, 'fetch_head', lambda: next(heads))
    monkeypatch.setattr(updater, 'authorization', lambda commit: authorization(updater))
    monkeypatch.setattr(updater, 'stage', lambda commit: updater.releases / commit)
    monkeypatch.setattr(updater, 'install', lambda release: pytest.fail('Newer commit exists'))
    assert updater.update()['status'] == 'superseded'
    assert not (updater.state / 'failed.json').exists()


def test_failed_install_is_not_repeated_until_explicit_retry(updater, monkeypatch):
    monkeypatch.setattr(updater, 'authorization', lambda commit: authorization(updater))
    monkeypatch.setattr(updater, 'stage', lambda commit: updater.releases / commit)
    attempts = []
    def install(release):
        attempts.append(release)
        if len(attempts) == 1:
            raise subprocess.CalledProcessError(1, ['bash', 'deploy/install.sh'])
        auto.write_json(updater.current / 'release-manifest.json', {'application_commit': COMMIT})
    monkeypatch.setattr(updater, 'install', install)
    with pytest.raises(subprocess.CalledProcessError):
        updater.update()
    assert updater.update()['status'] == 'failed_commit_skipped'
    assert len(attempts) == 1
    assert updater.update(retry=True)['status'] == 'deployed'
    assert len(attempts) == 2
    assert not (updater.state / 'failed.json').exists()
    assert auto.read_json(updater.state / 'last-success.json')['commit'] == COMMIT


def test_installer_success_without_selected_commit_is_failure(updater, monkeypatch):
    monkeypatch.setattr(updater, 'authorization', lambda commit: authorization(updater))
    monkeypatch.setattr(updater, 'stage', lambda commit: updater.releases / commit)
    monkeypatch.setattr(updater, 'install', lambda release: None)
    with pytest.raises(RuntimeError, match='select'):
        updater.update()
    assert (updater.state / 'failed.json').is_file()
    assert not (updater.state / 'last-success.json').exists()


def test_preparation_failure_can_retry_without_repeating_production_changes(updater, monkeypatch):
    monkeypatch.setattr(updater, 'authorization', lambda commit: authorization(updater))
    monkeypatch.setattr(updater, 'stage', lambda commit: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        updater.update()
    assert not (updater.state / 'failed.json').exists()


def test_insufficient_storage_preserves_existing_backups(updater, monkeypatch):
    backup = updater.backups / 'verified-snapshot'
    backup.write_text('retained')
    monkeypatch.setattr(auto.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1024))
    with pytest.raises(OSError, match='Insufficient free disk'):
        updater.space_check(staging_bytes=1024)
    assert backup.read_text() == 'retained'


def test_failed_staging_removes_only_its_uninstalled_candidate(updater, monkeypatch):
    existing = updater.releases / 'previous-installed-release'
    existing.mkdir()
    (existing / 'retained').write_text('previous version')
    monkeypatch.setattr(updater, 'space_check', lambda **kwargs: None)
    def prepare(release, commit):
        release.mkdir()
        (release / 'partial-runtime').write_text('incomplete')
        raise OSError('registry unavailable')
    monkeypatch.setattr(updater, 'prepare', prepare)
    with pytest.raises(OSError, match='registry unavailable'):
        updater.stage(COMMIT)
    assert list(updater.releases.iterdir()) == [existing]
    assert (existing / 'retained').read_text() == 'previous version'


def test_publish_script_creates_idempotent_annotated_authorization(tmp_path):
    """Exercise the actual CI publishing script against a local bare repository."""
    import os
    from pathlib import Path

    origin, checkout = tmp_path / 'origin.git', tmp_path / 'checkout'
    auto.run(['git', 'init', '--bare', str(origin)])
    auto.run(['git', 'clone', str(origin), str(checkout)])
    (checkout / 'README').write_text('release')
    auto.run(['git', 'add', 'README'], cwd=checkout)
    auto.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.org',
              'commit', '-m', 'release'], cwd=checkout)
    commit = auto.run(['git', 'rev-parse', 'HEAD'], cwd=checkout, capture=True)
    environment = dict(os.environ, DEPLOY_COMMIT=commit, DEPLOY_BRANCH='codex/dsh-lab-delivery',
                       DEPLOY_REPOSITORY='Maotechh/Lab-Management-Copilot',
                       DEPLOY_RUN_URL='https://github.com/Maotechh/Lab-Management-Copilot/actions/runs/123')
    script = Path(__file__).resolve().parents[1] / 'deploy/publish-ci-release.sh'
    for _ in range(2):
        auto.run(['bash', str(script)], cwd=checkout, env=environment)
    reference = f'refs/tags/lab-ci/{commit}'
    assert auto.run(['git', '--git-dir', str(origin), 'cat-file', '-t', reference], capture=True) == 'tag'
    metadata = json.loads(auto.run(['git', '--git-dir', str(origin), 'for-each-ref',
                                    '--format=%(contents)', reference], capture=True))
    assert metadata['commit'] == commit
    assert metadata['branch'] == 'codex/dsh-lab-delivery'
