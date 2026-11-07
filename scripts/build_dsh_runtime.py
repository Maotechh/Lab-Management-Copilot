"""Build a relocatable Linux DSH dependency tree from one committed source version."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

NODE_VERSION = 'v24.22.2'
PNPM_VERSION = '11.28.5'


def dependency_closure(source: Path, roots: dict[str, str]) -> dict[str, str]:
    """Include required workspace peers that pnpm production deploy omits."""
    manifests = {}
    for pattern in ('vendor/*/package.json', 'packages/*/*/package.json',
                    'native/system/package.json', 'native/system/packages/*/package.json',
                    'apps/*/package.json'):
        for path in source.glob(pattern):
            manifest = json.loads(path.read_text())
            manifests[manifest['name']] = manifest
    result = dict(roots)
    pending = list(roots)
    while pending:
        name = pending.pop()
        manifest = manifests[name]
        for section in ('dependencies', 'peerDependencies', 'optionalDependencies'):
            for dependency, version in manifest.get(section, {}).items():
                if dependency in manifests and dependency not in result:
                    result[dependency] = version if version.startswith('workspace:') else 'workspace:*'
                    pending.append(dependency)
    return result


@contextmanager
def build_directory(parent: Path):
    directory = Path(tempfile.mkdtemp(prefix='lab-dsh-build-', dir=parent))
    try:
        yield directory
    except BaseException:
        print(f'Build files retained for diagnosis: {directory}', flush=True)
        raise
    else:
        shutil.rmtree(directory)


def run(arguments: list[str], cwd: Path, capture: bool = False) -> str:
    result = subprocess.run(arguments, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ''


def build(source: Path, destination: Path, node_prefix: Path) -> None:
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise ValueError('Build this server runtime on Linux x86_64')
    if destination.exists():
        raise ValueError('The runtime destination already exists')
    node = node_prefix / 'bin' / 'node'
    if run([str(node), '--version'], source, True) != NODE_VERSION or run(['pnpm', '--version'], source, True) != PNPM_VERSION:
        raise ValueError('Use the pinned Node.js and pnpm versions')
    if Path(shutil.which('node') or '').resolve() != node.resolve():
        raise ValueError('The pinned Node.js bin directory must be first on PATH')
    if run(['git', 'status', '--porcelain', '--untracked-files=no'], source, True):
        raise ValueError('Commit the DSH changes before producing a release runtime')
    commit = run(['git', 'rev-parse', 'HEAD'], source, True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with build_directory(destination.parent) as directory:
        temporary = Path(directory)
        tree = temporary / 'source'
        tree.mkdir()
        archive = temporary / 'source.tar'
        run(['git', 'archive', '--format=tar', '-o', str(archive), commit], source)
        with tarfile.open(archive) as stream:
            stream.extractall(tree, filter='data')
        lock = tree / 'pnpm-lock.yaml'
        original_lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()
        run(['pnpm', 'install', '--frozen-lockfile'], tree)
        run(['env', f'DSH_CLIENT_COMMIT_HASH={commit}', 'pnpm', 'run', 'build:artifacts'], tree)
        # A dependency-only deployment importer adds no model capabilities.
        # Its registry resolutions must remain exactly those in the source lock.
        run(['node', '--input-type=module', '-e',
             "import{readFileSync,writeFileSync}from'node:fs';import{load}from'js-yaml';"
             "writeFileSync('registry-lock.json',JSON.stringify(load(readFileSync('pnpm-lock.yaml','utf8')).packages))"], tree)
        profile_dependencies = {
            '@deepseek-ai/dsh': 'workspace:*',
            '@deepseek-ai/dsh-lab-copilot': 'workspace:*',
        }
        dependencies = dependency_closure(tree, profile_dependencies)
        closure = tree / 'apps/lab-delivery'
        closure.mkdir()
        (closure / 'package.json').write_text(json.dumps({
            'name': 'lab-delivery-runtime', 'private': True, 'version': '1.0.0',
            'type': 'module', 'dependencies': dependencies,
        }, indent=2) + '\n')
        run(['pnpm', 'install', '--lockfile-only', '--ignore-scripts'], tree)
        run(['node', '--input-type=module', '-e',
             "import{readFileSync}from'node:fs';import{load}from'js-yaml';"
             "if(JSON.stringify(load(readFileSync('pnpm-lock.yaml','utf8')).packages)!==readFileSync('registry-lock.json','utf8'))"
             "throw Error('Deployment changed pinned registry dependencies')"], tree)
        payload = temporary / 'runtime'
        payload.mkdir()
        run(['pnpm', '--filter', 'lab-delivery-runtime', 'deploy', '--prod',
             '--config.allow-unused-patches=true', '--config.node-linker=hoisted',
             '--config.pnpmfile=' + str(tree / 'scripts/python-runtime-deploy.pnpmfile.mjs'),
             '--config.link-workspace-packages=true', '--config.hoist-workspace-packages=false', str(payload / 'dsh')], tree)
        helper = temporary / 'materialize.mts'
        helper.write_text(
            'import {materializeStagedLinks,deduplicateStagedWorkspacePackages} from '
            + json.dumps(str(tree / 'scripts/build-exe-for-python-sdk-staging.ts')) + ';\n'
            + 'await materializeStagedLinks(' + json.dumps(str(payload / 'dsh')) + ');\n'
            + 'await deduplicateStagedWorkspacePackages(' + json.dumps(str(payload / 'dsh')) + ','
            + json.dumps(str(payload / 'dsh')) + ');\n')
        run(['node', '--import', str(tree / 'node_modules/tsx/dist/esm/index.mjs'), str(helper)], tree)
        shutil.copytree(node_prefix, payload / 'node', symlinks=True)
        profile = payload / 'profile'
        profile.mkdir()
        (profile / 'cordis.yml').write_text('[]\n')
        installed = {name: json.loads((payload / 'dsh/node_modules' / name / 'package.json').read_text())['version']
                     for name in profile_dependencies}
        (profile / 'package.json').write_text(json.dumps({
            'name': 'lab-profile', 'private': True, 'type': 'module', 'dependencies': installed,
            'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-lab-copilot']}},
        }, indent=2) + '\n')
        shutil.copyfile(lock, payload / 'pnpm-lock.yaml')
        (payload / 'build.json').write_text(json.dumps({
            'dsh_commit': commit, 'node': NODE_VERSION, 'pnpm': PNPM_VERSION, 'platform': 'linux-x64',
            'source_lock_sha256': original_lock_hash,
            'deployment_lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest(),
        }, indent=2) + '\n')
        run([str(payload / 'node/bin/node'), str(payload / 'dsh/node_modules/@deepseek-ai/dsh/lib/bin.js'), '--version'], tree)
        os.rename(payload, destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--node-prefix', required=True, type=Path)
    args = parser.parse_args()
    build(args.source.resolve(), args.destination.resolve(), args.node_prefix.resolve())
