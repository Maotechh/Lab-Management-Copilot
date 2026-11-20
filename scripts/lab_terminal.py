#!/usr/bin/env python3
"""Collect bounded, read-only lab diagnostics from Windows, WSL or the server."""

import argparse
import json
import os
import re
import shutil
import ssl
import subprocess
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, build_opener

SITE = "https://10.15.21.115"
RUN_ID = re.compile(r"[0-9a-f]{32}")
COMMIT = re.compile(r"[0-9a-f]{40}")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?")
ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
SERVICE_VALUE = re.compile(r"[a-zA-Z0-9-]{1,40}")
MODEL_HTTP_STATUS = re.compile(r"API error \(([45][0-9]{2})\)", re.IGNORECASE)
DSH_SESSIONS = Path("/var/lib/lab-dsh/sessions/_no-cwd")
MAX_SESSION_BYTES = 2 * 1024 * 1024
SERVICES = (
    "nginx.service", "consumable-assistant.service", "consumable-dsh.service",
    "consumable-analysis.service", "consumable-notifications.service",
    "consumable-backup.service", "consumable-backup.timer", "consumable-autoupdate.service",
    "consumable-autoupdate.timer",
)
SIGNALS = {
    "connection_refused": r"connection refused|could(?:n't| not) connect to server",
    "upstream_timeout": r"upstream timed out|connecttimeout|readtimeout",
    "storage_full": r"no space left on device|insufficient free disk",
    "permission_denied": r"permission denied",
    "database_locked": r"database is locked",
    "restore_conflict": r"previous restoration|automatic restoration failed",
    "school_chat_failed": r"no verified read-only dsh inventory answer",
    "school_gateway_failed": r"school (?:https preflight failed|streaming gateway unavailable|preflight returned http)",
    "dsh_relay_unavailable": r"missing dsh model relay credential",
    "startup_failed": r"application startup failed|start request repeated too quickly",
    "service_stopped": r"(?:^|:\s)stopped consumable-",
    "service_started": r"(?:^|:\s)started consumable-",
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def scalar(data, key, pattern):
    value = data.get(key)
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def choice(data, key, *allowed):
    value = data.get(key)
    return value if value in allowed else None


def http_check(base, path, *, ca=None, read_json=False):
    try:
        context = ssl.create_default_context(cafile=ca)
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
        with opener.open(base + path, timeout=8) as response:
            if not read_json:
                return {"http_status": response.status}, {}
            body = response.read(128 * 1024 + 1)
            if len(body) > 128 * 1024:
                return {"http_status": response.status, "error": "response_too_large"}, {}
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
            return {"http_status": response.status}, payload
    except HTTPError as error:
        error.close()
        return {"http_status": error.code}, {}
    except (OSError, ValueError, HTTPException) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, ssl.SSLCertVerificationError):
            kind = "certificate_verification"
        elif isinstance(reason, TimeoutError):
            kind = "timeout"
        elif isinstance(reason, ConnectionRefusedError):
            kind = "connection_refused"
        else:
            kind = type(reason).__name__ if isinstance(reason, BaseException) else "connection_error"
        return {"error": kind}, {}


def chat_fields(data):
    result = data.get("result")
    return {
        "run_id": scalar(data, "run_id", RUN_ID),
        "status": choice(data, "status", "queued", "running", "completed", "failed", "cancelled"),
        "error_code": scalar(result if isinstance(result, dict) else {}, "error_code", ERROR_CODE)
        or scalar(data, "error", ERROR_CODE),
        "started_at": scalar(data, "started_at", TIME),
        "completed_at": scalar(data, "completed_at", TIME),
    }


def web_report(base, ca=None, run_id=None):
    home, _ = http_check(base, "/", ca=ca)
    config, app = http_check(base, "/api/config", ca=ca, read_json=True)
    ai_http, ai = http_check(base, "/api/ai/status", ca=ca, read_json=True)
    report = {
        "site": base,
        "website": home,
        "application": {**config, "profile": choice(app, "profile", "production", "simulation")},
        "model_health": {
            **ai_http,
            "status": choice(ai, "status", "available", "unavailable", "unknown", "stale", "unconfigured"),
            "checked_at": scalar(ai, "checked_at", TIME),
        },
    }
    if run_id:
        info, data = http_check(base, f"/api/chat/runs/{run_id}", ca=ca, read_json=True)
        report["chat"] = {**info, **chat_fields(data)}
    return report


def command(*args):
    try:
        result = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", timeout=8, check=False,
        )
        return result.stdout, result.returncode
    except (OSError, TimeoutError, subprocess.TimeoutExpired):
        return "", None


def service_status():
    properties = ("Id", "LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus", "NRestarts")
    output, code = command(
        "systemctl", "show", "--no-pager", "--property=" + ",".join(properties), *SERVICES,
    )
    units = {}
    for block in output.split("\n\n"):
        row = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name = row.pop("Id", None)
        if name in SERVICES:
            units[name] = {key: scalar(row, key, SERVICE_VALUE) for key in properties[1:]}
    return {"command_exit": code, "units": units}


