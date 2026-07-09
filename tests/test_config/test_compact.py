"""Tests for the compact-config expander (``cruxible_core.config.compact``).

Covers every compact grammar/construct in the agent-operation compact source
(``kits/agent-operation/config.yaml``) and each of the 5 expander contract
invariants. (The docs/dev draft is a local commented reference of the same grammar.)
"""

from __future__ import annotations

import re
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
                source_evidence:
                  role: required
                  always_review_on_unsure: true
                  require_evidence_on_support: true
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
            proposal_policy: standard
        """,
    )
    policy = config["relationships"][0]["proposal_policy"]
    assert policy["signals"]["source_evidence"] == {
        "role": "required",
        "always_review_on_unsure": True,
        "require_evidence_on_support": True,
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


def test_relationship_write_policy_passes_through() -> None:
    config = _expand(
        _REL_HEADER,
        """
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
            write_policy: proposal_only
        """,
    )
    assert config["relationships"][0]["write_policy"] == "proposal_only"


def test_relationship_omitted_write_policy_absent() -> None:
    config = _expand(
        _REL_HEADER,
        """
        relationships:
          - work_item_owned_by_actor: WorkItem -> Actor
        """,
    )
    assert "write_policy" not in config["relationships"][0]


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
              properties: [title]
        """,
    )
    query = config["named_queries"]["q"]
    assert query["returns"] == "AnyEntity"
    # select owns the fields, not the include set.
    assert set(query["select"]) == {"entity_type", "entity_id", "title"}
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
# Explicit named-query escape hatch
# ---------------------------------------------------------------------------


def test_explicit_named_query_keys_without_marker_raise_with_marker_suggestion() -> None:
    with pytest.raises(CompactExpansionError) as excinfo:
        _expand(
            _QUERY_HEADER,
            """
            named_queries:
              q:
                mode: collection
                returns: WorkItem
                dedupe: path
            """,
        )

    message = str(excinfo.value)
    assert "query 'q': unsupported key 'dedupe'" in message
    assert "add 'explicit: true' to its body" in message


def test_explicit_named_query_marker_rejects_compact_only_keys() -> None:
    with pytest.raises(
        CompactExpansionError,
        match=(
            "query 'q': explicit body contains compact-grammar key 'order' "
            ".*remove 'explicit: true' or convert the body"
        ),
    ):
        _expand(
            _QUERY_HEADER,
            """
            named_queries:
              q:
                explicit: true
                mode: collection
                returns: WorkItem
                order:
                  - title asc string
            """,
        )


def test_explicit_named_query_marker_passes_through_and_is_stripped() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            explicit: true
            mode: traversal
            description: Explicit engine-schema query body.
            entry_point: Actor
            returns: WorkItem
            result_shape: path
            dedupe: path
            relationship_state: reviewable
            max_paths: 25
            traversal:
              - as: work_item
                relationship: work_item_owned_by_actor
                direction: incoming
            order_by:
              - by: $result.entity_id
                direction: asc
        """,
    )

    assert config["named_queries"]["q"] == {
        "mode": "traversal",
        "description": "Explicit engine-schema query body.",
        "entry_point": "Actor",
        "returns": "WorkItem",
        "result_shape": "path",
        "dedupe": "path",
        "relationship_state": "reviewable",
        "max_paths": 25,
        "traversal": [
            {
                "as": "work_item",
                "relationship": "work_item_owned_by_actor",
                "direction": "incoming",
            }
        ],
        "order_by": [{"by": "$result.entity_id", "direction": "asc"}],
    }


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
# Parity grammar extensions
# ---------------------------------------------------------------------------


def test_traverse_relationship_list_with_where_expands_to_explicit_step() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: WorkItem
            returns: AnyEntity
            traverse:
              - relationship: [work_item_owned_by_actor, work_item_depends_on_work_item]
                direction: outgoing
                as: context
                max_depth: 2
                where: {status: {not_in: [closed]}}
        """,
    )
    assert config["named_queries"]["q"]["traversal"][0] == {
        "relationship": ["work_item_owned_by_actor", "work_item_depends_on_work_item"],
        "direction": "outgoing",
        "as": "context",
        "where": {"candidate.properties.status": {"not_in": ["closed"]}},
        "max_depth": 2,
    }


