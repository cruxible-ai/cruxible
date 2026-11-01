"""One typed reference to a value a Procedure run holds.

Authored Procedures name what a step reads with a :class:`Ref`, never a
``"$steps.x.y"`` string: ``{"$ref": "step", "step": "scores", "path":
["records"]}`` reads the ``records`` member of step ``scores``. The roots are
the Procedure ``input``, a Line ``param`` (parameter), an earlier ``step``'s
output, and the current ``item`` inside a ``for_each``.

The stored Procedure graph keeps its reference strings (``$input.<p>``,
``$params.<p>``, ``$steps.<alias>.<p>``, ``$item.<p>``); :meth:`Ref.graph_text`
and :func:`ref_from_graph_text` convert exactly between the two, so a stored
graph decompiles to the typed form and lowers back to the same bytes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RefRoot = Literal["input", "param", "step", "item"]

_MEMBER_RE = re.compile(r"^(?:[a-z][a-z0-9_]{0,127}|[0-9]{1,9}|\[\*\])$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")

#: The graph reference prefix of each root.
GRAPH_PREFIX: dict[str, str] = {
    "input": "$input",
    "param": "$params",
    "step": "$steps",
    "item": "$item",
}
_ROOT_OF_PREFIX = {prefix: root for root, prefix in GRAPH_PREFIX.items()}


class Ref(BaseModel):
    """A typed reference: its ``$ref`` root, the ``step`` it reads, and a member ``path``.

    ``step`` is named exactly when the root is ``step``. ``path`` members are
    field names, list positions as digits, or explicit ``[*]`` projections.
    A ``param`` reference names the parameter as its first path member.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        json_schema_mode_override="validation",
    )

    root: RefRoot = Field(alias="$ref")
    step: str | None = Field(default=None, exclude_if=lambda value: value is None)
    path: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)

    @field_validator("step")
    @classmethod
    def _step(cls, value: str | None) -> str | None:
        if value is not None and not _ALIAS_RE.fullmatch(value):
            raise ValueError(f"reference step {value!r} is not a step id")
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for member in value:
            if not _MEMBER_RE.fullmatch(member):
                raise ValueError(f"reference path member {member!r} is not a name or position")
        return value

    @model_validator(mode="after")
    def _shape(self) -> Ref:
        if (self.root == "step") != (self.step is not None):
            raise ValueError("a reference names its step exactly when its root is 'step'")
        if self.root == "param" and not self.path:
            raise ValueError("a parameter reference names the parameter as its first path member")
        return self

    def graph_text(self) -> str:
        """The stored graph's spelling of this reference."""

        parts = [GRAPH_PREFIX[self.root]]
        if self.step is not None:
            parts.append(self.step)
        parts.extend(self.path)
        return ".".join(parts)

    def render(self) -> str:
        """A concise review rendering (the graph spelling)."""

        return self.graph_text()

    def child(self, *members: str) -> Ref:
        return self.model_copy(update={"path": (*self.path, *members)})


def make_ref(root: RefRoot, *path: str, step: str | None = None) -> Ref:
    """A reference: ``make_ref("step", "records", step="scores")``."""

    data: dict[str, object] = {"$ref": root, "path": path}
    if step is not None:
        data["step"] = step
    return Ref.model_validate(data)


def input_ref(*path: str) -> Ref:
    """``$input.<path>``: the Procedure input (or a member of it)."""

    return make_ref("input", *path)


def param_ref(name: str, *path: str) -> Ref:
    """``$params.<name>``: one Line parameter."""

    return make_ref("param", name, *path)


def step_ref(step: str, *path: str) -> Ref:
    """``$steps.<step>.<path>``: an earlier step's output (or a member of it)."""

    return make_ref("step", *path, step=step)


def item_ref(*path: str) -> Ref:
    """``$item.<path>``: the current record or element of a ``for_each``."""

    return make_ref("item", *path)


def ref_from_graph_text(text: str) -> Ref | None:
    """The typed reference a graph reference string spells, or ``None`` for a literal."""

    if not text.startswith("$"):
        return None
    head, _, rest = text.partition(".")
    root = _ROOT_OF_PREFIX.get(head)
    if root is None:
        return None
    members = tuple(rest.split(".")) if rest else ()
    if root == "step":
        if not members:
            return None
        return step_ref(members[0], *members[1:])
    return make_ref(root, *members)  # type: ignore[arg-type]


def is_ref_data(value: object) -> bool:
    """Whether authored JSON names a reference (an object with a ``$ref`` key)."""

    return isinstance(value, dict) and "$ref" in value


def concise(value: object) -> object:
    """A review rendering of authored JSON: each ``{"$ref": ...}`` as its ``$...`` text.

    Display only. A spec never spells a literal beginning with ``$``, so the
    rendering is unambiguous; the authored (typed) form is what is submitted.
    """

    if isinstance(value, dict):
        if "$ref" in value:
            try:
                return Ref.model_validate(value).render()
            except ValueError:
                pass
        return {key: concise(member) for key, member in value.items()}
    if isinstance(value, list | tuple):
        return [concise(member) for member in value]
    return value


def iter_refs(value: object) -> Iterator[Ref]:
    """Every :class:`Ref` nested in a typed value (models, mappings, sequences)."""

    if isinstance(value, Ref):
        yield value
        return
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from iter_refs(getattr(value, name))
        return
    if isinstance(value, dict):
        for member in value.values():
            yield from iter_refs(member)
        return
    if isinstance(value, list | tuple):
        for member in value:
            yield from iter_refs(member)


__all__ = [
    "GRAPH_PREFIX",
    "Ref",
    "RefRoot",
    "concise",
    "input_ref",
    "is_ref_data",
    "item_ref",
    "iter_refs",
    "make_ref",
    "param_ref",
    "ref_from_graph_text",
    "step_ref",
]
