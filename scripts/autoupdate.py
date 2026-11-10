"""Deploy the latest production commit only after CI publishes its immutable tag.

Uses outbound public Git, never a GitHub token, inbound webhook or remote runner.
All inventory changes and rollback remain the responsibility of deploy/install.sh.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.next')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def run(arguments: list[str], *, cwd: Path | None = None, timeout: int | None = 180,
        capture: bool = False, env: dict | None = None) -> str:
    result = subprocess.run(arguments, cwd=cwd, check=True, text=True, timeout=timeout,
                            stdout=subprocess.PIPE if capture else None, env=env)
    return result.stdout.strip() if capture else ''


class Updater:
    def __init__(self, config: dict):
        self.repository = config['repository']
        self.branch = config['branch']
        self.workflow = config['workflow']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', self.repository):
            raise ValueError('Invalid GitHub repository')
        run(['git', 'check-ref-format', 'refs/heads/' + self.branch], capture=True)
        if self.workflow != '.github/workflows/ci.yml':
            raise ValueError('Unsupported release workflow')
        self.minimum_free = int(config.get('minimum_free_bytes', 2 * 1024**3))
        if self.minimum_free < 2 * 1024**3:
            raise ValueError('Keep at least 2 GiB free after staging')
        self.cache = Path('/var/cache/lab-management-autoupdate')
        self.state = Path('/var/lib/lab-management-autoupdate')
        self.releases = Path('/opt/lab-management/releases')
        self.current = Path('/opt/lab-management/current')
        self.backups = Path('/var/backups/lab-management')
        self.git_env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
                            GIT_CONFIG_GLOBAL='/dev/null')

    @property
    def url(self) -> str:
        return 'https://github.com/' + self.repository + '.git'

    def git(self, *arguments: str) -> str:
        return run(['git', '--git-dir', str(self.cache / 'repository.git'), *arguments],
                   capture=True, env=self.git_env)

    def fetch_head(self) -> str:
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        repository = self.cache / 'repository.git'
        if not repository.exists():
            run(['git', 'init', '--bare', str(repository)])
            self.git('remote', 'add', 'origin', self.url)
        if self.git('remote', 'get-url', 'origin') != self.url:
            raise ValueError('Unexpected update repository URL')
        self.git('fetch', '--depth=1', '--no-tags', 'origin',
                 f'+refs/heads/{self.branch}:refs/heads/candidate')
        commit = self.git('rev-parse', 'refs/heads/candidate')
        if not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('Invalid commit from GitHub')
        return commit

    def authorization(self, commit: str) -> dict | None:
        reference = 'refs/tags/lab-ci/' + commit
        if not self.git('ls-remote', '--refs', 'origin', reference):
            return None
        self.git('fetch', '--depth=1', '--no-tags', 'origin', f'{reference}:{reference}')
        if self.git('cat-file', '-t', reference) != 'tag' or self.git('rev-parse', reference + '^{commit}') != commit:
            raise ValueError('The CI tag must be annotated and point to the candidate commit')
        authorization = json.loads(self.git('for-each-ref', '--format=%(contents)', reference))
        expected = {'format': 1, 'commit': commit, 'repository': self.repository,
                    'branch': self.branch, 'workflow': self.workflow}
        if any(authorization.get(key) != value for key, value in expected.items()):
            raise ValueError('CI authorization does not match this deployment')
        if not re.fullmatch(re.escape(f'https://github.com/{self.repository}/actions/runs/') + r'\d+',
                            authorization.get('run_url', '')):
            raise ValueError('Invalid CI run URL')
        return authorization

    def record(self, status: str, **details) -> dict:
        result = dict(status=status, checked_at=datetime.now(UTC).isoformat(),
                      repository=self.repository, branch=self.branch, **details)
        write_json(self.state / 'status.json', result)
        print(json.dumps(result), flush=True)
        return result

    def space_check(self, *, staging_bytes: int = 0) -> None:
        for path in (self.releases, self.backups):
            if shutil.disk_usage(path).free < self.minimum_free + staging_bytes:
                raise OSError(f'Insufficient free disk space: {path}. Existing releases and backups were retained.')

    def stage(self, commit: str) -> Path:
        stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
        release = self.releases / f'auto-{stamp}-{commit[:12]}'
        runtime_size = sum(path.stat().st_size for path in (self.current / 'runtime').rglob('*')
                           if path.is_file() and not path.is_symlink())
        self.space_check(staging_bytes=runtime_size + 512 * 1024**2)
        try:
            self.prepare(release, commit)
        except Exception:
            # No installer has run. Remove only this incomplete attempt so a
            # retried registry/network failure cannot fill the release disk.
            if release.exists():
                shutil.rmtree(release)
            raise
        return release

    def prepare(self, release: Path, commit: str) -> None:
        run(['git', 'clone', '--no-hardlinks', '--no-checkout', str(self.cache / 'repository.git'), str(release)])
        run(['git', 'checkout', '--detach', commit], cwd=release)
        expected = read_json(release / 'deploy/runtime.json')
        if not re.fullmatch('[0-9a-f]{40}', expected.get('dsh_commit', '')):
            raise ValueError('A release must pin its DSH runtime')
        # Verify the previous runtime before copying, so drift cannot be silently blessed.
        run(['python3', str(self.current / 'scripts/release_manifest.py'), 'verify', '--root', str(self.current)])
        if read_json(self.current / 'runtime/build.json') == expected:
            run(['cp', '--archive', '--reflink=auto', str(self.current / 'runtime'), str(release / 'runtime')])
        else:
            self.build_runtime(release, expected)
        if read_json(release / 'runtime/build.json') != expected:
            raise ValueError('Runtime build identity differs from deploy/runtime.json')
        run(['python3', 'scripts/release_manifest.py', 'create', '--root', str(release)], cwd=release)
        self.space_check()

    def build_runtime(self, release: Path, expected: dict) -> None:
        # Node/pnpm changes require deliberately provisioning the matching toolchain.
        node = self.current / 'runtime/node'
        if expected.get('node') != run([str(node / 'bin/node'), '--version'], capture=True):
            raise ValueError('Provision the pinned Node.js toolchain before changing its version')
        source = self.cache / ('dsh-' + expected['dsh_commit'])
        if not source.exists():
            run(['git', 'init', str(source)])
            run(['git', 'remote', 'add', 'origin', 'https://github.com/Maotechh/deepseek-harness.git'], cwd=source)
        run(['git', 'fetch', '--depth=1', 'origin', expected['dsh_commit']], cwd=source)
        run(['git', 'checkout', '--detach', expected['dsh_commit']], cwd=source)
        environment = dict(self.git_env, PATH=str(node / 'bin') + ':' + os.environ['PATH'])
        run(['python3', 'scripts/build_dsh_runtime.py', '--source', str(source),
             '--destination', str(release / 'runtime'), '--node-prefix', str(node)],
            cwd=release, timeout=None, env=environment)
        shutil.copytree(self.current / 'runtime/voice', release / 'runtime/voice', symlinks=True)

    def install(self, release: Path) -> None:
        # No timeout: never kill a database migration or a verified restoration halfway.
        run(['bash', str(release / 'deploy/install.sh')], cwd=release, timeout=None)

    def update(self, *, check_only: bool = False, retry: bool = False) -> dict:
        commit = self.fetch_head()
        installed = read_json(self.current / 'release-manifest.json').get('application_commit')
        if installed == commit:
            return self.record('current', commit=commit)
        authorization = self.authorization(commit)
        if authorization is None:
            return self.record('waiting_for_ci', commit=commit, installed_commit=installed)
        failed = read_json(self.state / 'failed.json')
        if failed.get('commit') == commit and not retry:
            return self.record('failed_commit_skipped', commit=commit, installed_commit=installed,
                               error='Fix the commit or explicitly retry after inspecting the previous failure')
        if check_only:
            return self.record('ready', commit=commit, installed_commit=installed, ci=authorization)
        release = None
        try:
            release = self.stage(commit)
            # Skip a superseded commit without pausing production writers.
            if self.fetch_head() != commit:
                if release.exists():
                    shutil.rmtree(release)
                return self.record('superseded', commit=commit, release=str(release))
            self.record('installing', commit=commit, release=str(release), ci=authorization)
            self.install(release)
            if read_json(self.current / 'release-manifest.json').get('application_commit') != commit:
                raise RuntimeError('Installer did not select the authorized release')
            result = self.record('deployed', commit=commit, release=str(release), ci=authorization)
            write_json(self.state / 'last-success.json', result)
            (self.state / 'failed.json').unlink(missing_ok=True)
            return result
        except Exception as exc:
            failure = self.record('failed', commit=commit, release=str(release) if release else None,
                                  error=f'{type(exc).__name__}: {exc}')
            # Storage/network/build preparation failures may recover on the next poll.
            # Only suppress repeat production installations of a failing revision.
            if release is not None:
                write_json(self.state / 'failed.json', failure)
            raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=Path('/etc/lab-management-autoupdate/config.json'))
    parser.add_argument('--check', action='store_true', help='Check readiness without staging or deploying')
    parser.add_argument('--retry', action='store_true', help='Explicitly retry a previously failed installation')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('Run the deployment coordinator as root')
    os.umask(0o077)
    with Path('/run/lab-management-autoupdate.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another automatic update is running; skipped.', flush=True)
            return
        updater = Updater(read_json(args.config))
        try:
            updater.update(check_only=args.check, retry=args.retry)
        except Exception as exc:
            print(f'Automatic update failed: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            raise


if __name__ == '__main__':
    main()
