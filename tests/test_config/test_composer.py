"""Tests for config composition (base + overlay merge)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.config.composer import (
    ResolvedConfigLayer,
    compose_config_files,
    compose_config_sequence,
    compose_configs,
    compose_runtime_configs,
    resolve_config_layer_sequence,
    resolve_config_layers,
    write_composed_config,
)
from cruxible_core.config.loader import load_config, load_config_from_string
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError

_BASE_YAML = {
    "version": "1.0",
    "name": "base",
    "entity_types": {
        "Case": {
            "properties": {
                "case_id": {"type": "string", "primary_key": True},
            },
        },
    },
    "relationships": [
        {"name": "cites", "from": "Case", "to": "Case"},
    ],
}


def _base() -> CoreConfig:
    return CoreConfig.model_validate(_BASE_YAML)


def _overlay(extra: dict) -> CoreConfig:
    data = {
        "version": "1.0",
        "name": "overlay",
        "extends": "base.yaml",
        "entity_types": {},
        "relationships": [],
        **extra,
    }
    return CoreConfig.model_validate(data)


_COMPACT_ALL_ADJACENT_BASE = """\
version: "1.0"
name: compact_base
entity_types:
  AnyEntity:
    properties:
      entity_id:
        type: string
        primary_key: true
  WorkItem:
    properties:
      work_item_id:
        type: string
        primary_key: true
  Actor:
    properties:
      actor_id:
        type: string
        primary_key: true
relationships:
  - work_item_owned_by_actor: WorkItem -> Actor
named_queries:
  work_item_context:
    mode: traversal
    entry_point: WorkItem
    returns: AnyEntity
    relationship_state: reviewable
    include: all_adjacent
"""

_COMPACT_ALL_ADJACENT_OVERLAY = """\
version: "1.0"
name: compact_overlay
extends: base.yaml
entity_types:
  ReleaseLine:
    properties:
      release_line_id:
        type: string
        primary_key: true
relationships:
  - work_item_targets_release_line: WorkItem -> ReleaseLine
named_queries:
  release_work_item_context:
    mode: traversal
    entry_point: WorkItem
    returns: AnyEntity
    relationship_state: reviewable
    include: all_adjacent
"""


class TestAllAdjacentComposition:
    def test_standalone_all_adjacent_expansion_is_unchanged(self) -> None:
        config = load_config_from_string(_COMPACT_ALL_ADJACENT_BASE)
        query = config.named_queries["work_item_context"]

        assert query.traversal[0].relationship == ["work_item_owned_by_actor"]
        assert set(query.include) == {"work_item_owned_by_actor"}
        assert query.include["work_item_owned_by_actor"].direction == "outgoing"

    def test_base_all_adjacent_query_includes_overlay_relationship(self) -> None:
        base = load_config_from_string(_COMPACT_ALL_ADJACENT_BASE)
        overlay = load_config_from_string(_COMPACT_ALL_ADJACENT_OVERLAY)

        composed = compose_configs(base, overlay)
        query = composed.named_queries["work_item_context"]

        assert query.traversal[0].relationship == [
            "work_item_owned_by_actor",
            "work_item_targets_release_line",
        ]
        assert "work_item_targets_release_line" in query.include
        assert query.include["work_item_targets_release_line"].direction == "outgoing"

    def test_compact_overlay_all_adjacent_expands_once_after_composition(self) -> None:
        base = load_config_from_string(_COMPACT_ALL_ADJACENT_BASE)
        overlay = load_config_from_string(_COMPACT_ALL_ADJACENT_OVERLAY)

        composed = compose_configs(base, overlay)
        query = composed.named_queries["release_work_item_context"]
        relationships = query.traversal[0].relationship

        assert relationships == [
            "work_item_owned_by_actor",
            "work_item_targets_release_line",
        ]
        assert len(relationships) == len(set(relationships))
        assert set(query.include) == set(relationships)


class TestSequenceComposition:
    def test_two_layer_sequence_matches_pairwise_compose(self) -> None:
        base = _base()
        overlay = _overlay(
            {
                "feedback_profiles": {
                    "cites": {"version": 1, "reason_codes": {}, "scope_keys": {}},
                },
            }
        )

        pairwise = compose_configs(base, overlay)
        sequence = compose_config_sequence(
            [
                ResolvedConfigLayer(config=base),
                ResolvedConfigLayer(config=overlay),
            ]
        )

        assert sequence.model_dump(mode="python") == pairwise.model_dump(mode="python")

    def test_runtime_sequence_matches_pairwise_runtime_compose(self) -> None:
        base = load_config_from_string(
            """\
