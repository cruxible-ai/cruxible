"""Every public ``Cruxible``, ``World`` and ``ChangeSetDraft`` member has a reference entry.

The SDK reference (``packages/cruxible-client/README.md``) is where an agent
looks a call up before it reads ``sdk.py``. Nothing compared it with the
public SDK: the docstring guardrail (``test_playbill_sdk_discoverability``)
checks that each member names its next call, and the old exhaustive reference
guards covered the CLI and MCP only. So ``Cruxible.note``,
``Cruxible.backlinks``, ``Cruxible.section`` and ``ChangeSetDraft.subject_kind``
shipped with no entry. Here a member's entry is a heading that names it as
``Class.member`` in backticks (one heading may name several).
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from cruxible_client.authoring import sdk, world

README = Path(__file__).resolve().parents[2] / "packages" / "cruxible-client" / "README.md"

#: The classes an SDK user writes against; each public member needs an entry.
_REFERENCED: tuple[type, ...] = (sdk.Cruxible, world.World, sdk.ChangeSetDraft)

_HEADING = re.compile(r"^#{2,4}\s+(?P<text>.+?)\s*$", re.MULTILINE)
_NAME = re.compile(r"`([A-Za-z_][\w.]*)`")


def _public_members(cls: type) -> Iterator[str]:
    """Public methods and properties the class defines or inherits from SDK code."""

    for name in sorted(dir(cls)):
        if name.startswith("_"):
            continue
        attribute = inspect.getattr_static(cls, name)
        if isinstance(attribute, property):
            target: object = attribute.fget
        elif isinstance(attribute, (staticmethod, classmethod)):
            target = attribute.__func__
        elif inspect.isfunction(attribute):
            target = attribute
        else:
            continue
        if (getattr(target, "__module__", "") or "").startswith("cruxible_client."):
            yield name


def _referenced_names(text: str) -> set[str]:
    return {
        name for heading in _HEADING.finditer(text) for name in _NAME.findall(heading["text"])
    }


@pytest.mark.parametrize("cls", _REFERENCED, ids=lambda cls: cls.__name__)
def test_every_public_member_has_a_reference_entry(cls: type) -> None:
    documented = _referenced_names(README.read_text(encoding="utf-8"))
    missing = [
        f"{cls.__name__}.{name}"
        for name in _public_members(cls)
        if f"{cls.__name__}.{name}" not in documented
    ]
    assert not missing, (
        "public SDK members with no heading in packages/cruxible-client/README.md "
        "(add a `### \\`Class.member\\`` entry):\n" + "\n".join(missing)
    )


def test_the_rule_reads_headings_not_prose() -> None:
    text = "### `Cruxible.set`\nUse `Cruxible.note` for prose.\n## `World.kinds` and `World.kind`\n"

    assert _referenced_names(text) == {"Cruxible.set", "World.kinds", "World.kind"}


def test_no_entry_names_a_member_that_is_gone() -> None:
    documented = _referenced_names(README.read_text(encoding="utf-8"))
    stale = sorted(
        name
        for cls in _REFERENCED
        for name in documented
        if name.startswith(f"{cls.__name__}.")
        and not hasattr(cls, name.split(".", 1)[1])
    )
    assert not stale, stale
