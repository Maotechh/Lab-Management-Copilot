import base64
import importlib.util
import os
import re
import socket
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/lab-agent-windows/windows_lab_agent.py"
SPEC = importlib.util.spec_from_file_location("windows_lab_agent", SCRIPT)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


def invoke(*arguments, input_text=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        input=input_text, capture_output=True, encoding="utf-8", timeout=35,
    )


@pytest.mark.skipif(os.name == "nt", reason="The shell framing test uses Bash")
def test_command_framing_preserves_quotes_unicode_and_exit_status():
    command = "printf '%s\\n' \"\u4e2d\u6587\"; printf '%s\\n' \"it's a test\" >&2\nexit 17"
    line = agent.command_frame(command, "test123")
    result = subprocess.run(
        ["bash", "-c", f"hostname() {{ printf '%s\\n' '{agent.HOST}'; }}\n{line}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True,
    )
    assert agent.completed_output(result.stdout.splitlines(), "test123") == (
        "\u4e2d\u6587\nit's a test\n", 17,
    )


def test_console_lines_preserve_output_wrapped_by_terminal():
    output = "  \u4e2d\u6587  \n" + "long diagnostic output " * 40 + "\n\n"
    encoded = base64.b64encode(output.encode()).decode()
    rows = ["LAB:test123:BEGIN", *textwrap.wrap(encoded, 37), "LAB:test123:END:17"]
    assert agent.completed_output(rows, "test123") == (output, 17)
    assert agent.completed_output(rows[:-1], "test123") is None
    with pytest.raises(agent.ConsoleError, match="exceeded the available terminal buffer"):
        agent.completed_output(rows[1:], "test123")
    with pytest.raises(agent.ConsoleError, match="incomplete or altered"):
        agent.completed_output([rows[0], "*", rows[-1]], "test123")


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows console APIs")
def test_open_powershell_console_receives_command_and_returns_result():
    fixture = Path(__file__).with_name("windows_lab_console_fixture.py")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = dict(os.environ, LAB_TEST_PYTHON=sys.executable, LAB_TEST_FIXTURE=str(fixture))
    with subprocess.Popen(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command",
         "& $env:LAB_TEST_PYTHON $env:LAB_TEST_FIXTURE"],
        env=env, creationflags=subprocess.CREATE_NEW_CONSOLE,
    ) as console:
        try:
            deadline = time.monotonic() + 20
            while True:
                check = invoke("check")
                if check.returncode == 0:
                    break
                assert time.monotonic() < deadline, check.stderr
                time.sleep(0.2)
            assert f"PID {console.pid}" in check.stdout
            diagnosis = invoke("check", "--diagnose")
            assert diagnosis.returncode == 0, diagnosis.stderr
            assert f"powershell.exe (PID {console.pid}, parent {os.getpid()}): ready at hxsy-zngl" in diagnosis.stderr
            result = invoke("run", "--timeout", "8", "printf fixture")
            assert result.returncode == 17, (result.stdout, result.stderr)
            assert result.stdout.splitlines() == ["\u4e2d\u6587", "fixture stderr"]
            piped = invoke("run", "--stdin", input_text="printf fixture\n")
            assert piped.returncode == 17, piped.stderr
            with subprocess.Popen(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 30"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            ) as unrelated:
                try:
                    rejected = invoke("--pid", str(unrelated.pid), "check")
                    assert rejected.returncode == 125
                    assert "No idle hxsy-zngl prompt found" in rejected.stderr
                finally:
                    unrelated.terminate()
        finally:
            console.terminate()


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows console APIs")
def test_explicit_pid_without_console_reports_windows_error_six():
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=subprocess.DETACHED_PROCESS,
    ) as detached:
        try:
            result = invoke("--pid", str(detached.pid), "check")
            assert result.returncode == 125
            assert f"Cannot attach to console PID {detached.pid} (Windows error 6)" in result.stderr
            assert "lab-agent check --diagnose without --pid" in result.stderr
        finally:
            detached.terminate()


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows console APIs")
def test_detached_launcher_pid_finds_child_console():
    fixture = Path(__file__).with_name("windows_lab_console_fixture.py")
    launch = (
        "import subprocess, sys; "
        "with_console = subprocess.Popen(sys.argv[1:], creationflags=subprocess.CREATE_NEW_CONSOLE); "
        "with_console.wait(timeout=45)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", launch, sys.executable, str(fixture)],
        creationflags=subprocess.DETACHED_PROCESS,
    ) as launcher:
        try:
            deadline = time.monotonic() + 15
            while True:
                check = invoke("--pid", str(launcher.pid), "check", "--diagnose")
                if check.returncode == 0:
                    break
                assert time.monotonic() < deadline, check.stderr
                time.sleep(0.2)
            assert "Windows error 6" in check.stderr
            assert f"parent {launcher.pid}): ready at hxsy-zngl" in check.stderr
            result = invoke("--pid", str(launcher.pid), "run", "--timeout", "8", "printf fixture")
            assert result.returncode == 17, result.stderr
            assert result.stdout.splitlines() == ["\u4e2d\u6587", "fixture stderr"]
        finally:
            subprocess.run(["taskkill.exe", "/T", "/F", "/PID", str(launcher.pid)], capture_output=True, timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows OpenSSH")
def test_powershell_ssh_console_transmits_commands():
    import paramiko

    shell_ready = Event()

    class TestServer(paramiko.ServerInterface):
        def check_auth_none(self, username):
            return paramiko.AUTH_SUCCESSFUL

        def check_channel_request(self, kind, chanid):
            if kind == "session":
                return paramiko.OPEN_SUCCEEDED
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_pty_request(self, *args):
            return True

        def check_channel_shell_request(self, channel):
            shell_ready.set()
            return True

    system32 = Path(os.environ["SystemRoot"]) / "System32"
    powershell = system32 / "WindowsPowerShell/v1.0/powershell.exe"
    ssh = system32 / "OpenSSH/ssh.exe"
    assert ssh.is_file(), "Windows OpenSSH client is required for this test"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(15)
        env = dict(os.environ, LAB_TEST_SSH=str(ssh), LAB_TEST_PORT=str(listener.getsockname()[1]))
        with subprocess.Popen(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command",
             "& $env:LAB_TEST_SSH -F NUL -tt -o BatchMode=yes -o StrictHostKeyChecking=no "
             "-o UserKnownHostsFile=NUL -o GlobalKnownHostsFile=NUL "
             "-o LogLevel=ERROR -p $env:LAB_TEST_PORT fixture@127.0.0.1"],
            env=env, creationflags=subprocess.CREATE_NEW_CONSOLE,
        ) as console:
            try:
                client, _ = listener.accept()
                with client, paramiko.Transport(client) as transport:
                    transport.add_server_key(paramiko.RSAKey.generate(2048))
                    transport.start_server(server=TestServer())
                    channel = transport.accept(10)
                    assert channel is not None and shell_ready.wait(10)
                    channel.settimeout(10)
                    channel.sendall(b"fixture@hxsy-zngl:~$ ")
                    with ThreadPoolExecutor() as executor:
                        result = executor.submit(invoke, "run", "--timeout", "12", "hostname")
                        command = b""
                        while b"\r" not in command and b"\n" not in command:
                            chunk = channel.recv(4096)
                            assert chunk, "SSH shell closed before receiving the command"
                            command += chunk
                        token = re.search(rb"LAB:%s:BEGIN\\n' ([0-9a-f]{10})", command)
                        assert token and b"/bin/bash -c $'hostname'" in command, command
                        content = base64.b64encode(b"hxsy-zngl\n")
                        channel.sendall(
                            b"\r\nLAB:" + token[1] + b":BEGIN\r\n" + content
                            + b"\r\nLAB:" + token[1] + b":END:0\r\nfixture@hxsy-zngl:~$ "
                        )
                        response = result.result()
                        assert response.returncode == 0, response.stderr
                        assert response.stdout == "hxsy-zngl\n"
            finally:
                console.terminate()
