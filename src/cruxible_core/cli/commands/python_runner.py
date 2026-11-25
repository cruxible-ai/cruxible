"""`cruxible python`: run or type-check a script with Cruxible's own interpreter.

A script that imports `cruxible_client` runs here with the interpreter this CLI
runs on (``sys.executable``), so the SDK it imports is the one installed with
the CLI, from any repository and with nothing installed into that repository's
environment. The connection the CLI resolved (``--env-file`` /
``CRUXIBLE_ENV_FILE``, ``--server-url`` / ``--server-socket``,
``--instance-id``, the acting principal and its key and credential) reaches the
child through its environment only: never on its command line, never printed.

``--check`` type-checks instead of running, with pyright, against the instance's
world stub cached in the per-user Cruxible directory
(`cruxible_client.authoring.world_stub_cache`), imported as
``from world import World``; no pyright configuration is needed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import click

from cruxible_core.cli.commands._common import _root_ctx_obj, ensure_cli_daemon
from cruxible_core.cli.main import handle_errors

_URL_ENV = "CRUXIBLE_SERVER_URL"
_SOCKET_ENV = "CRUXIBLE_SERVER_SOCKET"
_INSTANCE_ENV = "CRUXIBLE_INSTANCE_ID"
_NO_WORKSPACE_ENV = "CRUXIBLE_NO_WORKSPACE"


def _package_roots() -> tuple[str, ...]:
    """The import roots of the CLI's own `cruxible_client` and `cruxible_core`.

    A root that is not a site-packages directory (a source checkout on
    PYTHONPATH) is handed on, so the child imports the same SDK; site-packages
    is already on the interpreter's own path.
    """

    import cruxible_client
    import cruxible_core

    roots: list[str] = []
    for module in (cruxible_client, cruxible_core):
        root = Path(str(module.__file__)).resolve().parent.parent
        if root.name in {"site-packages", "dist-packages"}:
            continue
        if str(root) not in roots:
            roots.append(str(root))
    return tuple(roots)


def child_environment(
    obj: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The child's environment: this process's, with the CLI's resolved connection.

    The env file is already applied to this process's environment (explicit
    flags kept), so ``CRUXIBLE_ENV_FILE`` is dropped: read again in the child it
    could mix in what an explicit flag kept out. Exactly one transport is set.
    """

    from cruxible_client.authoring.env_file import ENV_FILE_ENV
    from cruxible_core.cli.principal_settings import PRINCIPAL_ID_ENV

    env = dict(os.environ if environ is None else environ)
    env.pop(ENV_FILE_ENV, None)
    server_url = obj.get("server_url")
    server_socket = obj.get("server_socket")
    if server_url or server_socket:
        env.pop(_URL_ENV, None)
        env.pop(_SOCKET_ENV, None)
        if server_url:
            env[_URL_ENV] = str(server_url)
        else:
            env[_SOCKET_ENV] = str(server_socket)
    if obj.get("instance_id"):
        env[_INSTANCE_ENV] = str(obj["instance_id"])
    if obj.get("principal_id"):
        env[PRINCIPAL_ID_ENV] = str(obj["principal_id"])
    if obj.get("no_workspace"):
        env[_NO_WORKSPACE_ENV] = "1"
    roots = [root for root in _package_roots() if root]
    existing = [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item]
    merged = roots + [item for item in existing if item not in roots]
    if merged:
        env["PYTHONPATH"] = os.pathsep.join(merged)
    return env


def _exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _run(argv: Sequence[str], env: Mapping[str, str]) -> int:
    process = subprocess.Popen(list(argv), env=dict(env))
    while True:
        try:
            return _exit_code(process.wait())
        except KeyboardInterrupt:
            # The terminal sent SIGINT to the whole process group; the child
            # decides what it means. Wait for it, then exit as it did.
            continue


def _find_pyright() -> str:
    from cruxible_client.contracts.errors import PyrightUnavailable

    beside = Path(sys.executable).parent / "pyright"
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    found = shutil.which("pyright")
    if found is None:
        raise PyrightUnavailable()
    return found


def _stub_for_check(obj: Mapping[str, Any]) -> Any:
    from cruxible_client.authoring.world_stub_cache import cached_world_stub
    from cruxible_core.cli.commands.playbill import _server_call
    from cruxible_core.cli.context import normalized_transport

    def read(client: Any, instance_id: str) -> Any:
        transport = normalized_transport(obj.get("server_url"), obj.get("server_socket"))
        return cached_world_stub(client, instance_id, transport=transport)

    return _server_call(read, command_name="cruxible python --check")


def _display(path: str, labels: Mapping[str, str]) -> str:
    if path in labels:
        return labels[path]
    try:
        return str(Path(path).relative_to(Path.cwd()))
    except ValueError:
        return path