version: "1.0"
name: base
entity_types:
  Vendor:
    properties:
      vendor_id:
        type: string
        primary_key: true
relationships: []
contracts:
  EmptyInput:
    fields: {}
  BundleRows:
    fields:
      items:
        type: json
artifacts:
  canonical_bundle:
    kind: directory
    uri: ./bundle
    digest: sha256:bundle
providers:
  reference_loader:
    kind: function
    contract_in: EmptyInput
    contract_out: BundleRows
    ref: tests.support.workflow_test_providers.reference_bundle_loader
    version: 1.0.0
    deterministic: true
    runtime: python
    artifact: canonical_bundle
workflows:
  build_reference:
    type: canonical
    contract_in: EmptyInput
    steps:
      - id: rows
        provider: reference_loader
        input: {}
        as: rows
    returns: rows
"""
        )
        overlay = load_config_from_string(
            """\
version: "1.0"
name: overlay
extends: base.yaml
entity_types: {}
relationships: []
named_queries:
  vendor_index:
    mode: collection
    returns: Vendor
    result_shape: entity
"""
        )

        pairwise = compose_runtime_configs(base, overlay)
        sequence = compose_config_sequence(
            [
                ResolvedConfigLayer(config=base),
                ResolvedConfigLayer(config=overlay),
            ],
            runtime=True,
        )

        assert sequence.model_dump(mode="python") == pairwise.model_dump(mode="python")
        assert "build_reference" not in sequence.workflows
        assert "reference_loader" not in sequence.providers


class TestRecursiveLayerResolution:
    @staticmethod
    def _write_layer(
        path: Path,
        *,
        name: str,
        entity_type: str,
        extends: str | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        extends_line = f"extends: {extends}\n" if extends is not None else ""
        path.write_text(
            "version: '1.0'\n"
            f"name: {name}\n"
            f"{extends_line}"
            "entity_types:\n"
            f"  {entity_type}:\n"
            "    properties:\n"
            f"      {entity_type.lower()}_id: {{type: string, primary_key: true}}\n"
            "relationships: []\n"
        )

    def test_resolves_three_layer_extends_chain_base_first(self, tmp_path: Path) -> None:
        base = tmp_path / "base" / "config.yaml"
        domain = tmp_path / "domain" / "config.yaml"
        overlay = tmp_path / "overlay" / "config.yaml"
        self._write_layer(base, name="base", entity_type="Actor")
        self._write_layer(
            domain,
            name="domain",
            entity_type="WorkItem",
            extends="../base/config.yaml",
        )
        self._write_layer(
            overlay,
            name="overlay",
            entity_type="Strategy",
            extends="../domain/config.yaml",
        )

        layers = resolve_config_layers(load_config(overlay), config_path=overlay)
        composed = compose_config_sequence(layers)

        assert [layer.config_path for layer in layers] == [
            base.resolve(),
            domain.resolve(),
            overlay.resolve(),
        ]
        assert list(composed.entity_types) == ["Actor", "WorkItem", "Strategy"]

    def test_multiple_roots_deduplicate_shared_base_at_first_position(self, tmp_path: Path) -> None:
        base = tmp_path / "base" / "config.yaml"
        first = tmp_path / "first" / "config.yaml"
        second = tmp_path / "second" / "config.yaml"
        self._write_layer(base, name="base", entity_type="Actor")
        self._write_layer(
            first,
            name="first",
            entity_type="WorkItem",
            extends="../base/config.yaml",
        )
        self._write_layer(
            second,
            name="second",
            entity_type="Strategy",
            extends="../base/config.yaml",
        )

        layers = resolve_config_layer_sequence(
            [
                ResolvedConfigLayer(config=load_config(first), config_path=first),
                ResolvedConfigLayer(config=load_config(second), config_path=second),
            ]
        )

        assert [layer.config_path for layer in layers] == [
            base.resolve(),
            first,
            second,
        ]
        composed = compose_config_sequence(layers)
        assert list(composed.entity_types) == ["Actor", "WorkItem", "Strategy"]

    def test_recursive_parent_prefers_explicit_transformed_root(self, tmp_path: Path) -> None:
        base = tmp_path / "base" / "config.yaml"
        child = tmp_path / "child" / "config.yaml"
        self._write_layer(base, name="base", entity_type="Actor")
        self._write_layer(
            child,
            name="child",
            entity_type="WorkItem",
            extends="../base/config.yaml",
        )
        transformed_base = load_config(base)
        transformed_base.name = "namespaced-base"

        layers = resolve_config_layer_sequence(
            [
                ResolvedConfigLayer(config=load_config(child), config_path=child),
                ResolvedConfigLayer(config=transformed_base, config_path=base),
            ]
        )

        assert [layer.config.name for layer in layers] == ["namespaced-base", "child"]

    def test_conflicting_explicit_roots_for_same_path_are_rejected(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        self._write_layer(base, name="base", entity_type="Actor")
        transformed = load_config(base)
        transformed.name = "transformed"

        with pytest.raises(ConfigError, match="supplied with conflicting in-memory content"):
            resolve_config_layer_sequence(
                [
                    ResolvedConfigLayer(config=load_config(base), config_path=base),
                    ResolvedConfigLayer(config=transformed, config_path=base),
                ]
            )

    def test_cycle_error_prints_the_resolved_chain(self, tmp_path: Path) -> None:
        first = tmp_path / "first.yaml"
        second = tmp_path / "second.yaml"
        self._write_layer(first, name="first", entity_type="Actor", extends="second.yaml")
        self._write_layer(second, name="second", entity_type="WorkItem", extends="first.yaml")

        with pytest.raises(ConfigError) as exc_info:
            resolve_config_layers(load_config(first), config_path=first)

        assert str(exc_info.value) == (
            "Config extends cycle detected: "
            f"{first.resolve()} -> {second.resolve()} -> {first.resolve()}"
        )

    def test_two_layer_resolution_matches_explicit_pair(self, tmp_path: Path) -> None:
        base_path = tmp_path / "base.yaml"
        overlay_path = tmp_path / "overlay.yaml"
        self._write_layer(base_path, name="base", entity_type="Actor")
        self._write_layer(
            overlay_path,
            name="overlay",
            entity_type="WorkItem",
            extends="base.yaml",
        )
        base = load_config(base_path)
        overlay = load_config(overlay_path)

        recursive = compose_config_sequence(
            resolve_config_layers(overlay, config_path=overlay_path)
        )
        explicit = compose_configs(
            base,
            overlay,
            base_config_path=base_path,
            overlay_config_path=overlay_path,
        )

        assert recursive.model_dump(mode="python") == explicit.model_dump(mode="python")


class TestSequenceCompositionValidation:
    def test_compose_validates_semantic_output_by_default(self) -> None:
        base = _base()
        overlay = _overlay(
            {
                "named_queries": {
                    "bad_query": {
                        "mode": "traversal",
                        "entry_point": "Case",
                        "traversal": [
                            {"relationship": "missing_relationship", "direction": "outgoing"}
                        ],
                        "returns": "Case",
                    },
                },
            }
        )

        with pytest.raises(ConfigError, match="Named query 'bad_query'"):
            compose_configs(base, overlay)

    def test_composed_auth_managed_type_requires_mint_only_policy(self) -> None:
        base = _base()
        overlay = _overlay(
            {
                "entity_types": {
                    "Principal": {
                        "auth_managed": True,
                        "properties": {
                            "actor_id": {"type": "string", "primary_key": True},
                            "kind": {"type": "string"},
                        },
                    }
                }
            }
        )

        with pytest.raises(
            ValidationError,
            match="Auth-managed entity type 'Principal' must declare write_policy: mint_only",
        ):
            compose_configs(base, overlay)

    def test_compose_validate_false_allows_raw_merge_inspection(self) -> None:
        base = _base()
        overlay = _overlay(
            {
                "named_queries": {
                    "bad_query": {
                        "mode": "traversal",
                        "entry_point": "Case",
                        "traversal": [
                            {"relationship": "missing_relationship", "direction": "outgoing"}
                        ],
                        "returns": "Case",
                    },
                },
            }
        )

        composed = compose_configs(base, overlay, validate=False)

        assert "bad_query" in composed.named_queries

    def test_inline_layer_can_resolve_extends_from_config_dir(self, tmp_path: Path) -> None:
        (tmp_path / "base.yaml").write_text(
            """\
