"""Rootless container execution owned by the separate analysis service."""
from __future__ import annotations

import fcntl
import io
import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

MAX_OUTPUT = 50 * 1024 * 1024


def execute(code: str, attachments: list[dict]) -> tuple[dict, list[tuple[str, bytes]]]:
    image = os.environ.get("LAB_ANALYSIS_IMAGE", "")
    if not re.fullmatch(r"(?:[a-zA-Z0-9./:_-]+@sha256:)?[0-9a-f]{64}", image):
        raise ValueError("分析环境尚未配置固定镜像")
    if os.geteuid() == 0:
        raise ValueError("分析必须由非 root 服务账户运行")
    info = subprocess.run(["podman", "--cgroup-manager=systemd", "info", "--format", "json"], capture_output=True, text=True, timeout=10, check=True)
    host = json.loads(info.stdout).get("host", {})
    if host.get("security", {}).get("rootless") is not True or host.get("cgroupVersion") != "v2" or host.get("cgroupManager") != "systemd":
        raise ValueError("分析环境必须使用 rootless Podman、cgroup v2 和 systemd 资源限制")
    if not attachments or len(attachments) > 8 or not code or len(code) > 50000:
        raise ValueError("请选择一至八个附件，并提供有效 Python 程序")
    with (Path(os.environ["LAB_ANALYSIS_STATE"]) / ".analysis.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("另一项分析正在运行，请稍后继续") from exc
        with tempfile.TemporaryDirectory(prefix="lab-analysis-", dir=os.environ["LAB_ANALYSIS_STATE"]) as folder:
            root = Path(folder)
            root.chmod(0o755)
            inputs = root / "inputs"
            inputs.mkdir(mode=0o755)
            inputs.chmod(0o755)
            manifest = []
            for attachment in attachments:
                name = attachment["id"] + Path(attachment["filename"]).suffix.lower()
                shutil.copyfile(attachment["path"], inputs / name)
                (inputs / name).chmod(0o444)
                manifest.append({"path": "/inputs/" + name, "filename": attachment["filename"], "sha256": attachment["sha256"]})
            (inputs / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
            (inputs / "manifest.json").chmod(0o444)
            program = root / "program.py"
            program.write_text(code)
            program.chmod(0o444)
            name = "lab-analysis-" + uuid.uuid4().hex
            command = ["podman", "--cgroup-manager=systemd", "run", "--pull=never", "--name", name, "--rm", "--network", "none", "--read-only",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
                       "--cpus", "1", "--memory", "1g", "--memory-swap", "1g", "--log-driver", "none",
                       "--user", "65534:65534", "--http-proxy=false", "--hostname", "analysis",
                       "--tmpfs", "/output:rw,nosuid,nodev,noexec,size=50m,mode=1777",
                       "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777",
                       "--volume", f"{inputs}:/inputs:ro,Z", "--volume", f"{program}:/program.py:ro,Z",
                       "--volume", f"{Path(__file__).with_name('analysis_runner.py')}:/runner.py:ro,Z",
                       image, "python", "-I", "/runner.py"]
            result = bytearray()
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        deadline = time.monotonic() + 60
                        while selector.get_map():
                            if time.monotonic() >= deadline:
                                raise ValueError("分析超过六十秒，已终止")
                            for key, _ in selector.select(0.2):
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                else:
                                    result.extend(chunk)
                                    if len(result) > MAX_OUTPUT + 256 * 1024:
                                        raise ValueError("分析输出超过 50 MB，已终止")
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                    if process.returncode:
                        raise ValueError("分析进程未完成，可能超过资源限制。请减少资料量或修改程序")
                finally:
                    subprocess.run(["podman", "--cgroup-manager=systemd", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
            return unpack(bytes(result))


def unpack(content: bytes) -> tuple[dict, list[tuple[str, bytes]]]:
    files, total = [], 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if len(archive.infolist()) > 201:
                raise ValueError("分析文件数量超过限制")
            for entry in archive.infolist():
                name = PurePosixPath(entry.filename)
                if name.is_absolute() or ".." in name.parts or "\\" in entry.filename or entry.is_dir():
                    raise ValueError("分析输出路径无效")
                if ((entry.external_attr >> 16) & 0o170000) not in {0, 0o100000}:
                    raise ValueError("分析输出只能包含普通文件")
                total += entry.file_size
                if total > MAX_OUTPUT + 1024:
                    raise ValueError("分析输出超过 50 MB")
                files.append((entry.filename, archive.read(entry)))
            metadata = [value for name, value in files if name == "__analysis__.json"]
            if len(metadata) != 1:
                raise ValueError("分析结果缺少执行状态")
            state = json.loads(metadata[0])
            if not isinstance(state, dict) or state.get("status") not in {"completed", "failed", "timed_out"}:
                raise ValueError("分析执行状态无效")
            if len({name for name, _ in files}) != len(files):
                raise ValueError("分析输出文件名重复")
            return state, [(name, value) for name, value in files if name != "__analysis__.json"]
    except (zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise ValueError("分析结果无法读取") from exc
