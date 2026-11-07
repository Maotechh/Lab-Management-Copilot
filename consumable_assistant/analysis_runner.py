"""Container-only runner: retain bounded ordinary files and emit one ZIP archive."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path


def main():
    cpu, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    memory = Path("/sys/fs/cgroup/memory.max").read_text().strip()
    if cpu == "max" or int(cpu) > int(period) or memory == "max" or int(memory) > 1024 ** 3:
        raise ValueError("Analysis resource limits are not active")
    output = Path("/output")
    with (output / "analysis.log").open("wb") as log:
        try:
            process = subprocess.run([sys.executable, "-I", "/program.py"], cwd=output,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     timeout=55, check=False,
                                     env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp"})
            result = {"status": "completed" if process.returncode == 0 else "failed", "exit_code": process.returncode}
        except subprocess.TimeoutExpired:
            result = {"status": "timed_out"}
    files, total = [], 0
    for directory, folders, names in os.walk(output, followlinks=False):
        folders[:] = [name for name in folders if not (Path(directory) / name).is_symlink()]
        for name in names:
            path = Path(directory) / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                continue
            total += metadata.st_size
            files.append(path)
            if total > 50 * 1024 * 1024 or len(files) > 200:
                raise ValueError("Analysis output limit exceeded")
    with zipfile.ZipFile(sys.stdout.buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in files:
            if path.relative_to(output).as_posix() != "__analysis__.json":
                archive.write(path, path.relative_to(output).as_posix())
        archive.writestr("__analysis__.json", json.dumps(result))


if __name__ == "__main__":
    main()