version: "1.0"
name: base
entity_types:
  Case:
    properties:
      case_id: {type: string, primary_key: true}
relationships:
  - name: cites
    from: Case
    to: Case
"""
        )
        overlay = load_config_from_string(
            """\
version: "1.0"
name: overlay
extends: base.yaml
entity_types: {}
relationships:
  - name: follows
    from: Case
    to: Case
"""
        )

        composed = compose_config_sequence(resolve_config_layers(overlay, config_dir=tmp_path))

        assert "Case" in composed.entity_types
        assert composed.get_relationship("cites") is not None
        assert composed.get_relationship("follows") is not None


# --- feedback_profiles (keyed-map merge) ---


class TestEnumComposition:
    def test_overlay_adds_new_enum(self) -> None:
        base = _base()
        overlay = _overlay(
            {
                "enums": {
                    "case_status": {
                        "values": ["open", "closed"],
                        "description": "Case lifecycle",
                    },
                },
            }
        )
        composed = compose_configs(base, overlay)
        assert composed.enums["case_status"].values == ["open", "closed"]

    def test_overlay_cannot_redefine_base_enum(self) -> None:
        base_data = {
            **_BASE_YAML,
            "enums": {"case_status": {"values": ["open", "closed"]}},
        }
        base = CoreConfig.model_validate(base_data)
        overlay = _overlay(
            {
                "enums": {"case_status": {"values": ["pending", "resolved"]}},
            }
        )
        with pytest.raises(ConfigError, match="redefine upstream.*enums.*case_status"):
            compose_configs(base, overlay)


class TestFeedbackProfilesComposition:
    def test_overlay_adds_new_feedback_profile(self) -> None:
        overlay = _overlay(
            {
                "feedback_profiles": {
                    "cites": {
                        "version": 1,
                        "reason_codes": {
                            "bad_cite": {
                                "description": "Citation is wrong",
                                "remediation_hint": "constraint",
                            },
                        },
                        "scope_keys": {},
                    },
                },
            }
        )
        composed = compose_configs(_base(), overlay)
        assert "cites" in composed.feedback_profiles
        assert "bad_cite" in composed.feedback_profiles["cites"].reason_codes

    def test_overlay_cannot_redefine_base_feedback_profile(self) -> None:
        base = _base()
        base_data = _BASE_YAML.copy()
        base_data["feedback_profiles"] = {
            "cites": {"version": 1, "reason_codes": {}, "scope_keys": {}},
        }
        base = CoreConfig.model_validate(base_data)

        overlay = _overlay(
            {
                "feedback_profiles": {
                    "cites": {"version": 2, "reason_codes": {}, "scope_keys": {}},
                },
            }
        )
        with pytest.raises(ConfigError, match="redefine upstream.*feedback_profiles.*cites"):
            compose_configs(base, overlay)

    def test_both_base_and_overlay_feedback_profiles_merged(self) -> None:
        base_data = {
            **_BASE_YAML,
            "feedback_profiles": {
                "cites": {"version": 1, "reason_codes": {}, "scope_keys": {}},
            },
        }
        base = CoreConfig.model_validate(base_data)

        overlay = _overlay(
            {
                "relationships": [
                    {"name": "follows", "from": "Case", "to": "Case"},
                ],
                "feedback_profiles": {
                    "follows": {"version": 1, "reason_codes": {}, "scope_keys": {}},
                },
            }
        )
        composed = compose_configs(base, overlay)
        assert "cites" in composed.feedback_profiles
        assert "follows" in composed.feedback_profiles


# --- outcome_profiles (keyed-map merge) ---


class TestOutcomeProfilesComposition:
    def test_overlay_adds_new_outcome_profile(self) -> None:
        overlay = _overlay(
            {
                "outcome_profiles": {
                    "cites_resolution": {
                        "anchor_type": "resolution",
                        "relationship_type": "cites",
                        "version": 1,
                        "outcome_codes": {},
                        "scope_keys": {},
                    },
                },
            }
        )
        composed = compose_configs(_base(), overlay)
        assert "cites_resolution" in composed.outcome_profiles

    def test_overlay_cannot_redefine_base_outcome_profile(self) -> None:
        base_data = {
            **_BASE_YAML,
            "outcome_profiles": {
                "cites_resolution": {
                    "anchor_type": "resolution",
                    "relationship_type": "cites",
                    "version": 1,
                },
            },
        }
        base = CoreConfig.model_validate(base_data)

        overlay = _overlay(
            {
                "outcome_profiles": {
                    "cites_resolution": {
                        "anchor_type": "resolution",
                        "relationship_type": "cites",
                        "version": 2,
                    },
                },
            }
        )
        with pytest.raises(
            ConfigError,
            match="redefine upstream.*outcome_profiles.*cites_resolution",
        ):
            compose_configs(base, overlay)


# --- decision_policies (safe-list append) ---


class TestDecisionPoliciesComposition:
    def test_overlay_appends_decision_policies(self) -> None:
        base_data = {
            **_BASE_YAML,
            "decision_policies": [
                {
                    "name": "base_policy",
                    "applies_to": "query",
                    "query_name": "find_cases",
                    "relationship_type": "cites",
                    "effect": "suppress",
                },
            ],
            "named_queries": {
                "find_cases": {
                    "mode": "traversal",
                    "entry_point": "Case",
                    "returns": "Case",
                    "traversal": [{"relationship": "cites", "direction": "outgoing"}],
                },
            },
        }
        base = CoreConfig.model_validate(base_data)

        overlay = _overlay(
            {
                "decision_policies": [
                    {
                        "name": "overlay_policy",
                        "applies_to": "query",
                        "query_name": "find_cases",
                        "relationship_type": "cites",
                        "effect": "suppress",
                        "match": {"from": {"case_id": "CASE-X"}},
                    },
                ],
            }
        )
        composed = compose_configs(base, overlay)
        names = [p.name for p in composed.decision_policies]
        assert names == ["base_policy", "overlay_policy"]

    def test_overlay_decision_policies_without_base(self) -> None:
        overlay = _overlay(
            {
                "named_queries": {
                    "find_cases": {
                        "mode": "traversal",
                        "entry_point": "Case",
                        "returns": "Case",
                        "traversal": [
                            {"relationship": "cites", "direction": "outgoing"},
                        ],
                    },
                },
                "decision_policies": [
                    {
                        "name": "overlay_only",
                        "applies_to": "query",
                        "query_name": "find_cases",
                        "relationship_type": "cites",
                        "effect": "suppress",
                    },
                ],
            }
        )
        composed = compose_configs(_base(), overlay)
        assert len(composed.decision_policies) == 1
        assert composed.decision_policies[0].name == "overlay_only"


class TestMutationGuardsComposition:
    def test_overlay_appends_mutation_guards(self) -> None:
        base_data = {
            **_BASE_YAML,
            "named_queries": {
                "find_cases": {
                    "mode": "collection",
                    "returns": "Case",
                    "result_shape": "entity",
                    "where": {"result.entity_id": {"eq": "$input.case_id"}},
                },
            },
            "mutation_guards": [
                {
                    "name": "base_case_guard",
                    "entity_type": "Case",
                    "property": "case_id",
                    "new_value": "CASE-BASE",
                    "condition": {
                        "type": "query",
                        "query_name": "find_cases",
                        "params": {"case_id": "$entity.entity_id"},
                        "min_count": 1,
                    },
                },
            ],
        }
        base = CoreConfig.model_validate(base_data)

        overlay = _overlay(
            {
                "mutation_guards": [
                    {
                        "name": "overlay_case_guard",
                        "entity_type": "Case",
                        "property": "case_id",
                        "new_value": "CASE-OVERLAY",
                        "condition": {
                            "type": "query",
                            "query_name": "find_cases",
                            "params": {"case_id": "$entity.entity_id"},
                            "min_count": 1,
                        },
                    },
                ],
            }
        )

        composed = compose_configs(base, overlay)

        names = [guard.name for guard in composed.mutation_guards]
        assert names == ["base_case_guard", "overlay_case_guard"]


class TestArtifactUriComposition:
    def test_compose_config_files_rebases_relative_artifacts(self, tmp_path: Path) -> None:
        base_path = tmp_path / "base" / "config.yaml"
        overlay_path = tmp_path / "overlay" / "config.yaml"
        base_path.parent.mkdir(parents=True)
        overlay_path.parent.mkdir(parents=True)

        (base_path.parent / "bundle").mkdir()
        (overlay_path.parent / "seed").mkdir()

        base_path.write_text(
            """\