def _check(targets: Sequence[Path], labels: Mapping[str, str], scratch: Path) -> int:
    pyright = _find_pyright()
    stub = _stub_for_check(_root_ctx_obj())
    script_dirs = []
    for target in targets:
        parent = str(target.parent)
        if parent not in script_dirs and parent not in labels:
            script_dirs.append(parent)
    config = scratch / "pyrightconfig.json"
    config.write_text(
        json.dumps(
            {
                "extraPaths": [str(stub.directory), *script_dirs, *_package_roots()],
                "typeCheckingMode": "basic",
                # `world` is a stub-only module by design.
                "reportMissingModuleSource": "none",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            pyright,
            "--outputjson",
            "-p",
            str(config),
            "--pythonpath",
            sys.executable,
            *(str(target) for target in targets),
        ],
        capture_output=True,
        text=True,
        env=child_environment(_root_ctx_obj()),
        check=False,
    )
    try:
        report = json.loads(completed.stdout)
    except ValueError:
        # Not a report: pyright itself failed. Say what it said.
        click.echo((completed.stderr or completed.stdout).strip(), err=True)
        return completed.returncode or 2
    click.echo(f"stub: {stub.path} ({stub.status})")
    errors = 0
    for item in report.get("generalDiagnostics", ()):
        severity = str(item.get("severity", "error"))
        if severity == "error":
            errors += 1
        start = item.get("range", {}).get("start", {})
        rule = item.get("rule")
        message = " ".join(str(item.get("message", "")).split())
        click.echo(
            f"{_display(str(item.get('file', '')), labels)}:{int(start.get('line', 0)) + 1}:"
            f"{int(start.get('character', 0)) + 1}: {severity}: {message}"
            + (f" ({rule})" if rule else "")
        )
    files = int(report.get("summary", {}).get("filesAnalyzed", len(targets)))
    click.echo(f"check: {'ok' if errors == 0 else f'{errors} error(s)'}, files: {files}")
    return 1 if errors else 0


@click.command(
    "python",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("-c", "code", default=None, help="Run (or check) CODE instead of a script file.")
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Type-check with pyright against the instance's world stub instead of running.",
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@handle_errors
def python_cmd(code: str | None, check: bool, args: tuple[str, ...]) -> None:
    """Run a script with Cruxible's interpreter, its connection in the environment.

    The way to use the typed Python SDK from any repository (Go, Rust,
    TypeScript, or Python with its own venv): `import cruxible_client` is the
    SDK installed with this CLI, at its exact version, with nothing installed
    in the repository. `Cruxible.connect()` finds the daemon, instance and
    principal this command resolved (--env-file, --server-url/--server-socket,
    --instance-id, --principal-id); nothing secret is printed or put on a
    command line. Options for the script go after SCRIPT. The exit status is
    the script's.

    \b
    cruxible python script.py [ARGS]...
    cruxible python -c 'from cruxible_client import Cruxible; print(Cruxible.connect().whoami())'
    cruxible python - <<'PY'
    from cruxible_client import Cruxible
    with Cruxible.connect() as cx:
        w = cx.world()
    PY

    .cruxible/world.pyi types the instance's vocabulary (its kinds, fields and
    enums) for an editor or pyright: floor delivery keeps it current, and
    `cruxible stub --out .cruxible/world.pyi` writes it. --check type-checks
    the script (or CODE, or standard input) with pyright against this
    instance's world stub, cached per instance in the per-user Cruxible
    directory and refreshed when the vocabulary moves; import it as `from
    world import World` under TYPE_CHECKING. Needs pyright on PATH.
    """

    obj = _root_ctx_obj()
    if code is None and not args:
        raise click.UsageError("cruxible python needs SCRIPT, -c CODE or - (standard input)")
    if check:
        with tempfile.TemporaryDirectory(prefix="cruxible-check-") as raw:
            scratch = Path(raw)
            labels: dict[str, str] = {}
            targets: list[Path] = []
            inline: list[tuple[str, str]] = []
            if code is not None:
                inline.append(("-c", code))
            paths = list(args)
            if "-" in paths:
                paths.remove("-")
                inline.append(("-", sys.stdin.read()))
            for label, source in inline:
                target = scratch / f"{'inline' if label == '-c' else 'stdin'}.py"
                target.write_text(source, encoding="utf-8")
                labels[str(target)] = label
                labels[str(target.resolve())] = label
                targets.append(target.resolve())
            for raw_path in paths:
                target = Path(raw_path).expanduser().resolve()
                if not target.is_file():
                    raise click.UsageError(f"no such script: {raw_path}")
                targets.append(target)
            status = _check(targets, labels, scratch)
        sys.exit(status)
    if not obj.get("server_url") and not obj.get("no_autostart"):
        # The script's Cruxible.connect() finds the daemon a CLI command would.
        ensure_cli_daemon()
    argv = [sys.executable, *(["-c", code] if code is not None else []), *args]
    status = _run(argv, child_environment(obj))
    sys.exit(status)
