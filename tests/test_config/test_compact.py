"""Tests for the compact-config expander (``cruxible_core.config.compact``).

Covers every compact grammar/construct in the agent-operation compact source
(``kits/agent-operation/config.yaml``) and each of the 5 expander contract
invariants. (The docs/dev draft is a local commented reference of the same grammar.)
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cruxible_core.config.compact import (
    CompactExpansionError,
    dump_expanded,
    expand_compact,
    expand_compact_file,
    expand_compact_file_full,
    expand_compact_full,
)
from cruxible_core.config.schema import CoreConfig

KIT_DIR = Path(__file__).resolve().parents[2] / "kits" / "agent-operation"
# config.yaml is the single source of truth (compact); the loader expands it on load,
# so there is no committed expanded artifact. Tests run against this committed source.
# (The docs/dev draft is a local-only commented reference; it expands identically.)
DRAFT_PATH = KIT_DIR / "config.yaml"


def _expand(*parts: str) -> dict:
    """Expand a compact source assembled from one or more fragments.

    Each fragment is dedented independently (so a flush-left header and an indented
    body triple-quote both normalize to column 0) before being concatenated.
    """
    return expand_compact(_join(*parts))


def _join(*parts: str) -> str:
    return "\n".join(dedent(part).strip("\n") for part in parts) + "\n"


def _minimal_header(body: str) -> str:
    """Wrap a body fragment with the minimum top-level keys CoreConfig needs."""
    return dedent(
        """
        version: "1.0"
        name: test_kit
        """
    ) + dedent(body)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_enum_list_shorthand_expands_to_values() -> None:
    config = _expand(
        """
        name: k
        enums:
          actor_kind: [human, agent]
        entity_types: {}
        """
    )
    assert config["enums"]["actor_kind"] == {"values": ["human", "agent"]}


def test_enum_ordered_mapping_passes_through() -> None:
    config = _expand(
        """
        name: k
        enums:
          priority: {values: [low, high], ordered: low_to_high}
        entity_types: {}
        """
    )
    assert config["enums"]["priority"] == {
        "values": ["low", "high"],
        "ordered": "low_to_high",
    }


# ---------------------------------------------------------------------------
# Entity property scalar grammar
# ---------------------------------------------------------------------------


def test_property_scalar_type_string() -> None:
    config = _expand(
        """
        name: k
        entity_types:
          E:
            properties:
              field: string
        """
    )
    assert config["entity_types"]["E"]["properties"]["field"] == {"type": "string"}


def test_property_optional_trailing_question_mark() -> None:
    config = _expand(
        """
        name: k
        entity_types:
          E:
            properties:
              field: string?
        """
    )
    assert config["entity_types"]["E"]["properties"]["field"] == {
        "type": "string",
        "optional": True,
    }


def test_property_indexed_modifier() -> None:
    config = _expand(
        """
        name: k
        entity_types:
          E:
            properties:
              field: string indexed
        """
    )
    assert config["entity_types"]["E"]["properties"]["field"] == {
        "type": "string",
        "indexed": True,
    }


def test_property_enum_ref() -> None:
    config = _expand(
        """
        name: k
        enums:
          actor_kind: [human, agent]
        entity_types:
          E:
            properties:
              kind: enum actor_kind
        """
    )
    assert config["entity_types"]["E"]["properties"]["kind"] == {
        "type": "string",
        "enum_ref": "actor_kind",
    }


def test_property_enum_ref_with_default() -> None:
    config = _expand(
        """
        name: k
        enums:
          actor_status: [active, inactive]
        entity_types:
          E:
            properties:
              status: enum actor_status = active
        """
    )
    assert config["entity_types"]["E"]["properties"]["status"] == {
        "type": "string",
        "enum_ref": "actor_status",
        "default": "active",
    }


def test_property_date_and_datetime() -> None:
    config = _expand(
        """
        name: k
        entity_types:
          E:
            properties:
              d: date?
              dt: datetime
        """
    )
    props = config["entity_types"]["E"]["properties"]
    assert props["d"] == {"type": "date", "optional": True}
    assert props["dt"] == {"type": "datetime"}


def test_entity_level_id_becomes_primary_key() -> None:
    config = _expand(
        """
        name: k
        entity_types:
          E:
            id: e_id
            properties:
              field: string
        """
    )
    props = config["entity_types"]["E"]["properties"]
    assert props["e_id"] == {"type": "string", "primary_key": True}
    # Primary key emitted first.
    assert list(props.keys())[0] == "e_id"


def test_property_unknown_token_raises() -> None:
    with pytest.raises(CompactExpansionError):
        _expand(
            """
            name: k
            entity_types:
              E:
                properties:
                  field: string bogus
            """
        )


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

_REL_HEADER = """
name: k
entity_types:
  WorkItem:
    id: work_item_id
    properties:
      title: string indexed
  Actor:
    id: actor_id
    properties:
      name: string indexed
