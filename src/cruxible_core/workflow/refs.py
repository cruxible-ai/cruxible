"""Reference resolution for workflow input and step outputs."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from cruxible_core.errors import ConfigError, QueryExecutionError

_SEGMENT_RE = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")


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
            raise QueryExecutionError(f"Unsupported workflow reference '{value}'")
        if value.startswith("$item."):
            if allow_item and item_payload is not None:
                return _extract_path(item_payload, value[len("$item.") :], value)
            raise QueryExecutionError(f"Unsupported workflow reference '{value}'")
        if value.startswith("$steps."):
            ref = value[len("$steps.") :]
            alias, _, remainder = ref.partition(".")
            if alias not in step_outputs:
                raise QueryExecutionError(
                    f"Unknown workflow step alias '{alias}' in reference '{value}'"
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
                raise QueryExecutionError(
                    f"Reference '{original_ref}' could not resolve path '{path}'"
                )
            current = current[key]
            continue
        assert index is not None
        if not isinstance(current, list):
            raise QueryExecutionError(
                f"Reference '{original_ref}' expected a list before '[{index}]'"
            )
        idx = int(index)
        try:
            current = current[idx]
        except IndexError as exc:
            raise QueryExecutionError(
                f"Reference '{original_ref}' index [{idx}] is out of range"
            ) from exc
    return current
