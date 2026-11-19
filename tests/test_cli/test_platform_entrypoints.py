"""Native Windows is refused by the console-script launchers before any import fails.

The CLI and MCP server import POSIX-only modules (``fcntl``, ``pwd``, ``grp``)
at module load. Each case runs in a fresh interpreter with ``sys.platform``
set to ``win32`` and checks the exit code, the stderr message, an empty
stdout (the MCP stdio protocol owns it), and that no project module beyond
the launcher was imported.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cruxible_core import entrypoints

REPO_ROOT = Path(__file__).resolve().parents[2]

_CALLS = {
    "cruxible": "from cruxible_core.entrypoints import cli; cli()",
    "cruxible-mcp": "from cruxible_core.entrypoints import mcp; mcp()",
    "python -m cruxible_core.mcp": (
        "import runpy; runpy.run_module('cruxible_core.mcp', run_name='__main__')"
    ),
}

# Package __init__ modules that hold no runtime imports, plus the launcher itself.
_ALLOWED_PROJECT_MODULES = {
    "cruxible_core",
    "cruxible_core.entrypoints",
    "cruxible_core.mcp",
    "cruxible_core.mcp.__main__",
}


def _run_as_windows(call: str) -> subprocess.CompletedProcess[str]:
    script = (
        "import json, sys\n"
        "sys.platform = 'win32'\n"
        "code = None\n"
        "try:\n"
        f"    {call}\n"
        "except SystemExit as exc:\n"
        "    code = exc.code\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('cruxible_core', 'cruxible_client'))\n"
        "sys.__stderr__.write('\\n' + json.dumps({'code': code, 'loaded': loaded}))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize("entry", sorted(_CALLS))
def test_windows_is_refused_before_posix_only_imports(entry: str) -> None:
    completed = _run_as_windows(_CALLS[entry])

    message, _, report_line = completed.stderr.rpartition("\n")
    report = json.loads(report_line)
    assert report["code"] == 1
    assert message.strip() == entrypoints.WINDOWS_UNSUPPORTED_MESSAGE
    assert completed.stdout == ""
    assert set(report["loaded"]) <= _ALLOWED_PROJECT_MODULES


def test_message_names_wsl2_and_links_the_supported_platforms_section() -> None:
    assert entrypoints.WINDOWS_UNSUPPORTED_MESSAGE.startswith(
        "Cruxible does not run natively on Windows yet. Install and run it inside WSL2: "
    )
    assert entrypoints.SUPPORTED_PLATFORMS_URL.endswith("docs/quickstart.md#supported-platforms")
    quickstart = (REPO_ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
    assert "\n## Supported platforms\n" in quickstart


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_supported_platforms_pass_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    entrypoints.require_supported_platform()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cli_launcher_runs_the_cli_on_supported_platforms() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['cruxible', '--version']\n"
            "from cruxible_core.entrypoints import cli; cli()",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert "cruxible" in completed.stdout.lower()