"""


def test_relationship_signature_expands_from_to() -> None:
    config = _expand(
        _REL_HEADER,
        """
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
        """,
    )
    rel = config["relationships"][0]
    assert rel["name"] == "work_item_owned_by_actor"
    assert rel["from"] == "WorkItem"
    assert rel["to"] == "Actor"


def test_relationship_trailing_comment_becomes_description() -> None:
    config = _expand(
        _REL_HEADER,
        """
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor   # Actor accountable for a work item.
        """,
    )
    assert config["relationships"][0]["description"] == "Actor accountable for a work item."


def test_relationship_block_description() -> None:
    config = _expand(
        _REL_HEADER,
        """
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
            description: >
              A multi-line
              description.
        """,
    )
    assert "multi-line" in config["relationships"][0]["description"]


def test_relationship_block_description_containing_arrow() -> None:
    # Regression: a block `description` whose text contains `->` must not be
    # mistaken for a second `name: From -> To` signature (_find_signature key set).
    config = _expand(
        _REL_HEADER,
        """
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
            description: Maps WorkItem -> Actor (the owner).
        """,
    )
    rel = config["relationships"][0]
    assert rel["name"] == "work_item_owned_by_actor"
    assert rel["from"] == "WorkItem"
    assert rel["to"] == "Actor"
    assert rel["description"] == "Maps WorkItem -> Actor (the owner)."


def test_relationship_self_evident_edge_has_no_description() -> None:
    config = _expand(
        _REL_HEADER,
        """
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
        """,
    )
    assert "description" not in config["relationships"][0]


def test_relationship_basis_adds_optional_string_property() -> None:
    config = _expand(
        _REL_HEADER,
        """
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
            basis: dependency_basis
        """,
    )
    props = config["relationships"][0]["properties"]
    assert props["dependency_basis"] == {"type": "string", "optional": True}


def test_relationship_proposal_policy_references_preset() -> None:
    config = _expand(
        _REL_HEADER,
        """
        presets:
          policies:
            standard:
              signals:
                source_evidence: {role: required, always_review_on_unsure: true}
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
            proposal_policy: standard
        """,
    )
    policy = config["relationships"][0]["proposal_policy"]
    assert policy["signals"]["source_evidence"] == {
        "role": "required",
        "always_review_on_unsure": True,
    }


def test_relationship_unknown_proposal_policy_raises() -> None:
    with pytest.raises(CompactExpansionError):
        _expand(
            _REL_HEADER,
            """
            relationships:
              - work_item_owned_by_actor: WorkItem -> Actor
                proposal_policy: nonexistent
            """,
        )


def test_relationship_omitted_policy_is_ungoverned() -> None:
    config = _expand(
        _REL_HEADER,
        """
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
        """,
    )
    assert "proposal_policy" not in config["relationships"][0]


def test_relationship_explicit_properties_block() -> None:
    config = _expand(
        _REL_HEADER,
        """
        enums:
          decision_impact_type: [blocks, scopes]
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
            properties: {impact_type: enum decision_impact_type}
        """,
    )
    props = config["relationships"][0]["properties"]
    assert props["impact_type"] == {"type": "string", "enum_ref": "decision_impact_type"}


# ---------------------------------------------------------------------------
# Traversal handles & direction inference
# ---------------------------------------------------------------------------

_QUERY_HEADER = """
name: k
entity_types:
  WorkItem:
    id: work_item_id
    properties:
      title: string indexed
      status: enum lifecycle_status
  Actor:
    id: actor_id
    properties:
      name: string indexed
enums:
  lifecycle_status: [active, closed]
relationships:
  - work_item_owned_by_actor: WorkItem -> Actor
  - work_item_depends_on_work_item: WorkItem -> WorkItem