def recent_signals():
    units = [option for name in SERVICES if name.endswith(".service") for option in ("-u", name)]
    output, code = command("journalctl", "--no-pager", "-n", "160", "-o", "json", *units)
    signals = []
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        unit = row.get("UNIT") or row.get("_SYSTEMD_UNIT")
        message = row.get("MESSAGE")
        if unit not in SERVICES or not isinstance(message, str):
            continue
        for name, pattern in SIGNALS.items():
            if re.search(pattern, message[:4096], re.IGNORECASE):
                timestamp = scalar(row, "__REALTIME_TIMESTAMP", re.compile(r"\d{1,20}"))
                signals.append({"unit": unit, "signal": name, "epoch_seconds": int(timestamp) // 1_000_000 if timestamp else None})
    return {"command_exit": code, "signals": signals[-20:]}


def local_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def timestamp_ms(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def session_failure(run, root=DSH_SESSIONS):
    """Return only the terminal model error identified inside this run's time window."""
    conversation = scalar(run, "conversation_id", RUN_ID)
    started = scalar(run, "started_at", TIME)
    finished = scalar(run, "completed_at", TIME)
    if not conversation or not started or not finished:
        return {"status": "missing_run_metadata"}
    try:
        start_ms = timestamp_ms(started)
        finish_ms = timestamp_ms(finished)
    except ValueError:
        return {"status": "invalid_run_time"}
    path = root / f"lab-{conversation}" / "session.v4.jsonl"
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            offset = max(0, size - MAX_SESSION_BYTES)
            stream.seek(offset)
            lines = stream.read(MAX_SESSION_BYTES).splitlines()
    except PermissionError:
        return {"status": "access_denied"}
    except OSError:
        return {"status": "session_unavailable"}

    events = []
    for line in lines[1:] if offset else lines:
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        timestamp = event.get("time")
        if type(timestamp) is int and start_ms <= timestamp < finish_ms + 1000:
            if event.get("type") in ("turn/start", "turn/end"):
                events.append(event)
    starts = [event for event in events if event["type"] == "turn/start"]
    ends = [event for event in events if event["type"] == "turn/end"]
    if len(starts) > 1 or len(ends) > 1:
        return {"status": "ambiguous_time_window"}
    if not starts or not ends or starts[0]["time"] > ends[0]["time"]:
        return {"status": "no_matching_turn"}
    data = ends[0].get("data")
    reason = data.get("reason") if isinstance(data, dict) else None
    error = reason.get("error") if isinstance(reason, dict) and reason.get("kind") == "error" else None
    if not isinstance(error, dict):
        return {"status": "no_model_error"}
    message = error.get("message")
    http_status = MODEL_HTTP_STATUS.search(message[:4096]) if isinstance(message, str) else None
    return {
        "status": "matched",
        "error_code": scalar(error, "code", ERROR_CODE),
        "upstream_http_status": int(http_status.group(1)) if http_status else None,
    }


def server_report(run_id):
    base = "http://127.0.0.1:8000"
    loopback = web_report(base)
    if run_id:
        info, run = http_check(base, f"/api/chat/runs/{run_id}", read_json=True)
        loopback["chat"] = {**info, **chat_fields(run)}
        if run.get("status") == "failed":
            loopback["chat"]["dsh_error"] = session_failure(run)
    dsh, dsh_data = http_check("http://127.0.0.1:8766", "/health", read_json=True)
    update = local_json("/var/lib/lab-management-autoupdate/status.json")
    release = local_json("/etc/lab-management/release.json")
    return {
        "application_loopback": loopback,
        "dsh_loopback": {**dsh, "status": choice(dsh_data, "status", "ok")},
        "services": service_status(),
        "journal": recent_signals(),
        "disk_free_gib": {path: round(shutil.disk_usage(path).free / 1024**3, 2)
                          for path in ("/", "/data") if Path(path).is_dir()},
        "deployment": {
            "selected_commit": scalar(release, "application_commit", COMMIT),
            "updater_commit": scalar(update, "commit", COMMIT),
            "updater_status": choice(update, "status", "current", "waiting_for_ci", "ready",
                                     "installing", "deployed", "superseded", "failed", "failed_commit_skipped"),
            "checked_at": scalar(update, "checked_at", TIME),
        },
    }


def print_server_command(run_id):
    source = Path(__file__).read_text(encoding="utf-8")
    marker = "LAB_TERMINAL_DIAGNOSTICS_PY"
    if marker in source.splitlines():
        raise ValueError("Here-document marker collides with source")
    run_arg = f" --run-id {run_id}" if run_id else ""
    print(f"python3 - server{run_arg} <<'{marker}'\n{source.rstrip()}\n{marker}")


def base_url(value):
    try:
        parsed = urlsplit(value)
        # Validate the port, including malformed or out-of-range values.
        _ = parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError("Expected an HTTPS origin or loopback HTTP origin") from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or any(character.isspace() for character in value)
            or (parsed.scheme != "https" and not
                (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise argparse.ArgumentTypeError("Expected an HTTPS origin or loopback HTTP origin")
    return value.rstrip("/")


def run_identifier(value):
    if not RUN_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("Expected a chat run ID of 32 lowercase hexadecimal characters")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    check = modes.add_parser("check", help="Check the campus HTTPS site or a local test instance")
    check.add_argument("--url", type=base_url, default=SITE)
    check.add_argument("--ca", help="Path to the verified lab root certificate, if it is not trusted by the OS")
    for mode, help_text in (
        ("server-command", "Print a command for an already authenticated Linux terminal"),
        ("server", "Collect local status from the Linux lab server"),
    ):
        modes.add_parser(mode, help=help_text)
    for mode in modes.choices.values():
        mode.add_argument("--run-id", type=run_identifier, help="Include the status of one existing chat run")
    args = parser.parse_args()
    if args.mode == "server-command":
        print_server_command(args.run_id)
        return
    if args.mode == "server":
        if os.name != "posix" or not Path("/run/systemd/system").is_dir():
            parser.error("Run server inside the authorized Linux server session")
        result = server_report(args.run_id)
    else:
        result = web_report(args.url, ca=args.ca, run_id=args.run_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
