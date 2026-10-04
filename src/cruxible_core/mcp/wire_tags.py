"""Format tags are internal: MCP input schemas hide them and the server fills them in.

Every request model carries a ``playbill-*-vN`` format tag (``tag``,
``artifact_format``...). The tag is how bytes name their own format, which is
the server's business; a model writing a call should not have to copy it. So the
advertised input schema drops every property whose only admissible values are
format tags, and before the call validates, each missing tag is filled from the
model: the field's one value, or, where a union is told apart by its tag, the
newest member the arguments validate as. A caller that does send a tag is
checked as before. Output schemas, proofs and the ledger keep their tags.
"""

from __future__ import annotations

import copy
import re
import types
import typing
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

#: A format tag: the frozen ``playbill-`` namespace, one family, a version.
TAG_RE = re.compile(r"^playbill-[a-z0-9-]+-v\d+(?:\.\d+)?$")
_VERSION_RE = re.compile(r"-v(\d+)(?:\.(\d+))?$")


def is_tag(value: object) -> bool:
    return isinstance(value, str) and TAG_RE.fullmatch(value) is not None


# ---------------------------------------------------------------- schemas


def _tag_only_schema(schema: object) -> bool:
    if not isinstance(schema, Mapping):
        return False
    if "const" in schema:
        return is_tag(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return all(is_tag(item) for item in enum)
    for key in ("anyOf", "oneOf"):
        members = schema.get(key)
        if isinstance(members, list) and members:
            return all(_tag_only_schema(member) for member in members)
    return False


def _strip(node: object, removed: set[str], stripped: set[int]) -> object:
    if isinstance(node, list):
        return [_strip(item, removed, stripped) for item in node]
    if not isinstance(node, dict):
        return node
    properties = node.get("properties")
    if isinstance(properties, dict):
        hidden = [name for name, schema in properties.items() if _tag_only_schema(schema)]
        for name in hidden:
            del properties[name]
            removed.add(name)
        if hidden:
            stripped.add(id(node))
        required = node.get("required")
        if isinstance(required, list) and hidden:
            kept = [name for name in required if name not in hidden]
            if kept:
                node["required"] = kept
            else:
                del node["required"]
    for key, value in list(node.items()):
        node[key] = _strip(value, removed, stripped)
    return node


def _branch(node: object, definitions: dict[str, Any]) -> object:
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        return definitions.get(node["$ref"].rsplit("/", 1)[-1], node)
    return node


def _loosen_unions(
    node: object, removed: set[str], stripped: set[int], definitions: dict[str, Any]
) -> None:
    """Hidden tags leave versions of one model accepting the same arguments.

    A ``oneOf`` over such branches would reject a valid call (exactly one branch
    must match), so it becomes ``anyOf``; the server picks the member when it
    fills the tag. A discriminator on a hidden tag goes with it.
    """

    if isinstance(node, list):
        for item in node:
            _loosen_unions(item, removed, stripped, definitions)
        return
    if not isinstance(node, dict):
        return
    discriminator = node.get("discriminator")
    if isinstance(discriminator, dict) and discriminator.get("propertyName") in removed:
        del node["discriminator"]
    branches = node.get("oneOf")
    if isinstance(branches, list) and any(
        id(_branch(branch, definitions)) in stripped for branch in branches
    ):
        node.pop("discriminator", None)
        node["anyOf"] = node.pop("oneOf")
    for value in node.values():
        _loosen_unions(value, removed, stripped, definitions)


def hide_wire_tags(schema: dict[str, Any]) -> dict[str, Any]:
    """The input schema a model sees: no property that only admits format tags."""

    result = copy.deepcopy(schema)
    removed: set[str] = set()
    stripped: set[int] = set()
    _strip(result, removed, stripped)
    definitions = result.get("$defs")
    _loosen_unions(result, removed, stripped, definitions if isinstance(definitions, dict) else {})
    return result


def exposed_wire_tags(schema: object) -> list[str]:
    """Every format tag an input schema still offers as a const or enum value."""

    found: list[str] = []
    if isinstance(schema, list):
        for item in schema:
            found.extend(exposed_wire_tags(item))
    elif isinstance(schema, Mapping):
        const = schema.get("const")
        if is_tag(const):
            found.append(str(const))
        enum = schema.get("enum")
        if isinstance(enum, list):
            found.extend(str(item) for item in enum if is_tag(item))
        for value in schema.values():
            found.extend(exposed_wire_tags(value))
    return found


# ---------------------------------------------------------------- arguments


_TYPE_ALIAS_TYPES: tuple[type, ...] = tuple(
    kind for kind in (getattr(typing, "TypeAliasType", None),) if isinstance(kind, type)
)


def _unwrap(annotation: Any) -> Any:
    while True:
        if isinstance(annotation, _TYPE_ALIAS_TYPES):
            annotation = getattr(annotation, "__value__")
            continue
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
            continue
        return annotation


def _union_members(annotation: Any) -> tuple[Any, ...] | None:
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return tuple(_unwrap(member) for member in get_args(annotation))
    return None


def _tag_values(annotation: Any) -> tuple[str, ...]:
    annotation = _unwrap(annotation)
    if get_origin(annotation) is Literal:
        values = get_args(annotation)
        if values and all(is_tag(value) for value in values):
            return tuple(str(value) for value in values)
    return ()


def _version(tag: str) -> tuple[int, int]:
    match = _VERSION_RE.search(tag)
    return (0, 0) if match is None else (int(match.group(1)), int(match.group(2) or 0))


def _model_tags(model: type[BaseModel]) -> dict[str, tuple[str, ...]]:
    """Tag fields of a model, by input key, with their admissible values."""

    tags: dict[str, tuple[str, ...]] = {}
    for name, field in model.model_fields.items():
        values = _tag_values(field.annotation)
        if values:
            tags[field.alias or name] = values
    return tags


def _default_tag(model: type[BaseModel], key: str, values: tuple[str, ...]) -> str:
    for name, field in model.model_fields.items():
        if (field.alias or name) == key and is_tag(field.default):
            return str(field.default)
    return max(values, key=_version)


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _fill_model(value: Any, model: type[BaseModel]) -> Any:
    if not isinstance(value, Mapping):
        return value
    filled = dict(value)
    for key, values in _model_tags(model).items():
        if key not in filled:
            filled[key] = _default_tag(model, key, values)
    for name, field in model.model_fields.items():
        key = field.alias or name
        if key in filled and not _tag_values(field.annotation):
            filled[key] = fill_wire_tags(filled[key], field.annotation)
    return filled


def _member_rank(member: Any) -> tuple[int, int]:
    if not _is_model(member):
        return (-1, 0)
    tags = [tag for values in _model_tags(member).values() for tag in values]
    return max((_version(tag) for tag in tags), default=(0, 0))


def _admits_supplied_tags(member: type[BaseModel], value: Mapping[str, Any]) -> bool:
    """Whether every tag ``value`` supplies is one ``member`` admits."""

    tags = _model_tags(member)
    return all(value[key] in admitted for key, admitted in tags.items() if key in value)


def _fill_union(value: Any, members: tuple[Any, ...]) -> Any:
    if isinstance(value, list):
        for member in members:
            if get_origin(member) in (list, tuple, set, frozenset):
                return fill_wire_tags(value, member)
        return value
    models = [member for member in members if _is_model(member)]
    if not isinstance(value, Mapping) or not models:
        return value
    # Supplied tag values narrow the members (a tag several members share, such
    # as a grammar version, narrows nothing); the newest remaining member the
    # arguments validate as is the one, and a call no member accepts is filled
    # for the newest so its refusal names that member's fields.
    candidates = [member for member in models if _admits_supplied_tags(member, value)] or models
    ordered = sorted(candidates, key=_member_rank, reverse=True)
    for member in ordered:
        candidate = _fill_model(value, member)
        try:
            member.model_validate(candidate)
        except ValidationError:
            continue
        return candidate
    return _fill_model(value, ordered[0])


def fill_wire_tags(value: Any, annotation: Any) -> Any:
    """``value`` with every format tag the declared type requires filled in."""

    annotation = _unwrap(annotation)
    if value is None:
        return value
    members = _union_members(annotation)
    if members is not None:
        return _fill_union(value, members)
    if _is_model(annotation):
        return _fill_model(value, annotation)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (list, set, frozenset, Sequence) or (
        isinstance(origin, type) and issubclass(origin, Sequence) and origin is not tuple
    ):
        if isinstance(value, list) and args:
            return [fill_wire_tags(item, args[0]) for item in value]
        return value
    if origin is tuple and isinstance(value, list | tuple):
        if len(args) == 2 and args[1] is Ellipsis:
            return [fill_wire_tags(item, args[0]) for item in value]
        return [
            fill_wire_tags(item, arg) if index < len(args) else item
            for index, (item, arg) in enumerate(zip(value, args, strict=False))
        ] + list(value[len(args) :])
    if origin in (dict, Mapping) or (isinstance(origin, type) and issubclass(origin, Mapping)):
        if isinstance(value, Mapping) and len(args) == 2:
            return {key: fill_wire_tags(item, args[1]) for key, item in value.items()}
        return value
    return value


def fill_tool_arguments(arg_model: type[BaseModel], arguments: dict[str, Any]) -> dict[str, Any]:
    """Fill the format tags of one MCP call's arguments before they validate."""

    filled = dict(arguments)
    for name, field in arg_model.model_fields.items():
        key = field.alias or name
        if key in filled:
            filled[key] = fill_wire_tags(filled[key], field.annotation)
    return filled


__all__ = [
    "TAG_RE",
    "exposed_wire_tags",
    "fill_tool_arguments",
    "fill_wire_tags",
    "hide_wire_tags",
    "is_tag",
]