"""


def test_direction_inferred_anchor_is_to_means_incoming() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: Actor
            returns: WorkItem
            traverse:
              - relationship: work_item_owned_by_actor
                as: work_item
        """,
    )
    step = config["named_queries"]["q"]["traversal"][0]
    # Actor == `to` of (WorkItem -> Actor) -> incoming.
    assert step["direction"] == "incoming"


def test_direction_inferred_anchor_is_from_means_outgoing() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: WorkItem
            returns: Actor
            traverse:
              - relationship: work_item_owned_by_actor
                as: owner
        """,
    )
    step = config["named_queries"]["q"]["traversal"][0]
    assert step["direction"] == "outgoing"


def test_self_ref_with_marker_out() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: WorkItem
            returns: WorkItem
            traverse:
              - relationship: work_item_depends_on_work_item>
                as: dep
        """,
    )
    step = config["named_queries"]["q"]["traversal"][0]
    assert step["relationship"] == "work_item_depends_on_work_item"
    assert step["direction"] == "outgoing"


def test_self_ref_with_marker_in() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: WorkItem
            returns: WorkItem
            traverse:
              - relationship: work_item_depends_on_work_item<
                as: dep
        """,
    )
    step = config["named_queries"]["q"]["traversal"][0]
    assert step["direction"] == "incoming"


def test_self_ref_without_marker_fails_loudly() -> None:
    """Invariant 3: ambiguous self-ref reference without >/< must raise."""
    with pytest.raises(CompactExpansionError, match="self-referential"):
        _expand(
            _QUERY_HEADER,
            """
            named_queries:
              q:
                mode: traversal
                entry_point: WorkItem
                returns: WorkItem
                traverse:
                  - relationship: work_item_depends_on_work_item
                    as: dep
            """,
        )


# ---------------------------------------------------------------------------
# Includes: all_adjacent, bound, named bounded set
# ---------------------------------------------------------------------------


def test_all_adjacent_includes_every_adjacent_edge() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: WorkItem
            returns: AnyEntity
            relationship_state: reviewable
            include: all_adjacent
        """,
    )
    includes = config["named_queries"]["q"]["include"]
    # work_item_owned_by_actor (WorkItem -> Actor, anchor=from -> outgoing).
    assert includes["work_item_owned_by_actor"]["direction"] == "outgoing"
    # Self-ref edge surfaces as _out and _in.
    assert "work_item_depends_on_work_item_out" in includes
    assert "work_item_depends_on_work_item_in" in includes
    assert includes["work_item_depends_on_work_item_out"]["direction"] == "outgoing"
    assert includes["work_item_depends_on_work_item_in"]["direction"] == "incoming"


def test_all_adjacent_does_not_alter_result_shape() -> None:
    """Invariant 2: all_adjacent only populates include; select/returns own the shape."""
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: WorkItem
            returns: AnyEntity
            relationship_state: reviewable
            include: all_adjacent
            select:
              properties: [work_item_id, title]
        """,
    )
    query = config["named_queries"]["q"]
    assert query["returns"] == "AnyEntity"
    # select owns the fields, not the include set.
    assert set(query["select"]) == {"work_item_id", "title"}
    # result_shape stays path (traversal default); include did not change it.
    assert query["result_shape"] == "path"


def test_named_bounded_include_set() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: Actor
            returns: WorkItem
            traverse:
              - relationship: work_item_owned_by_actor
                as: work_item
            include:
              latest:
                relationship: work_item_depends_on_work_item>
                limit: 1
                order: requested_at desc date
        """,
    )
    include = config["named_queries"]["q"]["include"]["latest"]
    assert include["relationship"] == "work_item_depends_on_work_item"
    assert include["limit"] == 1
    assert include["from"] == "$result"
    assert include["order_by"][0]["by"] == "$source.properties.requested_at"


def test_bound_caps_an_auto_include_set() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: WorkItem
            returns: AnyEntity
            relationship_state: reviewable
            include: all_adjacent
            bound:
              work_item_owned_by_actor:
                limit: 5
        """,
    )
    include = config["named_queries"]["q"]["include"]["work_item_owned_by_actor"]
    assert include["limit"] == 5


# ---------------------------------------------------------------------------
# Where scopes
# ---------------------------------------------------------------------------


def test_collection_where_targets_result() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: collection
            returns: WorkItem
            where: {status: {eq: active}}
        """,
    )
    assert config["named_queries"]["q"]["where"] == {"result.properties.status": {"eq": "active"}}