version: "1.0"
name: base
entity_types:
  Case:
    properties:
      case_id:
        type: string
        primary_key: true
relationships: []
artifacts:
  base_bundle:
    kind: directory
    uri: ./bundle
    digest: sha256:base
"""
        )
        overlay_path.write_text(
            """\
version: "1.0"
name: overlay
extends: ../base/config.yaml
entity_types: {}
relationships: []
artifacts:
  seed_bundle:
    kind: directory
    uri: ./seed
    digest: sha256:seed
"""
        )

        composed = compose_config_files(base_path=base_path, overlay_path=overlay_path)
        assert composed.artifacts["base_bundle"].uri == str((base_path.parent / "bundle").resolve())
        assert composed.artifacts["seed_bundle"].uri == str(
            (overlay_path.parent / "seed").resolve()
        )

    def test_write_composed_config_persists_rebased_artifacts(self, tmp_path: Path) -> None:
        base_path = tmp_path / "base" / "config.yaml"
        overlay_path = tmp_path / "overlay" / "config.yaml"
        output_path = tmp_path / "out" / "config.yaml"
        base_path.parent.mkdir(parents=True)
        overlay_path.parent.mkdir(parents=True)
        (base_path.parent / "bundle").mkdir()

        base_path.write_text(
            """\
version: "1.0"
name: base
entity_types:
  Case:
    properties:
      case_id:
        type: string
        primary_key: true
