"""Tests for the gates config element: strict kind-based schema, lint, composition."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_core.config.composer import compose_configs
from cruxible_core.config.loader import load_config, load_config_from_string
from cruxible_core.config.schema import GATE_KINDS, CoreConfig, schema_wire_payload
from cruxible_core.config.validator import validate_config
from cruxible_core.errors import ConfigError

BASE_CONFIG: dict[str, Any] = {
    "version": "1.0",
    "name": "gate_test",
    "entity_types": {
        "ReviewRequest": {
            "properties": {
                "review_request_id": {"type": "string", "primary_key": True},
                "status": {"type": "string"},
                "change_head": {"type": "string", "optional": True},
            }
        }
    },
}

VALID_GATE: dict[str, Any] = {
    "kind": "git-pre-push",
    "entity_type": "ReviewRequest",
    "match_property": "change_head",
    "condition": {"status": "approved"},
    "adapter": {"branch_pattern": "refs/heads/main"},
}


def _config_with_gate(gate: dict[str, Any]) -> CoreConfig:
    return CoreConfig.model_validate({**BASE_CONFIG, "gates": {"merge-review": gate}})


class TestGateSchema:
    def test_valid_gate_parses_and_lints_clean(self) -> None:
        config = _config_with_gate(VALID_GATE)
        gate = config.gates["merge-review"]
        assert gate.kind == "git-pre-push"
        assert gate.entity_type == "ReviewRequest"
        assert gate.match_property == "change_head"
        assert gate.condition == {"status": "approved"}
        assert gate.adapter is not None
        assert gate.adapter.branch_pattern == "refs/heads/main"
        assert validate_config(config) == []

    def test_unknown_gate_key_refused(self) -> None:
        gate = {**VALID_GATE, "install": "auto"}
        with pytest.raises(ValidationError, match="install"):
            _config_with_gate(gate)

    def test_missing_required_key_refused(self) -> None:
        gate = deepcopy(VALID_GATE)
        del gate["entity_type"]
        with pytest.raises(ValidationError, match="entity_type"):
            _config_with_gate(gate)

    def test_missing_kind_refused(self) -> None:
        gate = deepcopy(VALID_GATE)
        del gate["kind"]
        with pytest.raises(ValidationError, match="kind"):
            _config_with_gate(gate)

    def test_removed_v0_fields_refused(self) -> None:
        # The pre-kind field spellings must not silently parse.
        for legacy_key, value in (
            ("sha_property", "change_head"),
            ("predicate", {"status": "approved"}),
            ("applies_to", "refs/heads/main"),
        ):
            gate = {**VALID_GATE, legacy_key: value}
            with pytest.raises(ValidationError, match=legacy_key):
                _config_with_gate(gate)

    def test_empty_condition_refused(self) -> None:
        gate = {**VALID_GATE, "condition": {}}
        with pytest.raises(ValidationError, match="at least one property"):
            _config_with_gate(gate)

    def test_condition_constraining_match_property_refused(self) -> None:
        gate = {**VALID_GATE, "condition": {"status": "approved", "change_head": "abc"}}
        with pytest.raises(ValidationError, match="match_property"):
            _config_with_gate(gate)

    def test_condition_reserved_query_key_refused(self) -> None:
        # 'query' is reserved so a future named-query condition variant is a
        # non-breaking addition; using it as a property predicate must refuse.
        gate = {**VALID_GATE, "condition": {"query": "approved-reviews"}}
        with pytest.raises(ValidationError, match="reserved"):
            _config_with_gate(gate)

    def test_git_pre_push_without_adapter_config_refused(self) -> None:
        gate = deepcopy(VALID_GATE)
        del gate["adapter"]
        with pytest.raises(ValidationError, match="adapter"):
            _config_with_gate(gate)

    def test_blank_branch_pattern_refused(self) -> None:
        gate = {**VALID_GATE, "adapter": {"branch_pattern": "  "}}
        with pytest.raises(ValidationError, match="branch_pattern"):
            _config_with_gate(gate)

    def test_unknown_adapter_key_refused(self) -> None:
        gate = {
            **VALID_GATE,
            "adapter": {"branch_pattern": "refs/heads/main", "remote": "origin"},
        }
        with pytest.raises(ValidationError, match="remote"):
            _config_with_gate(gate)


class TestGateLint:
    def test_unknown_kind_refused(self) -> None:
        # Schema stays permissive on kind (a string) so newer configs parse;
        # lint is the enforcement point for the source-adapter enum.
        config = _config_with_gate({**VALID_GATE, "kind": "ci-status"})
        with pytest.raises(ConfigError, match="unknown kind 'ci-status'"):
            validate_config(config)

    def test_kind_enum_is_v1(self) -> None:
        assert GATE_KINDS == {"git-pre-push"}

    def test_undeclared_entity_type_refused(self) -> None:
        config = _config_with_gate({**VALID_GATE, "entity_type": "PullRequest"})
        with pytest.raises(ConfigError, match="entity type 'PullRequest'"):
            validate_config(config)

    def test_missing_match_property_refused(self) -> None:
        config = _config_with_gate({**VALID_GATE, "match_property": "merge_head"})
        with pytest.raises(ConfigError, match="match_property 'merge_head'"):
            validate_config(config)

    def test_missing_condition_property_refused(self) -> None:
        config = _config_with_gate({**VALID_GATE, "condition": {"state": "approved"}})
        with pytest.raises(ConfigError, match="condition property 'state'"):
            validate_config(config)


COMPACT_GATES_YAML = """\
version: "1.0"
name: compact_gates
metadata: {}
entity_types:
  ReviewRequest:
    id: review_request_id
    properties:
      status: string
      change_head: string?
gates:
  merge-review:
    kind: git-pre-push
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: approved}
    adapter: {branch_pattern: refs/heads/main}
"""


class TestGateLoadSurfaces:
    def test_compact_config_passes_gates_through(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(COMPACT_GATES_YAML)
        config = load_config(config_path)
        assert config.gates["merge-review"].match_property == "change_head"
        assert config.gates["merge-review"].kind == "git-pre-push"

    def test_schema_wire_payload_carries_gates(self) -> None:
        payload = schema_wire_payload(_config_with_gate(VALID_GATE))
        assert payload["gates"]["merge-review"] == {
            "kind": "git-pre-push",
            "entity_type": "ReviewRequest",
            "match_property": "change_head",
            "condition": {"status": "approved"},
            "adapter": {"branch_pattern": "refs/heads/main"},
            "description": None,
        }


OVERLAY_GATE_YAML = """\
version: "1.0"
name: gate_overlay
extends: base.yaml
gates:
  release-review:
    kind: git-pre-push
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: approved}
    adapter: {branch_pattern: refs/heads/release-*}
"""

OVERLAY_REDEFINE_YAML = """\
version: "1.0"
name: gate_overlay
extends: base.yaml
gates:
  merge-review:
    kind: git-pre-push
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: requested}
    adapter: {branch_pattern: refs/heads/main}
"""


class TestGateComposition:
    def test_overlay_adds_gate(self) -> None:
        base = _config_with_gate(VALID_GATE)
        overlay = load_config_from_string(OVERLAY_GATE_YAML)
        composed = compose_configs(base, overlay)
        assert set(composed.gates) == {"merge-review", "release-review"}

    def test_overlay_cannot_redefine_upstream_gate(self) -> None:
        base = _config_with_gate(VALID_GATE)
        overlay = load_config_from_string(OVERLAY_REDEFINE_YAML)
        with pytest.raises(ConfigError, match="merge-review"):
            compose_configs(base, overlay)