def test_traversal_entity_result_shape_does_not_add_path_guards() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: Actor
            returns: WorkItem
            result_shape: entity
            traverse:
              - relationship: work_item_owned_by_actor
                as: work_item
        """,
    )
    query = config["named_queries"]["q"]
    assert query == {
        "mode": "traversal",
        "entry_point": "Actor",
        "returns": "WorkItem",
        "result_shape": "entity",
        "traversal": [
            {"relationship": "work_item_owned_by_actor", "direction": "incoming", "as": "work_item"}
        ],
    }


def test_named_include_from_entry_and_result_expand_to_explicit_includes() -> None:
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
              entry_work:
                from: $entry
                relationship: work_item_owned_by_actor
              result_deps:
                from: $result
                relationship: work_item_depends_on_work_item>
        """,
    )
    includes = config["named_queries"]["q"]["include"]
    assert includes["entry_work"] == {
        "from": "$entry",
        "relationship": "work_item_owned_by_actor",
        "direction": "incoming",
        "many": True,
    }
    assert includes["result_deps"] == {
        "from": "$result",
        "relationship": "work_item_depends_on_work_item",
        "direction": "outgoing",
        "many": True,
    }


def test_named_include_direction_override_expands_without_anchor_inference() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: WorkItem
            returns: AnyEntity
            traverse:
              - relationship: work_item_owned_by_actor
                direction: outgoing
                as: owner
            include:
              owner:
                from: $result
                relationship: work_item_owned_by_actor
                direction: outgoing
        """,
    )
    assert config["named_queries"]["q"]["include"]["owner"] == {
        "from": "$result",
        "relationship": "work_item_owned_by_actor",
        "direction": "outgoing",
        "many": True,
    }


def test_direction_override_allows_base_relationship_refs_in_overlays() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        named_queries:
          q:
            mode: traversal
            entry_point: WorkItem
            returns: AnyEntity
            traverse:
              - relationship: base_supplied_relationship
                direction: outgoing
                as: base_path
            include:
              base_context:
                from: $result
                relationship: base_supplied_relationship
                direction: incoming
        """,
    )
    query = config["named_queries"]["q"]
    assert query["traversal"][0]["relationship"] == "base_supplied_relationship"
    assert query["include"]["base_context"] == {
        "from": "$result",
        "relationship": "base_supplied_relationship",
        "direction": "incoming",
        "many": True,
    }


def test_named_include_scoped_where_path_passes_through() -> None:
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
                where:
                  target.properties.status: {eq: active}
        """,
    )
    assert config["named_queries"]["q"]["include"]["latest"]["where"] == {
        "target.properties.status": {"eq": "active"}
    }


def test_required_on_traverse_and_named_include_passes_through() -> None:
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
                required: false
            include:
              latest:
                relationship: work_item_depends_on_work_item>
                required: true
        """,
    )
    query = config["named_queries"]["q"]
    assert query["traversal"][0] == {
        "relationship": "work_item_owned_by_actor",
        "direction": "incoming",
        "as": "work_item",
        "required": False,
    }
    assert query["include"]["latest"] == {
        "from": "$result",
        "relationship": "work_item_depends_on_work_item",
        "direction": "outgoing",
        "many": True,
        "required": True,
    }


def test_quality_check_severity_passes_through() -> None:
    config = _expand(
        _QUERY_HEADER,
        """
        quality_checks:
          - relationship_basis_required:
              property: work_item_depends_on_work_item.dependency_basis
              rule: non_empty
              severity: error
          - work_items_have_owner:
              cardinality:
                entity: WorkItem
                relationship: work_item_owned_by_actor
                direction: out
                min: 1
              severity: warning
        """,
    )
    assert config["quality_checks"] == [
        {
            "name": "relationship_basis_required",
            "kind": "property",
            "target": "relationship",
            "relationship_type": "work_item_depends_on_work_item",
            "property": "dependency_basis",
            "rule": "non_empty",
            "severity": "error",
        },
        {
            "name": "work_items_have_owner",
            "kind": "cardinality",
            "entity_type": "WorkItem",
            "relationship_type": "work_item_owned_by_actor",
            "direction": "outgoing",
            "severity": "warning",
            "min_count": 1,
        },
    ]


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
    assert list(select) == ["work_item_id", "title", "status"]
    assert select["work_item_id"] == "$result.entity_id"
    assert select["title"] == "$result.properties.title"
    assert select["status"] == "$result.properties.status"


