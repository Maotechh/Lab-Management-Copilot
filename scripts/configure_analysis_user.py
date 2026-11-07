"""Allocate nonoverlapping rootless Podman subordinate IDs without changing existing ranges."""
from __future__ import annotations

from pathlib import Path


def configure(path: Path, user: str = 'lab-analysis') -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    ranges = []
    found = False
    for line in lines:
        if not line or line.lstrip().startswith('#'):
            continue
        owner, start, count = line.split(':')
        first, length = int(start), int(count)
        ranges.append((first, first + length))
        if owner == user:
            if length < 65536:
                raise ValueError(f'The existing {user} range in {path} is too small')
            found = True
    if found:
        return
    candidate = 100000
    for start, end in sorted(ranges):
        if candidate + 65536 <= start:
            break
        if candidate < end:
            candidate = end
    lines.append(f'{user}:{candidate}:65536')
    path.write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    for path in (Path('/etc/subuid'), Path('/etc/subgid')):
        configure(path)
