"""Guardrail: every CLI argument that takes a one-off payload also accepts ``-`` for stdin.

The write surface never requires a throwaway file (ruling
surface-walk-payload-input-1005). A file-typed argument is either a
:class:`PayloadFile` (which accepts ``-`` and is read through the one shared
reader) or a real artifact named below with the reason it stays a file.
Directory-only paths are not payloads.
"""

from __future__ import annotations

from collections.abc import Iterator

import click
import pytest
from click.testing import CliRunner

from cruxible_core.cli.main import cli
from cruxible_core.cli.payloads import PayloadFile

#: File arguments that are real artifacts, not one-off payloads.
ARTIFACT_FILES: dict[tuple[tuple[str, ...], str], str] = {
    (("authoring", "bind"), "source_path"): "cited workspace file",
    (("block", "sync"), "paths"): "block pages",
    (("block", "detach"), "paths"): "block pages",
    (("credential", "claim-bootstrap"), "secret_file"): "secret material",
    (("server", "start"), "bootstrap_secret_file"): "secret material",
    (("get",), "output_path"): "output written by the command",
    (("kit", "build"), "out"): "output kit directory",
    (("kit", "pull"), "out"): "output kit directory",
    (("sources", "compile"), "output"): "output bundle written by the command",
    (("sources", "propose"), "bundle_path"): "signed source bundle",
    (("provider", "install"), "lock_path"): "provider lock file",
    (("provider", "install"), "dependencies"): "provider wheel files",
    (("principal", "add"), "signer_key"): "private key",
    (("proposal", "approve"), "private_key_path"): "private key",
}


def _commands(
    command: click.Command, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    if isinstance(command, click.Group):
        for name in sorted(command.commands):
            child = command.commands[name]
            loaded = getattr(child, "_load", None)
            yield from _commands(loaded() if callable(loaded) else child, (*path, name))
    else:
        yield path, command


def _file_parameters() -> Iterator[tuple[tuple[str, ...], click.Parameter]]:
    for path, command in _commands(cli):
        for param in command.params:
            kind = param.type
            if isinstance(kind, click.File) or (isinstance(kind, click.Path) and kind.file_okay):
                yield path, param


def test_every_payload_argument_accepts_stdin() -> None:
    offenders = [
        f"{' '.join(path)} {param.name}"
        for path, param in _file_parameters()
        if (path, param.name) not in ARTIFACT_FILES
        and not (isinstance(param.type, PayloadFile) and param.type.allow_dash)
    ]
    assert offenders == [], (
        "a payload argument does not accept - (type it PayloadFile and read it through "
        "cruxible_core.cli.payloads), or name the real artifact it is in ARTIFACT_FILES:\n"
        + "\n".join(offenders)
    )


def test_every_named_artifact_file_still_exists_and_is_not_a_payload() -> None:
    present = {(path, param.name): param for path, param in _file_parameters()}
    stale = sorted(key for key in ARTIFACT_FILES if key not in present)
    assert stale == []
    assert not [key for key in ARTIFACT_FILES if isinstance(present[key].type, PayloadFile)]


@pytest.mark.parametrize(
    "args",
    [
        ["procedure", "run", "nightly", "-", "--at", "-"],
        ["line", "run", "nightly", "--resolution-contract", "-", "--event", "-"],
    ],
)
def test_one_command_reads_stdin_for_one_argument_only(args: list[str]) -> None:
    result = CliRunner().invoke(
        cli,
        ["--server-url", "http://server", "--instance-id", "inst_stdin", *args],
        input="{}\n",
    )
    assert result.exit_code != 0
    assert "only one argument per command can read its payload from - (stdin)" in result.output
