"""Unix-socket broker with no database, application settings, or credential access."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import socketserver
import subprocess
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .analysis_sandbox import execute

MAX_REQUEST = 160 * 1024 * 1024 + 128 * 1024
LOCK = threading.Lock()


def selected_inputs(content: bytes, root: Path) -> tuple[str, list[dict]]:
    """Decode only named ordinary ZIP members; never accept host paths."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) < 3 or len(entries) > 10 or len({entry.filename for entry in entries}) != len(entries):
            raise ValueError("Invalid input count")
        if sum(entry.file_size for entry in entries) > MAX_REQUEST:
            raise ValueError("Input size limit exceeded")
        if any(entry.file_size > 20 * 1024 * 1024 or entry.is_dir() or ((entry.external_attr >> 16) & 0o170000) not in {0, 0o100000} for entry in entries):
            raise ValueError("Invalid input member")
        code = archive.read('program.py').decode('utf-8')
        manifest = json.loads(archive.read('manifest.json'))
        if not code or len(code) > 50000 or not isinstance(manifest, list) or not 1 <= len(manifest) <= 8:
            raise ValueError("Invalid analysis request")
        allowed = {'program.py', 'manifest.json'}
        attachments = []
        for index, item in enumerate(manifest):
            if not isinstance(item, dict) or not isinstance(item.get('filename'), str):
                raise ValueError("Invalid attachment metadata")
            member = str(item.get('path', '')).removeprefix('/')
            if not re.fullmatch(r'inputs/[0-7]\.(?:xls|xlsx|csv|jpg|jpeg|png|webp|heic|heif)', member):
                raise ValueError("Invalid selected input path")
            allowed.add(member)
            payload = archive.read(member)
            digest = hashlib.sha256(payload).hexdigest()
            if item.get('sha256') != digest:
                raise ValueError("Input checksum mismatch")
            path = root / (str(index) + Path(member).suffix)
            path.write_bytes(payload)
            attachments.append({'id': str(index), 'path': str(path), 'filename': Path(item['filename']).name, 'sha256': digest})
        if allowed != {entry.filename for entry in entries}:
            raise ValueError("Unexpected or repeated selected input")
        return code, attachments


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Requests contain private attachment content; retain no HTTP access log.
        return

    def do_POST(self):
        if self.path != '/execute':
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_REQUEST:
            self.send_error(413)
            return
        if not LOCK.acquire(blocking=False):
            self.send_error(429)
            return
        try:
            self.connection.settimeout(15)
            content = self.rfile.read(length)
            if len(content) != length:
                raise ValueError('Truncated request')
            with tempfile.TemporaryDirectory(prefix='lab-selected-', dir=os.environ['LAB_ANALYSIS_STATE']) as folder:
                code, attachments = selected_inputs(content, Path(folder))
                metadata, files = execute(code, attachments)
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
                archive.writestr('__analysis__.json', json.dumps(metadata))
                for name, value in files:
                    archive.writestr(name, value)
            result = output.getvalue()
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', str(len(result)))
            self.end_headers()
            self.wfile.write(result)
        except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile, subprocess.SubprocessError):
            self.send_error(422, 'Analysis did not complete')
        finally:
            LOCK.release()


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    path = Path(os.environ.get('LAB_ANALYSIS_SOCKET', '/run/lab-analysis/broker.sock'))
    path.unlink(missing_ok=True)
    with Server(str(path), Handler) as server:
        path.chmod(0o660)
        server.serve_forever()


if __name__ == '__main__':
    main()
