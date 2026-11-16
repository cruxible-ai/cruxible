"""What one Query row offers a Procedure step that iterates a read's ``rows``.

A ``read`` of a QueryDefinition exposes ``rows``: each row as a plain record of
its present projected columns (``$item.<column>``). A step whose ``for_each``
is exactly that list (an inbox step, a claims step) also reads, per row:

- ``$item.subject``: the row's result Subject, as the ``<kind>/<id>`` reference
  a ``subject<kind>`` field takes;
- ``$item.bindings.<name>``: the Subject the row bound to that declared binding
  (left out when an optional binding bound nothing).

They come from the read's whole result (``$steps.<read>.result``), row by row,
so the retained read material and its digests are unchanged. A declared column
of the same name wins. Anything else a row does not offer refuses at authoring
(:func:`validate_row_references`), listing what the row offers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from cruxible_client.contracts.shapes import SubjectType

_ROWS_SUFFIX = ".rows"
_STEPS = "$steps."
_SUBJECT_PREFIX = "Subject:"
ITEM_REFERENCE_UNRESOLVED = "cruxible.procedure.item_reference_unresolved"
#: Closed transforms that keep the records they are given whole.
ROW_PRESERVING_TRANSFORMS = frozenset({"filter", "dedupe"})


def rows_alias(for_each: object) -> str | None:
    """The read alias when ``for_each`` is exactly ``$steps.<alias>.rows``."""

    if not isinstance(for_each, str) or not for_each.startswith(_STEPS):
        return None
    parts = for_each[len(_STEPS) :].split(".")
    return parts[0] if len(parts) == 2 and parts[1] == "rows" else None


def _reference(identity: object) -> str | None:
    if not isinstance(identity, str) or not identity.startswith(_SUBJECT_PREFIX):
        return None
    return identity[len(_SUBJECT_PREFIX) :]


def query_row_items(
    for_each: object, outputs: Mapping[str, object], rows: Sequence[object]
) -> list[object] | None:
    """``rows`` with each row's ``subject`` and ``bindings``, or ``None`` when not a Query read.

    Only a ``for_each`` that is exactly a Query read's ``rows`` gains them; the
    read's view keeps its whole result beside ``rows`` in the same order.
    """

    alias = rows_alias(for_each)
    view = None if alias is None else outputs.get(alias)
    if not isinstance(view, Mapping) or "definition_digest" not in view:
        return None
    result = view.get("result")
    served = result.get("rows") if isinstance(result, Mapping) else None
    if not isinstance(served, list) or len(served) != len(rows):
        return None
    items: list[object] = []
    for row, whole in zip(rows, served, strict=True):
        if not isinstance(row, Mapping) or not isinstance(whole, Mapping):
            items.append(row)
            continue
        offered: dict[str, object] = {}
        subject = _reference(whole.get("result_subject_identity"))
        if subject is not None:
            offered["subject"] = subject
        bound = {
            str(binding["binding"]): reference
            for binding in whole.get("bindings") or ()
            if isinstance(binding, Mapping)
            and (reference := _reference(binding.get("subject_identity"))) is not None
        }
        offered["bindings"] = bound
        items.append({**offered, **row})
    return items


# -- authoring ---------------------------------------------------------------


class ItemReferenceUnresolved(ValueError):
    """A step reads ``$item...`` that the Query row it iterates does not offer."""

    code = ITEM_REFERENCE_UNRESOLVED

    def __init__(self, message: str, *, node_id: str, element: str, repair: str) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.element = element
        self.repair = repair


def binding_subject_kinds(query: Any) -> dict[str, tuple[str, ...]]:
    """Each declared row binding's Subject kinds (empty: any kind)."""

    entry = query.entry
    kinds: dict[str, tuple[str, ...]] = {
        entry.binding: tuple(getattr(entry, "subject_kinds", ()) or ())
    }
    for step in query.traversal:
        kinds[step.binding] = tuple(step.target_subject_kinds)
    return kinds


def query_columns(query: Any) -> tuple[str, ...]:
    projection = getattr(query, "projection", None)
    return () if projection is None else tuple(field.name for field in projection.fields)


def row_reference_type(query: Any, path: Sequence[str]) -> SubjectType | None:
    """The Subject type of ``$item.subject`` / ``$item.bindings.<b>``, when not a column."""

    if not path or path[0] in query_columns(query):
        return None
    kinds = binding_subject_kinds(query)
    if list(path) == ["subject"]:
        found = kinds.get(query.result_binding, ())
    elif len(path) == 2 and path[0] == "bindings" and path[1] in kinds:
        found = kinds[path[1]]
    else:
        return None
    return SubjectType(subject_kinds=found) if found else None


def _offers(query: Any, path: Sequence[str]) -> bool:
    if not path:
        return True
    columns = query_columns(query)
    if path[0] in columns:
        return True
    if list(path) == ["subject"]:
        return True
    return len(path) == 2 and path[0] == "bindings" and path[1] in query.row_bindings


def _item_strings(value: object) -> Iterator[str]:
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        yield from _item_strings(value.model_dump(mode="python", by_alias=True))
    elif isinstance(value, str):
        if value == "$item" or value.startswith("$item."):
            yield value
    elif isinstance(value, Mapping):
        for member in value.values():
            yield from _item_strings(member)
    elif isinstance(value, list | tuple):
        for member in value:
            yield from _item_strings(member)


