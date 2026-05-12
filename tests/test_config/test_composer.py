"""Tests for config composition (base + overlay merge)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.config.composer import (
    ResolvedConfigLayer,
    compose_config_files,
    compose_config_sequence,
    compose_configs,
    compose_runtime_configs,
    write_composed_config,
)
from cruxible_core.config.loader import load_config, load_config_from_string
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError

_BASE_YAML = {
    "version": "1.0",
    "name": "base",
    "kind": "world_model",
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
kind: world_model
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
    sha256: sha256:bundle
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
    entry_point: Vendor
    traversal: []
    returns: "list[Vendor]"
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

    def test_compose_validates_semantic_output_by_default(self) -> None:
        base = _base()
        overlay = _overlay(
            {
                "named_queries": {
                    "bad_query": {
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

    def test_compose_validate_false_allows_raw_merge_inspection(self) -> None:
        base = _base()
        overlay = _overlay(
            {
                "named_queries": {
                    "bad_query": {
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
kind: world_model
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
    sha256: sha256:base
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
    sha256: sha256:seed
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
kind: world_model
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
    sha256: sha256:base
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
kind: world_model
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