def test_traverse_where_targets_candidate() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: Actor
            returns: WorkItem
            traverse:
              - relationship: work_item_owned_by_actor
                as: work_item
                where: {status: {not_in: [closed]}}
        """,
    )
    step = config["named_queries"]["q"]["traversal"][0]
    assert step["where"] == {"candidate.properties.status": {"not_in": ["closed"]}}


# ---------------------------------------------------------------------------
# Select projection
# ---------------------------------------------------------------------------


def test_select_properties_and_pk() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: collection
            returns: WorkItem
            select:
              properties: [work_item_id, title, status]
        """,
    )
    select = config["named_queries"]["q"]["select"]
    assert select["work_item_id"] == "$result.entity_id"
    assert select["title"] == "$result.properties.title"
    assert select["status"] == "$result.properties.status"


def test_select_counts_list_and_alias() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: collection
            returns: WorkItem
            select:
              properties: [work_item_id]
              counts:
                owner: work_item_owned_by_actor
        """,
    )
    select = config["named_queries"]["q"]["select"]
    assert select["owner_count"] == "$include.work_item_owned_by_actor.count"


def test_select_items_alias() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: collection
            returns: WorkItem
            select:
              properties: [work_item_id]
              items:
                owners: work_item_owned_by_actor
        """,
    )
    select = config["named_queries"]["q"]["select"]
    assert select["owners"] == "$include.work_item_owned_by_actor.items"


def test_select_self_ref_count_must_be_aliased() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: collection
            returns: WorkItem
            select:
              properties: [work_item_id]
              counts:
                upstream: work_item_depends_on_work_item>
        """,
    )
    select = config["named_queries"]["q"]["select"]
    assert select["upstream_count"] == "$include.work_item_depends_on_work_item_out.count"


def test_select_self_ref_unaliased_raises() -> None:
    """A bare self-ref >/< in select (list form, no alias) must error."""
    with pytest.raises(CompactExpansionError, match="aliased"):
        _expand(
            _QUERY_HEADER,
            """
            named_queries:
              q:
                mode: collection
                returns: WorkItem
                select:
                  properties: [work_item_id]
                  counts: [work_item_depends_on_work_item>]
            """,
        )


def test_select_deep_projection_passes_through() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: Actor
            returns: WorkItem
            traverse:
              - relationship: work_item_owned_by_actor
                as: work_item
            include:
              latest:
                relationship: work_item_depends_on_work_item>
                limit: 1
            select:
              properties: [work_item_id]
              latest_id: $include.latest.items.0.source.entity_id
        """,
    )
    select = config["named_queries"]["q"]["select"]
    assert select["latest_id"] == "$include.latest.items.0.source.entity_id"


# ---------------------------------------------------------------------------
# Query knobs / defaults
# ---------------------------------------------------------------------------


def test_inert_defaults_filled_when_omitted() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: collection
            returns: WorkItem
        """,
    )
    query = config["named_queries"]["q"]
    assert query["result_shape"] == "entity"
    assert query["limit"] == 100


def test_traversal_inert_path_budget_defaults() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: WorkItem
            returns: AnyEntity
            relationship_state: reviewable
            include: all_adjacent
        """,
    )
    query = config["named_queries"]["q"]
    assert query["max_paths"] == 500
    assert query["max_paths_per_result"] == 50
    assert query["allow_relationship_state_override"] is True


# ---------------------------------------------------------------------------
# Order clause grammar
# ---------------------------------------------------------------------------


def test_order_clause_with_value_type() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: collection
            returns: WorkItem
            order: title asc string
        """,
    )
    order = config["named_queries"]["q"]["order_by"][0]
    assert order == {"by": "$result.properties.title", "direction": "asc", "value_type": "string"}


def test_order_clause_with_enum_ref() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: collection
            returns: WorkItem
            order: priority desc ^priority
        """,
    )
    order = config["named_queries"]["q"]["order_by"][0]
    assert order == {
        "by": "$result.properties.priority",
        "direction": "desc",
        "enum_ref": "priority",
    }