def _elements(templates: Iterator[tuple[str, object]]) -> Iterator[tuple[str, object]]:
    """``(element, template)`` per authored template, a record's fields one by one."""

    for field, template in templates:
        if field in ("for_each", "where", "expect"):
            continue
        if isinstance(template, Mapping):
            for key, member in template.items():
                yield f"{field}.{key}", member
        else:
            yield field, template


def _offer_text(query: Any) -> str:
    columns = query_columns(query)
    kinds = binding_subject_kinds(query)
    subject_kinds = kinds.get(query.result_binding, ())
    parts = [
        "columns " + ", ".join(f"$item.{name}" for name in columns) if columns else "no columns",
        "$item.subject (the row's Subject"
        + (f", subject<{' | '.join(subject_kinds)}>" if subject_kinds else "")
        + ")",
        "bindings " + ", ".join(f"$item.bindings.{name}" for name in query.row_bindings),
    ]
    return "; ".join(parts)


def _row_source(for_each: object, producers: Mapping[object, object]) -> tuple[Any, str | None]:
    """The Query read whose rows ``for_each`` reaches, and the step that dropped them.

    ``$steps.<a>.rows`` of a read is its rows; of a filter or dedupe, the rows
    it was given (followed back). Through any other transform the rows are
    rebuilt, so the read is returned with that step's id: it does not carry a
    row's Subject and bindings. ``(None, None)`` when no Query read is reached.
    """

    from cruxible_client.contracts.procedures.models import ReadNode, TransformNode

    seen: set[str] = set()
    reference = for_each
    while (alias := rows_alias(reference)) is not None and alias not in seen:
        seen.add(alias)
        producer = producers.get(alias)
        if isinstance(producer, ReadNode):
            return (producer, None) if producer.query is not None else (None, None)
        if not isinstance(producer, TransformNode):
            return None, None
        spec = producer.spec
        if producer.transform_kind in ROW_PRESERVING_TRANSFORMS:
            reference = getattr(spec, "input", None)
            continue
        inputs = getattr(spec, "inputs", None) or (getattr(spec, "input", None),)
        for member in inputs:
            read, _lost = _row_source(member, producers)
            if read is not None:
                return read, producer.node_id
        return None, None
    return None, None


def validate_row_references(definition: Any, lookup: Callable[[Any], Any]) -> None:
    """Refuse an inbox or claims step reading ``$item...`` its Query row does not offer.

    ``for_each`` reaches the read's rows directly or through filter/dedupe.
    Through any other transform a row's ``subject`` and ``bindings`` are gone,
    and a reference to them refuses naming that step. Only this Procedure's
    nodes and the QueryDefinitions they read are consulted; a read whose Query
    is not resolvable is left to the other laws.
    """

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.procedures.claims import ClaimsNode
    from cruxible_client.contracts.procedures.graph import reference_templates
    from cruxible_client.contracts.procedures.models import InboxEgressNode

    producers: dict[object, object] = {
        getattr(node, "as_", None): node for node in definition.nodes
    }
    for node in definition.nodes:
        if not isinstance(node, InboxEgressNode | ClaimsNode):
            continue
        read, lost = _row_source(node.for_each, producers)
        if read is None:
            continue
        query = lookup(ArtifactIdentity(kind="QueryDefinition", name=read.query.target.name))
        if query is None or not hasattr(query, "row_bindings"):
            continue
        name = query.identity.name
        rebuilt = None if lost is None else producers.get(rows_alias(node.for_each))
        declared = set(getattr(getattr(rebuilt, "spec", None), "fields", None) or ())
        for element, template in _elements(reference_templates(node)):
            for reference in _item_strings(template):
                path = reference.split(".")[1:]
                if lost is not None:
                    if path and path[0] in ("subject", "bindings") and path[0] not in declared:
                        raise ItemReferenceUnresolved(
                            f"{reference} does not resolve: for_each {node.for_each} reaches "
                            f"the rows of Query {name} through step {lost!r}, which rebuilds "
                            "them, so its rows no longer carry each row's subject and bindings "
                            "(only filter and dedupe keep them)",
                            node_id=node.node_id,
                            element=element,
                            repair=f"Iterate the read's rows directly or through filter/dedupe "
                            f"only, or declare the value as a column of Query {name} and carry "
                            f"it through step {lost!r}.",
                        )
                    continue
                if _offers(query, path):
                    continue
                raise ItemReferenceUnresolved(
                    f"{reference} does not resolve: a row of Query {name} (for_each "
                    f"{node.for_each}) offers {_offer_text(query)}",
                    node_id=node.node_id,
                    element=element,
                    repair=f"Read one of those, or declare the value as a column of Query "
                    f"{name} (a projection field) and read it as $item.<column>.",
                )


__all__ = [
    "ITEM_REFERENCE_UNRESOLVED",
    "ROW_PRESERVING_TRANSFORMS",
    "ItemReferenceUnresolved",
    "binding_subject_kinds",
    "query_row_items",
    "row_reference_type",
    "rows_alias",
    "validate_row_references",
]
