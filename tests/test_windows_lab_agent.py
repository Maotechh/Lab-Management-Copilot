import base64
import importlib.util
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/lab-agent-windows/windows_lab_agent.py"
SPEC = importlib.util.spec_from_file_location("windows_lab_agent", SCRIPT)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


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
        def invoke(*arguments, input_text=None):
            return subprocess.run(
                [sys.executable, str(SCRIPT), *arguments],
                input=input_text, capture_output=True, encoding="utf-8", timeout=35,
            )

        try:
            deadline = time.monotonic() + 20
            while True:
                check = invoke("check")
                if check.returncode == 0:
                    break
                assert time.monotonic() < deadline, check.stderr
                time.sleep(0.2)
            assert f"PID {console.pid}" in check.stdout
            result = invoke("run", "--timeout", "8", "printf fixture")
            assert result.returncode == 17, (result.stdout, result.stderr)
            assert result.stdout.splitlines() == ["\u4e2d\u6587", "fixture stderr"]
            piped = invoke("run", "--stdin", input_text="printf fixture\n")
            assert piped.returncode == 17, piped.stderr
            rejected = invoke("--pid", str(os.getpid()), "check")
            assert rejected.returncode == 125
        finally:
            console.terminate()
