"""The one reader for every one-off payload a CLI argument takes: a file, or ``-`` for stdin.

The write surface never requires a throwaway file. Every argument that takes a
one-off structured payload (a request, a spec, an input, a write file, an access
profile, a cursor) is typed :class:`PayloadFile`, which accepts ``-``, and is
read through this module, so a heredoc or a pipe works and nothing lands on
disk. Real artifacts stay files: Procedure source, signed source bundles, kit
and provider lock files, key directories, cited workspace files and block pages
(the CLI guardrail names each one and why).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar, cast

import click
import yaml
from pydantic import ValidationError

from cruxible_client.errors import DataValidationError

STDIN = "-"

ModelT = TypeVar("ModelT")

_STDIN_CLAIMED = "_payload_stdin_claimed"


class PayloadFile(click.Path):
    """A one-off payload argument: a file path, or ``-`` to read it from stdin."""

    name = "payload"

    def __init__(self) -> None:
        super().__init__(exists=True, dir_okay=False, allow_dash=True)

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> Any:
        if value == STDIN:
            # Refused while parsing, before any payload is read or sent.
            _claim_stdin(ctx)
        return super().convert(value, param, ctx)

    def get_metavar(self, param: click.Parameter, ctx: click.Context) -> str | None:
        # An option shows that it takes stdin; an argument keeps its own name.
        return "FILE|-" if isinstance(param, click.Option) else None


def _claim_stdin(ctx: click.Context | None) -> None:
    """Refuse a second ``-``: one command has one stdin to read."""

    if ctx is None:
        return
    root = ctx.find_root()
    root.ensure_object(dict)
    if root.obj.get(_STDIN_CLAIMED):
        raise click.UsageError("only one argument per command can read its payload from - (stdin)")
    root.obj[_STDIN_CLAIMED] = True


def payload_label(path: str) -> str:
    """How a refusal names where a payload came from."""

    return "stdin" if path == STDIN else str(Path(path).expanduser())


def read_payload_bytes(path: str) -> bytes:
    """The exact bytes of one payload argument."""

    if path == STDIN:
        return click.get_binary_stream("stdin").read()
    source = Path(path).expanduser()
    try:
        return source.read_bytes()
    except OSError as exc:
        raise click.ClickException(f"Could not read {source}: {exc}") from exc


def read_payload_text(path: str) -> str:
    """One payload argument as UTF-8 text."""

    content = read_payload_bytes(path)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise click.ClickException(f"Could not read {payload_label(path)}: {exc}") from exc


def read_payload_document(path: str) -> object:
    """One payload argument parsed as YAML (JSON is YAML)."""

    text = read_payload_text(path)
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise click.ClickException(f"Could not read {payload_label(path)}: {exc}") from exc


def read_mapping(path: str) -> dict[str, Any]:
    """One payload argument that must hold a single mapping."""

    payload = read_payload_document(path)
    if not isinstance(payload, dict):
        raise click.ClickException(f"{payload_label(path)} must contain one mapping")
    return cast(dict[str, Any], payload)


def model_field_errors(exc: ValidationError) -> list[str]:
    """Render one pydantic failure per line as ``field.path: message``."""

    rendered: list[str] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "invalid"))
        rendered.append(f"{location}: {message}" if location else message)
    return rendered


def read_model(path: str, model: type[ModelT]) -> ModelT:
    """One payload argument validated as ``model``; a malformed one refuses by field."""

    payload = read_mapping(path)
    validator = getattr(model, "model_validate")
    try:
        return cast(ModelT, validator(payload))
    except ValidationError as exc:
        # A malformed request is the caller's mistake, not a crash: carry the
        # field paths so the caller can repair it from the message alone.
        # DataValidationError renders `summary: <errors>` itself, so the summary
        # must not repeat the field list.
        raise DataValidationError(
            f"{payload_label(path)} is not a valid {model.__name__}",
            errors=model_field_errors(exc),
        ) from exc


__all__ = [
    "STDIN",
    "PayloadFile",
    "model_field_errors",
    "payload_label",
    "read_mapping",
    "read_model",
    "read_payload_bytes",
    "read_payload_document",
    "read_payload_text",
]