# ---------------------------------------------------------------------------
# Mutation guards
# ---------------------------------------------------------------------------

_GUARD_HEADER = (
    _QUERY_HEADER
    + """
named_queries:
  approved_reviews_for_work_item:
    mode: collection
    returns: WorkItem
"""
)


def test_guard_trigger_single_value() -> None:
    config = _expand(
        _GUARD_HEADER,
        """
        mutation_guards:
          - g:
              when: WorkItem.status -> closed
              require: {allowed_actors: [reviewer]}
        """,
    )
    guard = config["mutation_guards"][0]
    assert guard["entity_type"] == "WorkItem"
    assert guard["property"] == "status"
    assert guard["new_value"] == "closed"


def test_guard_trigger_value_list() -> None:
    config = _expand(
        _GUARD_HEADER,
        """
        mutation_guards:
          - g:
              when: WorkItem.status -> [closed, blocked]
              require: {allowed_actors: [reviewer]}
        """,
    )
    assert config["mutation_guards"][0]["new_value"] == ["closed", "blocked"]


def test_guard_cowrite_condition() -> None:
    config = _expand(
        _GUARD_HEADER,
        """
        mutation_guards:
          - g:
              when: WorkItem.status -> closed
              require:
                co_write: Actor via work_item_owned_by_actor
                kind: review_note
        """,
    )
    condition = config["mutation_guards"][0]["condition"]
    assert condition == {
        "type": "co_write",
        "requires": {
            "entity_type": "Actor",
            "via_relationship": "work_item_owned_by_actor",
            "kind": "review_note",
        },
    }


def test_guard_allowed_actors_literal_passthrough() -> None:
    """Invariant 4: allowed_actors -> allowed_actor_ids literal, no identity magic."""
    config = _expand(
        _GUARD_HEADER,
        """
        mutation_guards:
          - g:
              when: WorkItem.status -> closed
              require: {allowed_actors: [authorized-reviewer]}
        """,
    )
    condition = config["mutation_guards"][0]["condition"]
    assert condition == {
        "type": "actor",
        "allowed_actor_ids": ["authorized-reviewer"],
    }


def test_guard_query_condition() -> None:
    config = _expand(
        _GUARD_HEADER,
        """
        mutation_guards:
          - g:
              when: WorkItem.status -> closed
              require:
                query: approved_reviews_for_work_item
                params: {work_item_id: $entity.entity_id}
                min_count: 1
        """,
    )
    condition = config["mutation_guards"][0]["condition"]
    assert condition["type"] == "query"
    assert condition["query_name"] == "approved_reviews_for_work_item"
    assert condition["min_count"] == 1


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------


