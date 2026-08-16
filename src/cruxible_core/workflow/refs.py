"""Reference resolution for workflow input and step outputs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from cruxible_core.errors import ConfigError, QueryExecutionError

_SEGMENT_RE = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")
_RUNTIME_REFERENCE_ATTR = "_cruxible_workflow_reference"

_STEP_REFERENCE_TEMPLATE_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    # Query steps: params and relationship_state are resolved in workflow.io.
    "params": ((),),
    "relationship_state": ((),),
    # Provider steps: the whole input template is resolved in workflow.io.
    "input": ((),),
    # Assert family. ``message`` is literal operator-facing prose; ``step`` and
    # ``count`` name a prior alias and a selector. None of the three reach the
    # resolver, so none of them may be read as a reference.
    "assert": (("left",), ("right",)),
    "assert_count": (("value",),),
    "assert_exists": (("ref",),),
    # Item-shaping steps (workflow.transforms). ``rename``/``casts``/``required``
    # are literal field names; ``strategy``/``join_type``/``op`` are literals.
    "shape_items": (("items",), ("fields", "*")),
    "join_items": (
        ("left_items",),
        ("right_items",),
        ("left_key",),
        ("right_key",),
        ("fields", "*"),
    ),
    "filter_items": (
        ("items",),
        ("where", "*"),
        ("comparisons", "*", "left"),
        ("comparisons", "*", "right"),
    ),
    "aggregate_items": (
        ("items",),
        ("group_by", "*"),
        ("measures", "*", "count_where", "left"),
        ("measures", "*", "count_where", "right"),
        ("measures", "*", "count_distinct", "value"),
        ("measures", "*", "sum", "value"),
        ("measures", "*", "min", "value"),
        ("measures", "*", "max", "value"),
    ),
    "dedupe_items": (("items",), ("keys", "*"), ("rank",)),
    # Build steps (workflow.proposals / workflow.apply). ``entity_type``,
    # ``relationship_type``, ``signal_source``, ``candidates_from``,
    # ``signals_from`` and the ``score``/``enum`` mapping paths are literals.
    "make_candidates": (
        ("items",),
        ("from_type",),
        ("from_id",),
        ("to_type",),
        ("to_id",),
        ("properties", "*"),
        ("evidence", "refs"),
        ("evidence", "rationale"),
    ),
    "make_relationships": (
        ("items",),
        ("from_type",),
        ("from_id",),
        ("to_type",),
        ("to_id",),
        ("properties", "*"),
        ("evidence", "refs"),
        ("evidence", "rationale"),
    ),
    "make_entities": (("items",), ("entity_id",), ("properties", "*")),
    "map_signals": (
        ("items",),
        ("from_id",),
        ("to_id",),
        ("evidence",),
        ("evidence_refs",),
    ),
    "propose_relationship_group": (
        ("thesis_text",),
        ("analysis_state", "*"),
        ("suggested_priority",),
    ),
}
"""Per-step-field selectors naming exactly what :func:`resolve_value` walks.

Keys are the by-alias field names of a dumped workflow step; each selector is a
path within that field, ``()`` meaning the whole value and ``"*"`` matching every
dict value or list item at that position. Step kinds absent from the map
(``assert_not_truncated``, ``apply_entities``, ``apply_relationships``,
``apply_all``) carry no reference-bearing field at all.

