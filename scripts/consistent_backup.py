"""Pause all durable writers while creating or restoring one complete snapshot."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.full_backup import create, restore, verify

WRITERS = ('consumable-notifications.service', 'consumable-assistant.service', 'consumable-dsh.service')
ROOTS = {
    'application': Path('/var/lib/lab-management'),
    'dsh': Path('/var/lib/lab-dsh'),
    'configuration': Path('/etc/lab-management'),
    'dsh_configuration': Path('/etc/lab-dsh'),
    'analysis_configuration': Path('/etc/lab-analysis'),
    'tls': Path('/etc/lab-management-tls'),
    'deployment': Path('/var/lib/lab-release-configuration'),
    'autoupdate_configuration': Path('/etc/lab-management-autoupdate'),
    'autoupdate_program': Path('/usr/local/lib/lab-management-autoupdate'),
}
UNITS = tuple(f'consumable-{name}.{suffix}' for name, suffix in (
    ('assistant', 'service'), ('notifications', 'service'), ('dsh', 'service'),
    ('analysis', 'service'), ('backup', 'service'), ('backup', 'timer'),
    ('autoupdate', 'service'), ('autoupdate', 'timer'),
))


def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(['systemctl', *arguments], check=check, capture_output=True, text=True)


def capture_configuration() -> None:
    root = ROOTS['deployment']
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in UNITS:
        source = Path('/etc/systemd/system') / name
        target = root / name
        if source.is_file():
            shutil.copyfile(source, target)
            target.chmod(0o600)
        else:
            target.unlink(missing_ok=True)
    source = Path('/etc/nginx/sites-available/lab-management')
    if source.is_file():
        shutil.copyfile(source, root / 'nginx.conf')
        (root / 'nginx.conf').chmod(0o600)


def restore_configuration() -> None:
    root = ROOTS['deployment']
    for name in UNITS:
        source = root / name
        target = Path('/etc/systemd/system') / name
        if source.is_file():
            shutil.copyfile(source, target)
            target.chmod(0o644)
        else:
            target.unlink(missing_ok=True)
    if (root / 'nginx.conf').is_file():
        shutil.copyfile(root / 'nginx.conf', '/etc/nginx/sites-available/lab-management')
        Path('/etc/nginx/sites-available/lab-management').chmod(0o644)
    systemctl('daemon-reload')
    subprocess.run(['nginx', '-t'], check=True, capture_output=True)
    systemctl('reload-or-restart', 'nginx')


def active_writers() -> list[str]:
    result = []
    for name in WRITERS:
        state = systemctl('show', '--property=ActiveState', '--value', name).stdout.strip()
        if state in {'active', 'activating', 'reloading', 'deactivating'}:
            result.append(name)
    return result


def stop_writers() -> None:
    for name in WRITERS:
        if systemctl('show', '--property=LoadState', '--value', name).stdout.strip() == 'loaded':
            systemctl('stop', name)
    if active_writers():
        raise RuntimeError('A durable writer did not stop')


def resume_writers(names: list[str]) -> None:
    for name in reversed(WRITERS):
        if name in names:
            systemctl('start', name)


def snapshot(destination: Path, leave_stopped: bool = False) -> dict:
    previous = active_writers()
    completed = False
    try:
        stop_writers()
        capture_configuration()
        version_file = Path('/etc/lab-management/release.json')
        versions = json.loads(version_file.read_text()) if version_file.exists() else {
            'application_release': str(Path('/opt/lab-management/current').resolve()),
            'format': 'previous-release',
        }
        versions['active_writers'] = previous
        create(destination, {name: root for name, root in ROOTS.items() if root.is_dir()},
               'application/consumables.db', versions)
        result = verify(destination)
        completed = True
        return result
    finally:
        if not leave_stopped or not completed:
            resume_writers(previous)


def restore_snapshot(source: Path, leave_stopped: bool = False) -> dict:
    manifest = verify(source)
    release = Path(manifest['versions']['application_release'])
    if release.parent != Path('/opt/lab-management/releases') or not release.is_dir():
        raise ValueError('The recorded application release must be available before restoration')
    if (release / 'release-manifest.json').is_file():
        from scripts.release_manifest import verify as verify_release

        verify_release(release)
    stop_writers()
    # A failed restoration deliberately leaves writers stopped for recovery.
    result = restore(source, Path('/'))
    temporary = Path('/opt/lab-management/current.restore-new')
    temporary.symlink_to(release)
    temporary.replace('/opt/lab-management/current')
    restore_configuration()
    if not leave_stopped:
        resume_writers(manifest['versions'].get('active_writers', list(WRITERS)))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['create', 'restore'])
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--leave-stopped', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('Run the consistent backup coordinator as root')
    with Path('/run/lab-management-release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source = args.snapshot or Path('/var/backups/lab-management') / datetime.now(UTC).strftime('complete-%Y%m%dT%H%M%S.%fZ')
        result = snapshot(source, args.leave_stopped) if args.action == 'create' else restore_snapshot(source, args.leave_stopped)
        print(json.dumps({'snapshot': str(source), 'verified': True, 'versions': result['versions']}))


if __name__ == '__main__':
    main()