def test_quality_check_cardinality() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        quality_checks:
          - c:
              cardinality:
                entity: WorkItem
                relationship: work_item_owned_by_actor
                direction: out
                min: 1
              description: Work items should have an owner.
        """,
    )
    check = config["quality_checks"][0]
    assert check["kind"] == "cardinality"
    assert check["entity_type"] == "WorkItem"
    assert check["relationship_type"] == "work_item_owned_by_actor"
    assert check["direction"] == "outgoing"
    assert check["min_count"] == 1
    assert check["description"] == "Work items should have an owner."


def test_quality_check_property_non_empty() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        quality_checks:
          - c:
              property: work_item_depends_on_work_item.dependency_basis
              rule: non_empty
        """,
    )
    check = config["quality_checks"][0]
    assert check["kind"] == "property"
    assert check["target"] == "relationship"
    assert check["relationship_type"] == "work_item_depends_on_work_item"
    assert check["property"] == "dependency_basis"
    assert check["rule"] == "non_empty"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_query_template_expands_per_type() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          superseded_$T:
            for: [Decision, WorkItem]
            mode: collection
            returns: $T
            relationship_state: not-live
            description: $T retired.
        """,
    )
    queries = config["named_queries"]
    assert "superseded_decisions" in queries
    assert "superseded_work_items" in queries
    assert queries["superseded_decisions"]["returns"] == "Decision"
    assert queries["superseded_work_items"]["returns"] == "WorkItem"
    assert queries["superseded_decisions"]["description"] == "Decision retired."
    assert queries["superseded_decisions"]["relationship_state"] == "not-live"


# ---------------------------------------------------------------------------
# Invariant 5: presets / metadata stripped
# ---------------------------------------------------------------------------


def test_presets_and_metadata_are_stripped() -> None:
    result = expand_compact_full(
        _minimal_header(
            """
            metadata:
              requires_cruxible: ">=0.2"
            presets:
              policies:
                standard:
                  signals:
                    source_evidence: {role: required, always_review_on_unsure: true}
            entity_types:
              E:
                id: e_id
                properties:
                  field: string
            """
        )
    )
    assert "presets" not in result.config
    assert "metadata" not in result.config
    # metadata.requires_cruxible recorded separately.
    assert result.metadata == {"requires_cruxible": ">=0.2"}


# ---------------------------------------------------------------------------
# Invariant 1: deterministic + diff-stable output
# ---------------------------------------------------------------------------


def test_expansion_is_deterministic_byte_identical() -> None:
    text = DRAFT_PATH.read_text(encoding="utf-8")
    out1 = dump_expanded(expand_compact(text))
    out2 = dump_expanded(expand_compact(text))
    assert out1 == out2


def test_dump_is_stable_across_separate_loads() -> None:
    a = dump_expanded(expand_compact_file(DRAFT_PATH))
    b = dump_expanded(expand_compact_file(DRAFT_PATH))
    assert a == b


# ---------------------------------------------------------------------------
# End-to-end: the canonical draft validates as CoreConfig
# ---------------------------------------------------------------------------


def test_draft_expands_and_validates_as_core_config() -> None:
    result = expand_compact_file_full(DRAFT_PATH)
    # Must validate without error.
    CoreConfig.model_validate(result.config)


def test_draft_has_no_metadata_block() -> None:
    # Kit version/compat live in the manifest (cruxible-kit.yaml), not a config metadata block.
    result = expand_compact_file_full(DRAFT_PATH)
    assert result.metadata == {}
    assert "metadata" not in result.config
    assert "presets" not in result.config


def test_draft_all_adjacent_surfaces_self_ref_edges() -> None:
    config = expand_compact_file(DRAFT_PATH)
    includes = config["named_queries"]["work_item_context"]["include"]
    # Self-ref dependency edge appears with _out and _in keys.
    assert "work_item_depends_on_work_item_out" in includes
    assert "work_item_depends_on_work_item_in" in includes


def test_draft_actor_work_queue_aliased_counts() -> None:
    config = expand_compact_file(DRAFT_PATH)
    select = config["named_queries"]["actor_work_queue"]["select"]
    assert select["upstream_dependency_count"] == (
        "$include.work_item_depends_on_work_item_out.count"
    )
    assert select["blocking_risk_count"] == "$include.risk_blocks_work_item.count"


# --- Kit load-path: the loader expands the compact source ---------------------
# config.yaml is the single source of truth (compact); load_config expands it to the
# explicit CoreConfig on load -- there is NO committed expanded artifact. The manifest
# entry_config names the compact source directly.


def test_kit_manifest_entry_config_loads_as_valid_config() -> None:
    """The manifest's entry_config (the compact config.yaml) loads via load_config.

    load_config detects the compact grammar and expands it before validating, so the
    kit loads with no separate explicit artifact. materialize_kit/service_init/
    load_config all follow manifest.entry_config through this path.
    """
    import yaml as _yaml

    from cruxible_core.config.loader import load_config

    manifest = _yaml.safe_load((KIT_DIR / "cruxible-kit.yaml").read_text(encoding="utf-8"))
    assert manifest["entry_config"] == "config.yaml"  # the compact source, not an artifact
    entry = KIT_DIR / manifest["entry_config"]
    config = load_config(str(entry))  # compact -> expanded -> validated; must not raise
    assert config.name == "agent_operation"


def test_looks_compact_distinguishes_compact_from_explicit() -> None:
    """The loader's compact detector is True for the compact kit, False for explicit.

    Explicit (engine) configs must stay on the unchanged load path, so the detector
    must not fire on the explicit project-state kit config.
    """
    import yaml as _yaml

    from cruxible_core.config.compact import looks_compact

    compact = _yaml.safe_load((KIT_DIR / "config.yaml").read_text(encoding="utf-8"))
    assert looks_compact(compact) is True

    explicit = _yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "kits" / "project-state" / "config.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert looks_compact(explicit) is False
