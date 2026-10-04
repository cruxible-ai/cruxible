"""One reading of dotted codes across the public rename.

Codes are written ``cruxible.<family>.<name>``. Records written before the
rename carry ``playbill.``, and their bytes never change. Every reader treats
the two spellings as one code:

- a closed code vocabulary (a ``Literal`` field) reads a historical spelling as
  today's (``CurrentCode``), so the served view carries one spelling;
- a union told apart by its code picks its member on the current spelling
  (``code_told_union``), and so does each member read on its own (``CurrentCode``);
- an open code (a plain ``str`` field inside a record whose bytes are re-verified,
  such as a ledger evaluation note) keeps the spelling it was written with, and
  code-keyed logic compares on ``normalize_code``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BeforeValidator

CODE_PREFIX = "cruxible."
HISTORICAL_CODE_PREFIX = "playbill."


def normalize_code(code: str) -> str:
    """The current spelling of a dotted code; a historical ``playbill.`` code maps over."""

    if code.startswith(HISTORICAL_CODE_PREFIX):
        return CODE_PREFIX + code[len(HISTORICAL_CODE_PREFIX) :]
    return code


def _current(value: object) -> object:
    return normalize_code(value) if isinstance(value, str) else value


#: Annotate a closed code field: ``Annotated[Literal[...], CurrentCode]``.
CurrentCode = BeforeValidator(_current)


def current_code_keys(field: str) -> Callable[[Any, Any], Any]:
    """A before-validator for a sequence of code-told union members.

    Rewrites each member's ``field`` to the current spelling; for a field whose
    union type is not ``code_told_union`` itself.
    """

    def current(cls: Any, value: Any) -> Any:
        if not isinstance(value, list | tuple):
            return value
        return [
            {**item, field: normalize_code(item[field])}
            if isinstance(item, dict) and isinstance(item.get(field), str)
            else item
            for item in value
        ]

    return classmethod(current)  # type: ignore[return-value]


class _CodeToldSchema:
    """Restore the discriminator mapping a callable discriminator drops from the schema."""

    def __init__(self, field: str, codes: tuple[str, ...]) -> None:
        self.field = field
        self.codes = codes

    def __get_pydantic_json_schema__(self, core_schema: Any, handler: Any) -> Any:
        schema = handler(core_schema)
        branches = schema.get("oneOf", ())
        refs = [branch.get("$ref") for branch in branches]
        if len(refs) == len(self.codes) and all(isinstance(ref, str) for ref in refs):
            schema["discriminator"] = {
                "mapping": dict(zip(self.codes, refs, strict=True)),
                "propertyName": self.field,
            }
        return schema


def code_told_union(field: str, members: tuple[tuple[type[Any], str], ...]) -> Any:
    """A union of models told apart by a code ``field``, read on its current spelling.

    Pydantic reads a string discriminator before any validator runs, so a
    record written with a ``playbill.`` code would fail member selection; this
    union selects on ``normalize_code`` instead, and its schema keeps the plain
    ``{propertyName, mapping}`` discriminator the string form would publish.
    Each member's own ``field`` should carry ``CurrentCode`` too.
    """

    from typing import Annotated, Union

    from pydantic import Discriminator, Tag

    def discriminate(value: Any) -> str | None:
        raw = value.get(field) if isinstance(value, dict) else getattr(value, field, None)
        return normalize_code(raw) if isinstance(raw, str) else None

    choices = tuple(Annotated[model, Tag(code)] for model, code in members)
    return Annotated[
        Union[choices],  # noqa: UP007 - built from a runtime tuple
        Discriminator(discriminate),
        _CodeToldSchema(field, tuple(code for _model, code in members)),
    ]


__all__ = [
    "CODE_PREFIX",
    "HISTORICAL_CODE_PREFIX",
    "CurrentCode",
    "code_told_union",
    "current_code_keys",
    "normalize_code",
]
