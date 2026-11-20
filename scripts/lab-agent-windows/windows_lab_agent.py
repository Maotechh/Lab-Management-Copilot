"""Run commands in the already open AccessClient laboratory console."""

import argparse
import base64
import binascii
import csv
import ctypes as c
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import ExitStack
from ctypes import wintypes as w
from pathlib import Path

HOST = "hxsy-zngl"
PROMPT = re.compile(r"[A-Za-z0-9_.-]+@" + HOST + r":[^\r\n]*[#$] ")
CLIENTS = ("ssh.exe", "plink.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "putty.exe", "accessclient.exe")
CAPTURE_ROWS = 240
MISSING_CONSOLE_ERRORS = {6, 87}


class ConsoleError(RuntimeError):
    pass


class ConsoleAttachError(ConsoleError):
    def __init__(self, pid, code):
        self.code = code
        detail = {
            5: "Access denied. Open lab-agent in the same Windows session and at the same elevation as AccessClient.",
            6: "This PID has no attachable console. Try lab-agent check --diagnose without --pid.",
            87: "This PID is no longer available. Open AccessClient and rerun lab-agent check.",
        }.get(code, "Run lab-agent check --diagnose.")
        super().__init__(f"Cannot attach to console PID {pid} (Windows error {code}). {detail}")


class Coord(c.Structure):
    _fields_ = [("x", w.SHORT), ("y", w.SHORT)]


class BufferInfo(c.Structure):
    _fields_ = [
        ("size", Coord), ("cursor", Coord), ("attributes", w.WORD),
        ("window", w.SHORT * 4), ("maximum", Coord),
    ]


class KeyEvent(c.Structure):
    _fields_ = [
        ("down", w.BOOL), ("repeat", w.WORD), ("virtual_key", w.WORD), ("scan", w.WORD),
        ("character", w.WORD), ("modifiers", w.DWORD),
    ]


class InputRecord(c.Structure):
    _fields_ = [("kind", w.WORD), ("key", KeyEvent)]


def api(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


class Console:
    def __init__(self, pid):
        self.pid = pid
        kernel = c.WinDLL("kernel32", use_last_error=True)
        self.attach = api(kernel, "AttachConsole", w.BOOL, w.DWORD)
        self.detach = api(kernel, "FreeConsole", w.BOOL)
        self.open = api(
            kernel, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD,
            c.c_void_p, w.DWORD, w.DWORD, w.HANDLE,
        )
        self.close = api(kernel, "CloseHandle", w.BOOL, w.HANDLE)
        self.processes = api(kernel, "GetConsoleProcessList", w.DWORD, c.POINTER(w.DWORD), w.DWORD)
        self.info = api(kernel, "GetConsoleScreenBufferInfo", w.BOOL, w.HANDLE, c.POINTER(BufferInfo))
        self.read = api(
            kernel, "ReadConsoleOutputCharacterW", w.BOOL, w.HANDLE, w.LPWSTR,
            w.DWORD, Coord, c.POINTER(w.DWORD),
        )
        self.write = api(
            kernel, "WriteConsoleInputW", w.BOOL, w.HANDLE, c.POINTER(InputRecord),
            w.DWORD, c.POINTER(w.DWORD),
        )
        self.pending = api(kernel, "GetNumberOfConsoleInputEvents", w.BOOL, w.HANDLE, c.POINTER(w.DWORD))
        self.peek = api(
            kernel, "PeekConsoleInputW", w.BOOL, w.HANDLE, c.POINTER(InputRecord),
            w.DWORD, c.POINTER(w.DWORD),
        )
        self.handles = []

    def __enter__(self):
        # Only the worker detaches. Its standard streams are pipes back to the CLI.
        self.detach()
        if not self.attach(self.pid):
            raise ConsoleAttachError(self.pid, c.get_last_error())
        try:
            for name, access in (("CONIN$", 0xC0000000), ("CONOUT$", 0x80000000)):
                handle = self.open(name, access, 3, None, 3, 0, None)
                if handle == c.c_void_p(-1).value:
                    raise ConsoleError(f"Cannot open {name} (Windows error {c.get_last_error()}).")
                self.handles.append(handle)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        for handle in self.handles:
            self.close(handle)
        self.handles.clear()
        self.detach()

    def members(self):
        pids = (w.DWORD * 64)()
        count = self.processes(pids, len(pids))
        if count > len(pids):
            pids = (w.DWORD * count)()
            count = self.processes(pids, len(pids))
        return pids[:min(count, len(pids))]

    def screen(self):
        info = BufferInfo()
        output = self.handles[1]
        if not self.info(output, c.byref(info)):
            raise ConsoleError("Unable to read the selected terminal.")
        width, x, y = info.size.x, info.cursor.x, info.cursor.y
        if width < 32 or x < 0 or y < 0:
            raise ConsoleError("The terminal buffer needs to be at least 32 columns wide.")
        first = max(0, y - CAPTURE_ROWS)
        cells = (y - first + 1) * width
        text = c.create_unicode_buffer(cells)
        count = w.DWORD()
        if not self.read(output, text, cells, Coord(0, first), c.byref(count)):
            raise ConsoleError("Unable to read the selected terminal text.")
        raw = c.string_at(c.addressof(text), count.value * 2)
        row_bytes = width * 2
        current_offset = (y - first) * row_bytes
        rows = [
            raw[offset:offset + row_bytes].decode("utf-16-le", errors="replace").rstrip()
            for offset in range(0, current_offset, row_bytes)
        ]
        current = raw[current_offset:current_offset + x * 2].decode("utf-16-le", errors="replace")
        return rows, current

    def send(self, line):
        count = w.DWORD()
        if not self.pending(self.handles[0], c.byref(count)):
            raise ConsoleError("Unable to inspect terminal input.")
        events = (InputRecord * min(count.value, 256))()
        read = w.DWORD()
        if count.value and (
            not self.peek(self.handles[0], events, len(events), c.byref(read))
            or count.value > len(events)
            or any(event.kind == 1 and event.key.down for event in events[:read.value])
        ):
            raise ConsoleError("The AccessClient terminal has input waiting. Leave its prompt idle and retry.")
        keys = [InputRecord(1, KeyEvent(1, 1, 0xE7, 0, ord(char), 0)) for char in line]
        keys.append(InputRecord(1, KeyEvent(1, 1, 0x0D, 0x1C, 13, 0)))
        events = (InputRecord * len(keys))(*keys)
        written = w.DWORD()
        if not self.write(self.handles[0], events, len(events), c.byref(written)) or written.value != len(events):
            raise ConsoleError("Terminal input was interrupted. Check the AccessClient window before retrying.")


def find_console(preferred, diagnose=False):
    # Process names suffice. The AccessClient command line may contain a login ticket.
    if preferred is None:
        tasklist = Path(os.environ["SystemRoot"]) / "System32/tasklist.exe"
        listing = subprocess.check_output(
            [str(tasklist), "/FO", "CSV", "/NH"], encoding="mbcs", errors="replace", timeout=8,
        )
        candidates = [
            (row[0].lower(), int(row[1])) for row in csv.reader(listing.splitlines())
            if len(row) >= 2 and row[0].lower() in CLIENTS and row[1].isdigit()
        ]
        candidates.sort(key=lambda candidate: CLIENTS.index(candidate[0]))
    else:
        candidates = [("Selected process", preferred)]
    seen = set()
    available = []
    for name, pid in candidates:
        if pid in seen:
            continue
        try:
            with Console(pid) as terminal:
                members = terminal.members()
                seen.update(members)
                _, current = terminal.screen()
                ready = PROMPT.fullmatch(current) is not None
                if ready:
                    others = [member for member in members if member not in {pid, os.getpid()}]
                    available.append([pid, *others])
                detail = f"ready at {HOST}" if ready else f"no idle {HOST} prompt"
        except ConsoleError as error:
            if preferred is not None:
                raise
            detail = str(error)
        if diagnose:
            print(f"{name} (PID {pid}): {detail}", file=sys.stderr)
    if not available:
        raise ConsoleError(
            f"No idle {HOST} prompt found. Open the lab in AccessClient and wait for the Linux prompt. "
            "Run lab-agent check --diagnose for console details. If AccessClient opens in Windows Terminal, "
            "try Windows Console Host as the default terminal application and reconnect."
        )
    if len(available) > 1:
        raise ConsoleError("Multiple laboratory consoles found. Close the extra ones or select --pid: "
                           + ", ".join(str(pids[0]) for pids in available))
    return available[0]


def bash_quote(command):
    parts = []
    for character in command:
        if character in "\\'":
            parts.append("\\" + character)
        elif " " <= character <= "~":
            parts.append(character)
        elif ord(character) <= 127:
            parts.append(f"\\x{ord(character):02x}")
        else:
            parts.extend(f"\\x{byte:02x}" for byte in character.encode("utf-8"))
    return "$'" + "".join(parts) + "'"


def command_frame(command, token):
    if not command.strip() or "\0" in command:
        raise ConsoleError("Provide a nonempty command without null characters.")
    # Base64 preserves whitespace when the Windows console wraps long lines.
    line = (
        f"""( if [ "$(hostname -s)" = {HOST} ]; then """
        f"""printf '\\nLAB:%s:BEGIN\\n' {token}; """
        f"""/bin/bash -c {bash_quote(command)} </dev/null 2>&1 | base64; """
        """status=("${PIPESTATUS[@]}"); """
        """if [ "${status[1]}" -ne 0 ]; then status[0]=ERROR; fi; """
        f"""printf '\\nLAB:%s:END:%s\\n' {token} "${{status[0]}}"; fi )"""
    )
    if len(line) > 2000:
        raise ConsoleError("Command is too long for the terminal. Run a shorter command.")
    return line


def completed_output(rows, token):
    start = f"LAB:{token}:BEGIN"
    end = re.compile(r"LAB:" + token + r":END:([0-9]{1,3}|ERROR)")
    first = None
    for index, line in enumerate(rows):
        if line == start:
            first = index
        match = end.fullmatch(line)
        if match:
            if match[1] == "ERROR":
                raise ConsoleError("The remote command ran, but its output could not be encoded.")
            if first is None:
                raise ConsoleError(
                    "Remote output exceeded the available terminal buffer. "
                    "Request fewer lines, for example with journalctl -n 10."
                )
            try:
                output = base64.b64decode("".join(rows[first + 1:index]), validate=True)
            except binascii.Error as exc:
                raise ConsoleError("Remote output was incomplete or altered in the AccessClient console.") from exc
            return output.decode("utf-8", errors="replace"), int(match[1])
    return None


def execute(pids, command, timeout):
    token = secrets.token_hex(5)
    line = command_frame(command, token)
    with ExitStack() as console:
        for pid in pids:
            try:
                terminal = console.enter_context(Console(pid))
                break
            except ConsoleAttachError as error:
                if error.code not in MISSING_CONSOLE_ERRORS or pid == pids[-1]:
                    raise
        _, current = terminal.screen()
        if not PROMPT.fullmatch(current):
            raise ConsoleError(f"The selected console is no longer at the {HOST} prompt.")
        terminal.send(line)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows, current = terminal.screen()
            if PROMPT.fullmatch(current):
                result = completed_output(rows, token)
                if result is not None:
                    output, status = result
                    sys.stdout.write(output)
                    return status
            time.sleep(0.12)
    raise ConsoleError("No completed result before timeout. Check the AccessClient console before retrying.")


def worker(args):
    import msvcrt

    path = Path(os.environ["LOCALAPPDATA"]) / "LabAgent"
    path.mkdir(exist_ok=True)
    with (path / "console.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ConsoleError("Another lab-agent command is running. Wait for it to finish.") from exc
        pids = find_console(args.pid, diagnose=getattr(args, "diagnose", False))
        if args.mode == "check":
            print(f"AccessClient console ready: {HOST} (PID {pids[0]})")
            return 0
        return execute(pids, args.command, args.timeout)


def main(arguments):
    parser = argparse.ArgumentParser(prog="lab-agent", description=__doc__)
    parser.add_argument("--pid", type=int, help="select a console process if more than one lab is open")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    modes = parser.add_subparsers(dest="mode")
    check = modes.add_parser("check", help="find an open AccessClient session")
    check.add_argument("--diagnose", action="store_true", help="show candidate processes and Windows errors")
    run = modes.add_parser("run", help="run one Bash command")
    run.add_argument("--timeout", type=int, default=45)
    run.add_argument("--stdin", action="store_true", help="read the UTF-8 command from standard input")
    run.add_argument("command", nargs="?")
    args = parser.parse_args(arguments)
    if os.name != "nt":
        parser.error("Run lab-agent in native Windows PowerShell.")
    if args.mode == "run":
        if not 1 <= args.timeout <= 600:
            parser.error("--timeout must be between 1 and 600 seconds.")
        if args.stdin == (args.command is not None):
            parser.error("Provide a command or use --stdin.")
        if args.stdin:
            args.command = sys.stdin.buffer.read(8001).decode("utf-8-sig")
    if args.worker:
        return worker(args)

    def invoke(values):
        # The worker can attach to the other console while the CLI keeps its
        # original console and streams. Wait before returning the worker code.
        invocation = [sys.executable, str(Path(__file__).resolve()), "--worker"]
        if args.pid is not None:
            invocation += ["--pid", str(args.pid)]
        hang_limit = args.timeout if args.mode == "run" else 45
        result = subprocess.run(
            [*invocation, *values], stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=hang_limit + 20,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode

    if args.mode == "run":
        return invoke(["run", "--timeout", str(args.timeout), "--", args.command])
    if args.mode == "check":
        return invoke(["check", "--diagnose"] if args.diagnose else ["check"])
    if invoke(["check"]):
        return 125
    if not sys.stdin.isatty():
        for line in sys.stdin:
            if line.strip() and line.strip() not in {"exit", "quit"}:
                status = invoke(["run", "--", line.rstrip("\r\n")])
                if status:
                    return status
            elif line.strip():
                break
        return 0
    while True:
        try:
            line = input(f"{HOST}> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.strip() in {"exit", "quit"}:
            return 0
        if line.strip():
            invoke(["run", "--", line])


def entrypoint():
    try:
        if os.name == "nt":
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        return main(sys.argv[1:])
    except (OSError, ConsoleError, subprocess.SubprocessError, ValueError) as error:
        print(f"lab-agent: {error}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(entrypoint())
