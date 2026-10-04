"""One reading of dotted codes across the public rename.

Codes are written ``cruxible.<family>.<name>``. Records written before the
rename carry ``playbill.``, and their bytes never change. Every reader treats
the two spellings as one code:

- a closed code vocabulary (a ``Literal`` field) reads a historical spelling as
  today's (``CurrentCode``), so the served view carries one spelling;
- a union told apart by its code picks its member on the current spelling
  (``current_code_keys``);
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

    Pydantic reads a discriminator before any validator on the union itself, so
    the field holding the members rewrites each member's ``field`` to the
    current spelling first; the served schema keeps its discriminator mapping
    and a historical record still selects its member.
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


__all__ = [
    "CODE_PREFIX",
    "HISTORICAL_CODE_PREFIX",
    "CurrentCode",
    "current_code_keys",
    "normalize_code",
]
