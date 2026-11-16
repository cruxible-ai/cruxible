"""The operands a suggested command leaves for the caller, and the check that refuses them.

A repair or next line names everything its command needs; an operand only the
caller holds (why, which signer, which file) is shown as a placeholder such as
``<TEXT>`` or ``<SIGNER_ID>``. A placeholder must never be accepted as the
operand itself, so a line run unchanged refuses instead of acting:

- free text (``because``, ``reason``) is checked here, once, by every request
  model that takes it (``NotPlaceholder``);
- an identifier, digest, choice or path placeholder fails the field's own
  format, lookup or file read (``<CLAIM_ID>`` is no Claim ID, ``<DIR>`` no
  directory).

The CLI quotes each placeholder (``'<TEXT>'``), so the shell passes it through
and this boundary is what refuses it.
"""

from __future__ import annotations

import re
from typing import Annotated, TypeAlias

from pydantic import AfterValidator

from cruxible_client._error_base import CoreError

#: ``<NAME>`` or ``<NAME#PART>``: upper case, digits and underscores.
PLACEHOLDER_PATTERN = re.compile(r"<[A-Z][A-Z0-9_]*(?:#[A-Z][A-Z0-9_]*)?>")


def placeholder(name: str) -> str:
    """The placeholder a suggested command shows for one operand the caller supplies."""

    return f"<{name.upper()}>"


def is_placeholder(value: object) -> bool:
    return isinstance(value, str) and PLACEHOLDER_PATTERN.fullmatch(value.strip()) is not None


def refuse_placeholder(value: str) -> str:
    """Refuse a suggested command's placeholder sent as the text it stands for."""

    if is_placeholder(value):
        raise ValueError(
            f"{value.strip()} is a placeholder a suggested command left for you to fill in: "
            "replace it with your own words"
        )
    return value


class PlaceholderRefused(CoreError):
    """A suggested command's placeholder reached a client in place of a value."""

    error_code = "cruxible.request.placeholder_unreplaced"

    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(
            f"{self.error_code}: {value} is a placeholder a suggested command left for you to "
            "fill in; replace it with the value it names, then run the command again"
        )


def refuse_placeholder_operands(*values: object) -> None:
    """Refuse an operand a client consumes itself (a page, a block) that is a placeholder.

    A path is checked as written and by its last component, since a client may
    resolve it against the workspace first.
    """

    for value in values:
        items = value if isinstance(value, (list, tuple)) else (value,)
        for item in items:
            text = str(item)
            for candidate in (text, text.rsplit("/", 1)[-1]):
                if is_placeholder(candidate):
                    raise PlaceholderRefused(candidate.strip())


#: Free text a caller writes (why, a reason), never a suggestion's placeholder.
NotPlaceholder = AfterValidator(refuse_placeholder)
CallerText: TypeAlias = Annotated[str, NotPlaceholder]


__all__ = [
    "PLACEHOLDER_PATTERN",
    "CallerText",
    "NotPlaceholder",
    "PlaceholderRefused",
    "is_placeholder",
    "placeholder",
    "refuse_placeholder",
    "refuse_placeholder_operands",
]