def test_select_anyentity_auto_identity_and_no_pk_binding() -> None:
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
    select = config["named_queries"]["q"]["select"]
    assert list(select)[:2] == ["entity_type", "entity_id"]
    assert select["entity_type"] == "$result.entity_type"
    assert select["entity_id"] == "$result.entity_id"
    assert select["work_item_id"] == "$result.properties.work_item_id"
    assert select["title"] == "$result.properties.title"


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


# ---------------------------------------------------------------------------
# Fail-closed rejection
# ---------------------------------------------------------------------------

_FAIL_CLOSED_REJECTION_CASES = [
    pytest.param(
        _join(
            """
            name: k
            entity_types: {}
            unsupported_top: true
            """
        ),
        "compact config",
        "unsupported_top",
        id="top-level-config-key",
    ),
    pytest.param(
        _join(
            """
            name: k
            enums:
              priority:
                values: [low]
                unsupported_enum: true
            entity_types: {}
            """
        ),
        "enum 'priority'",
        "unsupported_enum",
        id="enum-block",
    ),
    pytest.param(
        _join(
            """
            name: k
            entity_types:
              E:
                id: e_id
                unsupported_entity: true
            """
        ),
        "entity 'E'",
        "unsupported_entity",
        id="entity-body",
    ),
    pytest.param(
        _join(
            _REL_HEADER,
            """
            relationships:
              - work_item_owned_by_actor: WorkItem -> Actor
                unsupported_relationship: true
            """,
        ),
        "relationship 'work_item_owned_by_actor'",
        "unsupported_relationship",
        id="relationship-item",
    ),
    pytest.param(
        _join(
            _REL_HEADER,
            """
            relationships:
              - work_item_owned_by_actor: WorkItem -> Actor
                proposal_policy:
                  signals: {}
                  unsupported_policy: true
            """,
        ),
        "proposal policy",
        "unsupported_policy",
        id="proposal-policy",
    ),
    pytest.param(
        _join(
            _QUERY_HEADER,
            """
            named_queries:
              q:
                mode: collection
                returns: WorkItem
                unsupported_query: true
            """,
        ),
        "query 'q'",
        "unsupported_query",
        id="named-query-body",
    ),
    pytest.param(
        _join(
            _QUERY_HEADER,
            """
            named_queries:
              by_$T:
                for: [WorkItem]
                mode: collection
                returns: $T
                unsupported_template: true
            """,
        ),
        "query template 'by_$T'",
        "unsupported_template",
        id="query-template-body",
    ),
    pytest.param(
        _join(
            _QUERY_HEADER,
            """
            named_queries:
              q:
                mode: traversal
                entry_point: Actor
                returns: WorkItem
                traverse:
                  - relationship: work_item_owned_by_actor
                    unsupported_traverse: true
            """,
        ),
        "query 'q' traverse step",
        "unsupported_traverse",
        id="traverse-step",
    ),
    pytest.param(
        _join(
            _QUERY_HEADER,
            """
            named_queries:
              q:
                mode: collection
                returns: WorkItem
                include:
                  owner:
                    relationship: work_item_owned_by_actor
                    unsupported_include: true
            """,
        ),
        "query 'q' include 'owner'",
        "unsupported_include",
        id="named-include-body",
    ),
    pytest.param(
        _join(
            _QUERY_HEADER,
            """
            named_queries:
              q:
                mode: collection
                returns: WorkItem
                bound:
                  work_item_owned_by_actor:
                    unsupported_bound: true
            """,
        ),
        "query 'q' bound 'work_item_owned_by_actor'",
        "unsupported_bound",
        id="bound-cap",
    ),
    pytest.param(
        _join(
            """
            name: k
            mutation_guards:
              - g:
                  when: WorkItem.status -> closed
                  require: {allowed_actors: [reviewer]}
                  unsupported_guard: true
            """
        ),
        "mutation guard 'g'",
        "unsupported_guard",
        id="mutation-guard-body",
    ),
    pytest.param(
        _join(
            """
            name: k
            mutation_guards:
              - g:
                  when: WorkItem.status -> closed
                  require:
                    co_write: Actor via work_item_owned_by_actor
                    unsupported_cowrite: true
            """
        ),
        "mutation guard 'g' require co_write",
        "unsupported_cowrite",
        id="guard-require-co-write",
    ),
    pytest.param(
        _join(
            """
            name: k
            mutation_guards:
              - g:
                  when: WorkItem.status -> closed
                  require:
                    allowed_actors: [reviewer]
                    unsupported_allowed_actors: true
            """
        ),
        "mutation guard 'g' require allowed_actors",
        "unsupported_allowed_actors",
        id="guard-require-allowed-actors",
    ),
    pytest.param(
        _join(
            """
            name: k
            mutation_guards:
              - g:
                  when: WorkItem.status -> closed
                  require:
                    query: approved_reviews_for_work_item
                    unsupported_query_guard: true
            """
        ),
        "mutation guard 'g' require query",
        "unsupported_query_guard",
        id="guard-require-query",
    ),
    pytest.param(
        _join(
            """
            name: k
            quality_checks:
              - c:
                  cardinality:
                    entity: WorkItem
                    relationship: work_item_owned_by_actor
                    direction: out
                  unsupported_quality_cardinality: true
            """
        ),
        "quality check 'c'",
        "unsupported_quality_cardinality",
        id="quality-check-cardinality-body",
    ),
    pytest.param(
        _join(
            """
            name: k
            quality_checks:
              - c:
                  property: work_item_depends_on_work_item.dependency_basis
                  rule: non_empty
                  unsupported_quality_property: true
            """
        ),
        "quality check 'c'",
        "unsupported_quality_property",
        id="quality-check-property-body",
    ),
    pytest.param(
        _join(
            """
            name: k
            quality_checks:
              - c:
                  cardinality:
                    entity: WorkItem
                    relationship: work_item_owned_by_actor
                    direction: out
                    unsupported_cardinality: true
            """
        ),
        "quality check 'c' cardinality",
        "unsupported_cardinality",
        id="cardinality-sub-mapping",
    ),
    pytest.param(
        _join(
            _QUERY_HEADER,
            """
            named_queries:
              q:
                mode: collection
                returns: WorkItem
                order: title asc string unsupported_order_token
            """,
        ),
        "order clause 'title asc string unsupported_order_token'",
        "unsupported_order_token",
        id="order-clause-extra-token",
    ),
]


