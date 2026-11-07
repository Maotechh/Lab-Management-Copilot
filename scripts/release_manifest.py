"""Record and verify exact application, DSH, Node.js and local voice release files."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

NODE_VERSION = 'v24.22.2'
PNPM_VERSION = '11.28.5'
VOICE_HASHES = {
    'sensevoice-onnx/model.int8.onnx': 'c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51',
    'sensevoice-onnx/tokens.txt': 'f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc',
    'silero/silero_vad.onnx': 'a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28',
}


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def describe(path: Path, root: Path) -> dict:
    if path.is_symlink():
        target = path.readlink()
        if target.is_absolute() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('A release link resolves outside the release directory')
        return {'link': str(target)}
    if not path.is_file():
        raise ValueError('A release contains a special or missing file')
    return {'sha256': checksum(path), 'bytes': path.stat().st_size}


def verify(root: Path) -> dict:
    manifest = json.loads((root / 'release-manifest.json').read_text())
    if manifest.get('format') != 1 or not re.fullmatch('[0-9a-f]{40}', manifest['application_commit']):
        raise ValueError('Invalid application release identity')
    if not re.fullmatch('[0-9a-f]{40}', manifest['runtime']['dsh_commit']):
        raise ValueError('Invalid DSH release identity')
    if manifest['runtime']['node'] != NODE_VERSION or manifest['runtime']['pnpm'] != PNPM_VERSION or manifest['runtime']['platform'] != 'linux-x64':
        raise ValueError('Unexpected DSH runtime versions')
    for name, expected in manifest['files'].items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or describe(root / path, root) != expected:
            raise ValueError(f'Release checksum mismatch: {name}')
    for name, digest in VOICE_HASHES.items():
        if checksum(root / 'runtime/voice' / name) != digest:
            raise ValueError('Local SenseVoice model checksum mismatch')
    return manifest


def create(root: Path) -> dict:
    result = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root,
                            check=True, capture_output=True, text=True)
    if result.stdout.strip():
        raise ValueError('Commit all tracked application changes before packaging')
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True, capture_output=True).stdout.decode().split('\0')
    paths = {name for name in tracked if name and (root / name).is_file()}
    paths.update(path.relative_to(root).as_posix() for path in (root / 'runtime').rglob('*')
                 if path.is_symlink() or not path.is_dir())
    manifest = {'format': 1, 'application_commit': commit,
                'runtime': json.loads((root / 'runtime/build.json').read_text()),
                'files': {name: describe(root / name, root) for name in sorted(paths)}}
    (root / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return verify(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['create', 'verify'])
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    result = create(args.root.resolve()) if args.action == 'create' else verify(args.root.resolve())
    print(json.dumps({'application_commit': result['application_commit'], 'runtime': result['runtime'],
                      'verified_files': len(result['files'])}))