Anything NOT selected here is literal text the resolver never sees -- an assert
``message``, a ``rename`` target, a step alias. Static analysis that reads
references out of a step definition must walk this map rather than the whole
dumped step, or literal prose beginning with ``$input.`` is mistaken for a
reference the runtime would never resolve.
"""


def iter_step_reference_templates(step: Mapping[str, Any]) -> Iterator[Any]:
    """Yield every sub-value of one dumped workflow step the resolver walks.

    The dump must be taken ``by_alias=True`` so the ``assert`` step kind is keyed
    by its alias rather than by the ``assert_spec`` attribute name.
    """
    for field_name, selectors in _STEP_REFERENCE_TEMPLATE_PATHS.items():
        if field_name not in step:
            continue
        value = step[field_name]
        for selector in selectors:
            yield from _select_reference_path(value, selector)


def _select_reference_path(value: Any, selector: tuple[str, ...]) -> Iterator[Any]:
    if not selector:
        yield value
        return
    head, rest = selector[0], selector[1:]
    if head == "*":
        if isinstance(value, Mapping):
            for item in value.values():
                yield from _select_reference_path(item, rest)
        elif isinstance(value, list):
            for item in value:
                yield from _select_reference_path(item, rest)
        return
    if isinstance(value, Mapping) and head in value:
        yield from _select_reference_path(value[head], rest)


def preview_value(
    value: Any,
    input_payload: dict[str, Any],
    *,
    step_aliases: Iterable[str] = (),
) -> Any:
    """Resolve only $input refs for plan preview output, failing closed.

    ``$input`` refs are resolved against ``input_payload``. A ``$steps.<alias>``
    ref to a *known prior step* (``step_aliases``) cannot be resolved at preview
    time — its value is only produced at execution — so the literal placeholder
    is preserved (the documented preview behavior for forward step references).

    Every other reference shape is **unresolvable** and fails closed with a
    clear :class:`ConfigError` rather than leaking the literal placeholder, which
    would silently misrepresent what the plan will send:

    * ``$item`` / ``$item.<...>`` — per-item payloads do not exist in a
      query/provider step preview;
    * bare ``$steps`` or ``$steps.<unknown>`` — no such step output exists.
    """
    known_aliases = frozenset(step_aliases)
    return _preview_value(value, input_payload, known_aliases)


def preview_definition_value(
    value: Any,
    *,
    step_aliases: Iterable[str] = (),
) -> Any:
    """Validate definition-time refs while preserving unresolved ``$input`` refs.

    State-held procedure definitions are compiled before an invocation payload
    exists. Their input references remain literal, while unknown step aliases
    and unsupported item refs still fail closed exactly as plan preview does.
    """
    known_aliases = frozenset(step_aliases)
    return _preview_definition_value(value, known_aliases)


def _preview_definition_value(value: Any, known_aliases: frozenset[str]) -> Any:
    if isinstance(value, str):
        if value == "$input" or value.startswith("$input."):
            return value
        if _is_resolvable_step_ref(value, known_aliases):
            return value
        _reject_unresolvable_preview_ref(value)
        return value
    if isinstance(value, dict):
        return {key: _preview_definition_value(item, known_aliases) for key, item in value.items()}
    if isinstance(value, list):
        return [_preview_definition_value(item, known_aliases) for item in value]
    return value


def _preview_value(
    value: Any,
    input_payload: dict[str, Any],
    known_aliases: frozenset[str],
) -> Any:
    if isinstance(value, str):
        if value.startswith("$input."):
            return _extract_path(input_payload, value[len("$input.") :], value)
        if _is_resolvable_step_ref(value, known_aliases):
            return value
        _reject_unresolvable_preview_ref(value)
        return value
    if isinstance(value, dict):
        return {k: _preview_value(v, input_payload, known_aliases) for k, v in value.items()}
    if isinstance(value, list):
        return [_preview_value(v, input_payload, known_aliases) for v in value]
    return value


def _is_resolvable_step_ref(value: str, known_aliases: frozenset[str]) -> bool:
    """Return whether a ``$steps.<alias>`` ref targets a known prior step."""
    if not value.startswith("$steps."):
        return False
    alias = value[len("$steps.") :].split(".", 1)[0].split("[", 1)[0]
    return bool(alias) and alias in known_aliases


def _reject_unresolvable_preview_ref(value: str) -> None:
    """Raise if a preview value is a ref that can't be resolved at preview time."""
    if value == "$item" or value.startswith("$item."):
        raise ConfigError(
            f"Workflow plan preview cannot resolve runtime reference '{value}': "
            "'$item' references are only available during per-item execution, "
            "not in a plan preview."
        )
    if value == "$steps" or value.startswith("$steps."):
        raise ConfigError(
            f"Workflow plan preview cannot resolve runtime reference '{value}': "
            "it does not name a known prior step output."
        )


def resolve_value(
    value: Any,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
    *,
    item_payload: Any | None = None,
    allow_item: bool = False,
) -> Any:
    """Resolve $input and $steps refs during workflow execution."""
    if isinstance(value, str):
        if value == "$input":
            return input_payload
        if value.startswith("$input."):
            return _extract_path(input_payload, value[len("$input.") :], value)
        if value == "$item":
            if allow_item and item_payload is not None:
                return item_payload
            raise _runtime_reference_error(
                value,
                f"Unsupported workflow reference '{value}'",
            )
        if value.startswith("$item."):
            if allow_item and item_payload is not None:
                return _extract_path(item_payload, value[len("$item.") :], value)
            raise _runtime_reference_error(
                value,
                f"Unsupported workflow reference '{value}'",
            )
        if value.startswith("$steps."):
            ref = value[len("$steps.") :]
            alias, _, remainder = ref.partition(".")
            if alias not in step_outputs:
                raise _runtime_reference_error(
                    value,
                    f"Unknown workflow step alias '{alias}' in reference '{value}'",
                )
            target = step_outputs[alias]
            if not remainder:
                return target
            return _extract_path(target, remainder, value)
        return value

    if isinstance(value, dict):
        return {
            k: resolve_value(
                v,
                input_payload,
                step_outputs,
                item_payload=item_payload,
                allow_item=allow_item,
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_value(
                v,
                input_payload,
                step_outputs,
                item_payload=item_payload,
                allow_item=allow_item,
            )
            for v in value
        ]
    return value


def _extract_path(root: Any, path: str, original_ref: str) -> Any:
    current = root
    for match in _SEGMENT_RE.finditer(path):
        key, index = match.groups()
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                raise _runtime_reference_error(
                    original_ref,
                    f"Reference '{original_ref}' could not resolve path '{path}'",
                )
            current = current[key]
            continue
        assert index is not None
        if not isinstance(current, list):
            raise _runtime_reference_error(
                original_ref,
                f"Reference '{original_ref}' expected a list before '[{index}]'",
            )
        idx = int(index)
        try:
            current = current[idx]
        except IndexError as exc:
            raise _runtime_reference_error(
                original_ref,
                f"Reference '{original_ref}' index [{idx}] is out of range",
            ) from exc
    return current


def runtime_reference_from_error(exc: BaseException) -> str | None:
    """Return the workflow reference attached to a typed resolution failure."""
    reference = getattr(exc, _RUNTIME_REFERENCE_ATTR, None)
    return reference if isinstance(reference, str) else None


def _runtime_reference_error(reference: str, message: str) -> QueryExecutionError:
    """Build a typed resolution error while retaining its exact source ref."""
    exc = QueryExecutionError(message)
    setattr(exc, _RUNTIME_REFERENCE_ATTR, reference)
    return exc