@pytest.mark.parametrize(
    ("source", "construct_label", "offending_key"),
    _FAIL_CLOSED_REJECTION_CASES,
)
def test_compact_expander_rejects_unknown_keys_fail_closed(
    source: str, construct_label: str, offending_key: str
) -> None:
    match = rf"{re.escape(construct_label)}.*{re.escape(offending_key)}"
    with pytest.raises(CompactExpansionError, match=match):
        expand_compact(source)


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


def test_guard_where_passthrough() -> None:
    config = _expand(
        _GUARD_HEADER,
        """
        mutation_guards:
          - g:
              when: WorkItem.status -> closed
              require: {allowed_actors: [reviewer]}
              where:
                candidate.properties.type: {eq: research}
        """,
    )
    guard = config["mutation_guards"][0]
    assert guard["where"] == {"candidate.properties.type": {"eq": "research"}}


def test_guard_where_related_passthrough() -> None:
    config = _expand(
        _GUARD_HEADER,
        """
        mutation_guards:
          - g:
              when: WorkItem.status -> closed
              require: {allowed_actors: [reviewer]}
              where_related:
                - relationship: work_item_owned_by_actor
                  direction: outgoing
              where_not_related:
                - relationship: work_item_blocked_by
                  direction: outgoing
                  target: {properties.status: {eq: open}}
        """,
    )
    guard = config["mutation_guards"][0]
    assert guard["where_related"] == [
        {"relationship": "work_item_owned_by_actor", "direction": "outgoing"}
    ]
    assert guard["where_not_related"] == [
        {
            "relationship": "work_item_blocked_by",
            "direction": "outgoing",
            "target": {"properties.status": {"eq": "open"}},
        }
    ]


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
                    source_evidence:
                      role: required
                      always_review_on_unsure: true
                      require_evidence_on_support: true
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
    must not fire on the explicit kev-reference kit config.
    """
    import yaml as _yaml

    from cruxible_core.config.compact import looks_compact

    compact = _yaml.safe_load((KIT_DIR / "config.yaml").read_text(encoding="utf-8"))
    assert looks_compact(compact) is True

    explicit = {
        "version": "1.0",
        "name": "explicit_fixture",
        "entity_types": {
            "Thing": {
                "properties": {
                    "thing_id": {"primary_key": True},
                    "label": {"indexed": True},
                }
            }
        },
        "relationships": [{"name": "thing_related_to_thing", "from": "Thing", "to": "Thing"}],
    }
    assert looks_compact(explicit) is False


# ---------------------------------------------------------------------------
# Grammar v2 fixes (2026-07-04): scalar coverage, optional token, chained
# traverse inference, entity property checks
# ---------------------------------------------------------------------------


def test_property_scalar_covers_all_property_types() -> None:
    config = _expand(
        _REL_HEADER,
        """
        entity_types:
          Thing:
            id: thing_id
            properties:
              flag: bool
              score: number?
              count: int
              ratio: float
              blob: json?
        """,
    )
    props = config["entity_types"]["Thing"]["properties"]
    assert props["flag"] == {"type": "bool"}
    assert props["score"] == {"type": "number", "optional": True}
    assert props["count"] == {"type": "int"}
    assert props["ratio"] == {"type": "float"}
    assert props["blob"] == {"type": "json", "optional": True}


def test_optional_token_is_flow_map_safe() -> None:
    config = _expand(
        _REL_HEADER,
        """
        enums:
          judge_role: [author, dissent]
        entity_types:
          Thing:
            id: thing_id
            properties: {role: enum judge_role optional, note: string optional}
        """,
    )
    props = config["entity_types"]["Thing"]["properties"]
    assert props["role"] == {"type": "string", "enum_ref": "judge_role", "optional": True}
    assert props["note"] == {"type": "string", "optional": True}


def test_traverse_direction_chains_through_hops() -> None:
    config = _expand(
        "",
        """
        entity_types:
          Client: {id: client_id, properties: {name: string}}
          Matter: {id: matter_id, properties: {name: string}}
          Opinion: {id: opinion_id, properties: {name: string}}
        relationships:
          - matter_for_client: Matter -> Client
          - opinion_affects_matter: Opinion -> Matter
        named_queries:
          client_impact:
            mode: traversal
            entry_point: Client
            returns: Opinion
            traverse:
              - relationship: matter_for_client
                as: client_matter
              - relationship: opinion_affects_matter
                as: impacting_opinion
        """,
    )
    steps = config["named_queries"]["client_impact"]["traversal"]
    assert steps[0]["direction"] == "incoming"
    # Second hop anchors on Matter (hop 1's landing), not on Client.
    assert steps[1]["direction"] == "incoming"


def test_traverse_ambiguous_landing_requires_direction() -> None:
    import pytest

    with pytest.raises(CompactExpansionError, match="direction cannot be inferred"):
        _expand(
            "",
            """
            entity_types:
              A: {id: a_id, properties: {name: string}}
              B: {id: b_id, properties: {name: string}}
              C: {id: c_id, properties: {name: string}}
            relationships:
              - a_to_b: A -> B
              - b_to_b: B -> B
              - b_to_c: B -> C
            named_queries:
              chained:
                mode: traversal
                entry_point: A
                returns: C
                traverse:
                  - relationship: [a_to_b, b_to_b]
                    direction: outgoing
                    as: fan
                  - relationship: b_to_c
                    as: landing
            """,
        )


def test_entity_property_check_expands_to_entity_target() -> None:
    config = _expand(
        _REL_HEADER,
        """
        quality_checks:
          - things_have_kind:
              property: WorkItem.title
              rule: required
              severity: error
        """,
    )
    check = config["quality_checks"][0]
    assert check["kind"] == "property"
    assert check["target"] == "entity"
    assert check["entity_type"] == "WorkItem"
    assert check["property"] == "title"
    assert check["rule"] == "required"