relationships: []
artifacts:
  base_bundle:
    kind: directory
    uri: ./bundle
    digest: sha256:base
"""
        )
        overlay_path.write_text(
            """\
version: "1.0"
name: overlay
extends: ../base/config.yaml
entity_types: {}
relationships: []
"""
        )

        write_composed_config(
            base_path=base_path,
            overlay_path=overlay_path,
            output_path=output_path,
        )
        composed = load_config(output_path)
        assert composed.artifacts["base_bundle"].uri == str((base_path.parent / "bundle").resolve())

    def test_write_composed_config_does_not_persist_semantically_invalid_output(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "base.yaml"
        overlay_path = tmp_path / "overlay.yaml"
        output_path = tmp_path / "out" / "config.yaml"

        base_path.write_text(
            """\
version: "1.0"
name: base
entity_types:
  Case:
    properties:
      case_id:
        type: string
        primary_key: true
relationships:
  - name: cites
    from: Case
    to: Case
"""
        )
        overlay_path.write_text(
            """\
version: "1.0"
name: overlay
extends: ./base.yaml
entity_types: {}
relationships: []
named_queries:
  bad_query:
    mode: traversal
    entry_point: Case
    traversal:
      - relationship: missing_relationship
        direction: outgoing
    returns: Case
"""
        )

        with pytest.raises(ConfigError, match="Named query 'bad_query'"):
            write_composed_config(
                base_path=base_path,
                overlay_path=overlay_path,
                output_path=output_path,
            )

        assert not output_path.exists()
