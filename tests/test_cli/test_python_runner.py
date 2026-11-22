"""`cruxible python`: run a script with the CLI's interpreter, or --check it with pyright."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import cruxible_client
import cruxible_core
from cruxible_client.authoring.env_file import ENV_FILE_ENV
from cruxible_client.authoring.world_stub_cache import CachedWorldStub
from cruxible_core.cli.commands import python_runner
from cruxible_core.cli.main import cli
from cruxible_core.cli.principal_settings import write_principal_settings

_HAS_PYRIGHT = shutil.which("pyright") is not None
needs_pyright = pytest.mark.skipif(not _HAS_PYRIGHT, reason="pyright is not installed")

_ENV_PROBE = """\
import os, sys
import cruxible_client
print("argv", sys.argv[1:])
print("client", cruxible_client.__file__)
for name in ("CRUXIBLE_SERVER_SOCKET", "CRUXIBLE_SERVER_URL", "CRUXIBLE_INSTANCE_ID",
             "CRUXIBLE_PRINCIPAL_ID", "CRUXIBLE_ENV_FILE"):
    print(name, os.environ.get(name))
print("token", "set" if os.environ.get("CRUXIBLE_SERVER_BEARER_TOKEN") else "unset")
sys.exit(int(os.environ.get("PROBE_EXIT", "0")))
"""


def _cli(
    args: list[str], *, cwd: Path, stdin: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the CLI from this checkout in a real process: the child shares its stdio."""

    roots = [
        str(Path(module.__file__ or "").resolve().parent.parent)
        for module in (cruxible_core, cruxible_client)
    ]
    environment = dict(os.environ if env is None else env)
    environment["PYTHONPATH"] = os.pathsep.join(roots)
    return subprocess.run(
        [sys.executable, "-c", "from cruxible_core.cli.main import cli; cli()", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _lines(output: str) -> dict[str, str]:
    return dict(line.split(" ", 1) for line in output.splitlines() if " " in line)


def test_a_script_runs_with_the_cli_interpreter_its_args_and_exit_status(tmp_path: Path) -> None:
    script = tmp_path / "probe.py"
    script.write_text(_ENV_PROBE, encoding="utf-8")

    result = _cli(
        [
            "--no-workspace",
            "--server-socket",
            str(tmp_path / "d.sock"),
            "--instance-id",
            "inst_run",
            "python",
            "probe.py",
            "--limit",
            "3",
            "-x",
        ],
        cwd=tmp_path,
        env={**os.environ, "PROBE_EXIT": "7"},
    )

    assert result.returncode == 7, result.stderr
    seen = _lines(result.stdout)
    assert seen["argv"] == "['--limit', '3', '-x']"
    # The SDK the script imports is the CLI's own.
    assert seen["client"] == cruxible_client.__file__
    assert seen["CRUXIBLE_SERVER_SOCKET"] == str((tmp_path / "d.sock").resolve())
    assert seen["CRUXIBLE_SERVER_URL"] == "None"
    assert seen["CRUXIBLE_INSTANCE_ID"] == "inst_run"


def test_dash_c_and_standard_input_run_like_python(tmp_path: Path) -> None:
    inline = _cli(
        ["--no-workspace", "python", "-c", "import sys; print(sys.argv)", "a", "b"], cwd=tmp_path
    )
    assert inline.returncode == 0, inline.stderr
    assert inline.stdout.strip() == "['-c', 'a', 'b']"

    piped = _cli(
        ["--no-workspace", "python", "-", "x"],
        cwd=tmp_path,
        stdin="import sys\nprint('stdin', sys.argv)\n",
    )
    assert piped.returncode == 0, piped.stderr
    assert piped.stdout.strip() == "stdin ['-', 'x']"


def _principal_env_file(tmp_path: Path) -> Path:
    key_dir = tmp_path / "keys" / "reviewer"
    key_dir.mkdir(parents=True)
    key_path = key_dir / "reviewer.ed25519"
    key_path.write_text("dummy-key", encoding="utf-8")
    key_path.chmod(0o600)
    return write_principal_settings(
        key_dir,
        ctx_obj={"server_socket": str(tmp_path / "file.sock")},
        instance_id="inst_envfile",
        principal_id="reviewer",
        private_key_path=key_path,
        token="tok_never_printed",
        written_by="test",
    )


def test_env_file_settings_reach_the_script_by_environment_only(tmp_path: Path) -> None:
    env_file = _principal_env_file(tmp_path)
    script = tmp_path / "probe.py"
    script.write_text(_ENV_PROBE, encoding="utf-8")

    result = _cli(
        ["--no-workspace", "--env-file", str(env_file), "python", "probe.py"], cwd=tmp_path
    )

    assert result.returncode == 0, result.stderr
    seen = _lines(result.stdout)
    assert seen["CRUXIBLE_SERVER_SOCKET"] == str((tmp_path / "file.sock").resolve())
    assert seen["CRUXIBLE_INSTANCE_ID"] == "inst_envfile"
    assert seen["CRUXIBLE_PRINCIPAL_ID"] == "reviewer"
    assert seen["token"] == "set"
    # Already applied: read again in the child it could undo an explicit flag.
    assert seen["CRUXIBLE_ENV_FILE"] == "None"
    assert "tok_never_printed" not in result.stdout + result.stderr

    # CRUXIBLE_ENV_FILE works too; an explicit transport keeps the file's transport,
    # and another principal keeps the file's key and credential, out of the child.
    explicit = _cli(
        [
            "--no-workspace",
            "--server-socket",
            str(tmp_path / "other.sock"),
            "--principal-id",
            "auditor",
            "python",
            "probe.py",
        ],
        cwd=tmp_path,
        env={**os.environ, ENV_FILE_ENV: str(env_file)},
    )
    assert explicit.returncode == 0, explicit.stderr
    seen = _lines(explicit.stdout)
    assert seen["CRUXIBLE_SERVER_SOCKET"] == str((tmp_path / "other.sock").resolve())
    assert seen["CRUXIBLE_PRINCIPAL_ID"] == "auditor"
    assert seen["token"] == "unset"
    assert seen["CRUXIBLE_ENV_FILE"] == "None"


def test_no_script_is_a_usage_error() -> None:
    result = CliRunner().invoke(cli, ["--no-workspace", "python"])
    assert result.exit_code == 2
    assert "needs SCRIPT, -c CODE or -" in result.output


def test_check_without_pyright_refuses_with_the_install_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "s.py"
    script.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(python_runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python"))
    stub_reads: list[Any] = []
    monkeypatch.setattr(python_runner, "_stub_for_check", stub_reads.append)

    result = CliRunner().invoke(cli, ["--no-workspace", "python", "--check", str(script)])

    assert result.exit_code == 1
    assert "cruxible.python.pyright_unavailable" in result.output
    assert "Repair: uv tool install pyright" in result.output
    # Refused before any daemon read.
    assert stub_reads == []


def _real_stub(tmp_path: Path) -> Path:
    """The stub a real World renders for the SDK world tests' vocabulary."""

    from cruxible_client import Cruxible
    from tests.test_client.test_playbill_sdk_world import _workspace, _WorldClient

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _workspace(workspace)
    cx = Cruxible._from_client(  # type: ignore[arg-type]
        _WorldClient(),
        instance_id="inst_world",
        workspace=workspace,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    stub = tmp_path / "stubs" / "world.pyi"
    stub.parent.mkdir()
    stub.write_text(cx.world().stub(), encoding="utf-8")
    return stub


_SCRIPT = """\
from typing import TYPE_CHECKING, cast

from cruxible_client import Cruxible

if TYPE_CHECKING:
    from world import World


def main(cx: Cruxible) -> None:
    w = cast("World", cx.world())
    print(w.sec.vuln.severity.high)
"""


@needs_pyright
def test_check_passes_a_valid_script_and_names_a_bad_field_and_enum_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _real_stub(tmp_path)
    monkeypatch.setattr(
        python_runner,
        "_stub_for_check",
        lambda obj: CachedWorldStub("inst_world", stub, "reused", None),
    )
    good = tmp_path / "good.py"
    good.write_text(_SCRIPT, encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text(
        _SCRIPT
        + "    print(w.sec.vuln.severity.critical)\n    print(w.sec.vulnerability.sevrity)\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    passed = runner.invoke(cli, ["--no-workspace", "python", "--check", str(good)])
    assert passed.exit_code == 0, passed.output
    assert passed.output.splitlines()[0] == f"stub: {stub} (reused)"
    assert passed.output.splitlines()[-1] == "check: ok, files: 1"

    failed = runner.invoke(cli, ["--no-workspace", "python", "--check", str(bad)])
    assert failed.exit_code == 1, failed.output
    assert ':12:31: error: Cannot access attribute "critical"' in failed.output
    assert ':13:31: error: Cannot access attribute "sevrity"' in failed.output
    assert failed.output.splitlines()[-1] == "check: 2 error(s), files: 1"

    # -c and standard input are checked too, and labelled as such.
    inline = runner.invoke(
        cli, ["--no-workspace", "python", "--check", "-c", _SCRIPT + "    w.nope\n"]
    )
    assert inline.exit_code == 1, inline.output
    assert '-c:12:7: error: Cannot access attribute "nope"' in inline.output
    piped = runner.invoke(cli, ["--no-workspace", "python", "--check", "-"], input=_SCRIPT)
    assert piped.exit_code == 0, piped.output


@needs_pyright
def test_check_reads_the_stub_through_the_per_instance_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.authoring import world_stub_cache
    from cruxible_core.cli.commands import _common
    from tests.test_client.test_world_stub_cache import _Client

    rendered = _real_stub(tmp_path).read_text(encoding="utf-8")
    renders: list[str] = []

    def render(client: Any, instance_id: str, *, workspace: Any) -> str:
        renders.append(instance_id)
        return rendered

    monkeypatch.setattr(world_stub_cache, "render_world_stub_for", render)
    monkeypatch.setattr(_common, "_get_client", _Client)
    script = tmp_path / "good.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    args = ["--no-workspace", "--instance-id", "inst_world", "python", "--check", str(script)]

    first = CliRunner().invoke(cli, args)
    second = CliRunner().invoke(cli, args)

    cache = world_stub_cache.world_stub_cache_directory("inst_world") / "world.pyi"
    assert first.exit_code == 0, first.output
    assert first.output.splitlines()[0] == f"stub: {cache} (rendered)"
    assert second.output.splitlines()[0] == f"stub: {cache} (reused)"
    assert second.output.splitlines()[-1] == "check: ok, files: 1"
    assert renders == ["inst_world"]
