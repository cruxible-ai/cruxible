"""Tests for the gates config element: strict schema, lint, and composition."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_core.config.composer import compose_configs
from cruxible_core.config.loader import load_config, load_config_from_string
from cruxible_core.config.schema import CoreConfig, schema_wire_payload
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
    "entity_type": "ReviewRequest",
    "sha_property": "change_head",
    "predicate": {"status": "approved"},
    "applies_to": "refs/heads/main",
}


def _config_with_gate(gate: dict[str, Any]) -> CoreConfig:
    return CoreConfig.model_validate({**BASE_CONFIG, "gates": {"merge-review": gate}})


class TestGateSchema:
    def test_valid_gate_parses_and_lints_clean(self) -> None:
        config = _config_with_gate(VALID_GATE)
        gate = config.gates["merge-review"]
        assert gate.entity_type == "ReviewRequest"
        assert gate.sha_property == "change_head"
        assert gate.predicate == {"status": "approved"}
        assert gate.applies_to == "refs/heads/main"
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

    def test_empty_predicate_refused(self) -> None:
        gate = {**VALID_GATE, "predicate": {}}
        with pytest.raises(ValidationError, match="at least one property"):
            _config_with_gate(gate)

    def test_predicate_constraining_sha_property_refused(self) -> None:
        gate = {**VALID_GATE, "predicate": {"status": "approved", "change_head": "abc"}}
        with pytest.raises(ValidationError, match="sha_property"):
            _config_with_gate(gate)

    def test_blank_applies_to_refused(self) -> None:
        gate = {**VALID_GATE, "applies_to": "  "}
        with pytest.raises(ValidationError, match="applies_to"):
            _config_with_gate(gate)


class TestGateLint:
    def test_undeclared_entity_type_refused(self) -> None:
        config = _config_with_gate({**VALID_GATE, "entity_type": "PullRequest"})
        with pytest.raises(ConfigError, match="entity type 'PullRequest'"):
            validate_config(config)

    def test_missing_sha_property_refused(self) -> None:
        config = _config_with_gate({**VALID_GATE, "sha_property": "merge_head"})
        with pytest.raises(ConfigError, match="sha_property 'merge_head'"):
            validate_config(config)

    def test_missing_predicate_property_refused(self) -> None:
        config = _config_with_gate({**VALID_GATE, "predicate": {"state": "approved"}})
        with pytest.raises(ConfigError, match="predicate property 'state'"):
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
    entity_type: ReviewRequest
    sha_property: change_head
    predicate: {status: approved}
    applies_to: refs/heads/main
"""


class TestGateLoadSurfaces:
    def test_compact_config_passes_gates_through(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(COMPACT_GATES_YAML)
        config = load_config(config_path)
        assert config.gates["merge-review"].sha_property == "change_head"

    def test_schema_wire_payload_carries_gates(self) -> None:
        payload = schema_wire_payload(_config_with_gate(VALID_GATE))
        assert payload["gates"]["merge-review"] == {
            "entity_type": "ReviewRequest",
            "sha_property": "change_head",
            "predicate": {"status": "approved"},
            "applies_to": "refs/heads/main",
            "description": None,
        }


OVERLAY_GATE_YAML = """\
version: "1.0"
name: gate_overlay
extends: base.yaml
gates:
  release-review:
    entity_type: ReviewRequest
    sha_property: change_head
    predicate: {status: approved}
    applies_to: refs/heads/release-*
"""

OVERLAY_REDEFINE_YAML = """\
version: "1.0"
name: gate_overlay
extends: base.yaml
gates:
  merge-review:
    entity_type: ReviewRequest
    sha_property: change_head
    predicate: {status: requested}
    applies_to: refs/heads/main
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
