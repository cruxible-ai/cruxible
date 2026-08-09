"""T1 -- the serialization field policy for procedure definitions.

Definitions are digested as ``model_dump(mode="json", by_alias=True,
exclude_none=True)`` over ``canonical_json``. Two consequences make this a
guardrail rather than a convention:

* a field whose default is ``None`` is DROPPED from every definition that does
  not set it, so adding one moves no existing byte;
* a field whose default is anything else is EMITTED, so adding one moves every
  existing definition's digest -- silently, at the next upgrade, on every
  instance, as a mass refusal.

So every field reachable from ``ProcedureDefinition`` must either default to
``None`` or appear in the pinned legacy allow-list below. The allow-list is the
set that already shipped: those defaults are already inside every frozen corpus
digest, and they may never change value.
"""

from __future__ import annotations

from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from cruxible_core.procedure.types import ProcedureDefinition

# (model name, field name) -> the shipped default, pinned by value.
# Adding a row is a deliberate act: it moves every definition that omits the
# field, so it belongs in the same review as a digest-corpus regeneration.
LEGACY_NON_NONE_DEFAULTS: dict[tuple[str, str], Any] = {
    ("AggregateItemsSpec", "group_by"): {},
    ("ApplyAllSpec", "entities_from"): [],
    ("ApplyAllSpec", "relationships_from"): [],
    # These pre-existing resolution-contract models become reachable from a
    # procedure definition only through F2's optional v2-only measurements
    # field. No historical definition can contain that subtree, and the field
    # is registered in the envelope in the same commit.
    ("AttestationMeasurement", "kind"): "attestation",
    ("ContractSchema", "allow_extra"): False,
    ("DedupeItemsSpec", "strategy"): "first",
    ("FilterItemsSpec", "comparisons"): [],
    ("FilterItemsSpec", "where"): {},
    ("JoinItemsSpec", "fields"): {},
    ("JoinItemsSpec", "join_type"): "inner",
    ("MakeCandidatesSpec", "properties"): {},
    ("MakeEntitiesSpec", "properties"): {},
    ("MakeRelationshipsSpec", "properties"): {},
    ("MeasurementExpectation", "condition_scope"): "all",
    ("NamedQuerySchema", "allow_relationship_state_override"): False,
    ("NamedQuerySchema", "dedupe"): "path",
    ("NamedQuerySchema", "include"): {},
    ("NamedQuerySchema", "order_by"): [],
    ("NamedQuerySchema", "relationship_state"): "live",
    ("NamedQuerySchema", "result_shape"): "path",
    ("NamedQuerySchema", "traversal"): [],
    ("ProcedureDefinition", "contract_in"): "cruxible.EmptyInput",
    ("ProcedureDefinition", "declared_tier"): "governed_write",
    ("PropertySchema", "indexed"): False,
    ("PropertySchema", "optional"): False,
    ("PropertySchema", "primary_key"): False,
    ("PropertySchema", "type"): "string",
    ("ProposeRelationshipGroupSpec", "analysis_state"): {},
    ("ProposeRelationshipGroupSpec", "pending_refresh_mode"): "replace",
    ("ProposeRelationshipGroupSpec", "thesis_text"): "",
    ("QueryIncludeSpec", "direction"): "outgoing",
    ("QueryIncludeSpec", "many"): False,
    ("QueryIncludeSpec", "order_by"): [],
    ("QueryIncludeSpec", "required"): False,
    ("QueryIncludeSpec", "where_not_related"): [],
    ("QueryIncludeSpec", "where_related"): [],
    ("QueryMeasurement", "kind"): "query",
    ("QueryMeasurement", "params"): {},
    ("QueryOrderSpec", "direction"): "asc",
    ("RelatedExclusionSpec", "direction"): "outgoing",
    ("RelatedPredicateSpec", "direction"): "outgoing",
    ("ShapeItemsSpec", "casts"): {},
    ("ShapeItemsSpec", "fields"): {},
    ("ShapeItemsSpec", "include_input"): False,
    ("ShapeItemsSpec", "on_missing_required"): "error",
    ("ShapeItemsSpec", "rename"): {},
    ("ShapeItemsSpec", "required"): [],
    ("TraversalStep", "direction"): "outgoing",
    ("TraversalStep", "exclude_if_related"): [],
    ("TraversalStep", "max_depth"): 1,
    ("TraversalStep", "required"): True,
    ("TraversalStep", "where_not_related"): [],
    ("TraversalStep", "where_related"): [],
    ("WorkflowStepSchema", "include_source"): False,
    ("WorkflowStepSchema", "input"): {},
    ("WorkflowStepSchema", "params"): {},
}


def _referenced_models(model: type[BaseModel]) -> dict[str, type[BaseModel]]:
    """Walk every model reachable from ``model`` through declared field types."""
    seen: dict[str, type[BaseModel]] = {model.__name__: model}
    frontier = [model]
    while frontier:
        current = frontier.pop()
        for field in current.model_fields.values():
            for nested in _model_types(field.annotation):
                if nested.__name__ in seen:
                    continue
                seen[nested.__name__] = nested
                frontier.append(nested)
    return seen


def _model_types(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    origin = get_origin(annotation)
    if origin is None:
        return []
    if origin in (Union, list, dict, tuple, set, frozenset) or origin is not None:
        return [nested for arg in get_args(annotation) for nested in _model_types(arg)]
    return []


REACHABLE_MODELS = _referenced_models(ProcedureDefinition)


@pytest.mark.parametrize("model_name", sorted(REACHABLE_MODELS))
def test_every_reachable_field_defaults_to_none_or_a_pinned_legacy_value(
    model_name: str,
) -> None:
    model = REACHABLE_MODELS[model_name]
    for field_name, field in model.model_fields.items():
        if field.default is PydanticUndefined and field.default_factory is None:
            continue  # required: always emitted, always was
        default = field.default
        if field.default_factory is not None:
            default = field.default_factory()  # type: ignore[call-arg]
        if default is None:
            continue
        key = (model_name, field_name)
        assert key in LEGACY_NON_NONE_DEFAULTS, (
            f"{model_name}.{field_name} defaults to {default!r}. A non-None default is "
            "EMITTED by exclude_none dumps, so it moves the digest of every stored "
            "definition that omits the field. Declare the field as `X | None = None`, "
            "or -- if it genuinely shipped already -- pin it in LEGACY_NON_NONE_DEFAULTS "
            "in the same review as a digest-corpus regeneration."
        )
        assert default == LEGACY_NON_NONE_DEFAULTS[key], (
            f"{model_name}.{field_name} default changed from "
            f"{LEGACY_NON_NONE_DEFAULTS[key]!r} to {default!r}; every frozen corpus "
            "digest was captured over the old value."
        )


def test_allow_list_has_no_stale_rows() -> None:
    stale = [
        key
        for key in LEGACY_NON_NONE_DEFAULTS
        if key[0] not in REACHABLE_MODELS or key[1] not in REACHABLE_MODELS[key[0]].model_fields
    ]
    assert stale == [], f"pinned defaults name fields that no longer exist: {stale}"
