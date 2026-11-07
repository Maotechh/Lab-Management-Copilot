"""Recreate the explicit lab profile in a volatile DSH home before service launch."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def prepare(release: Path, home: Path) -> None:
    root = home / 'profiles' / 'lab'
    root.mkdir(parents=True, exist_ok=True, mode=0o750)
    for name in ('package.json', 'cordis.yml'):
        shutil.copyfile(release / 'runtime' / 'profile' / name, root / name)
    modules = root / 'node_modules'
    if modules.is_symlink():
        modules.unlink()
    elif modules.exists():
        raise ValueError('The volatile profile contains an unexpected dependency directory')
    modules.symlink_to(release / 'runtime' / 'dsh' / 'node_modules', target_is_directory=True)


if __name__ == '__main__':
    prepare(Path(sys.argv[1]).resolve(), Path(os.environ['DSH_HOME']))
