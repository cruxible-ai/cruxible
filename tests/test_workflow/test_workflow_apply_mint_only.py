"""Regression tests: workflow apply_entities / apply_all enforce mint_only policy.

A ``mint_only`` entity type may be written ONLY by the ``token_mint`` source --
every other source, INCLUDING the governed ``workflow_apply`` verb, is refused at
the ``graph/operations.py`` chokepoint. Before the fix the canonical apply path
(``apply_entity_set``) persisted entities by calling ``graph.add_entity`` /
``graph.update_entity_properties`` DIRECTLY, so it never reached that chokepoint.
The only mint_only defense on the workflow path was the config-time
``make_entities`` validator -- which does not see a ``provider`` step that emits an
``EntitySet`` consumed by ``apply_entities``. On a default instance (no
``mutation_guards``) such a workflow wrote a mint_only entity with no refusal.

These tests build a config whose provider emits an ``EntitySet`` (no
``make_entities`` step, so the config loads) and run it through the real executor.
The mint_only case must be refused with ``DirectWriteRefusedError``; the direct
and proposal_only cases must still apply (workflow_apply is a governed source).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from tests.support.workflow_helpers import write_lock_for_instance

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DirectWriteRefusedError
from cruxible_core.workflow.executor import execute_workflow

_CONFIG_YAML = dedent(
    """
    version: "1.0"
    name: mint_only_workflow_apply

    entity_types:
      AuthToken:
        write_policy: mint_only
        properties:
          id:
            type: string
            primary_key: true
          label:
            type: string
      Widget:
        properties:
          id:
            type: string
            primary_key: true
          label:
            type: string
      Proposal:
        write_policy: proposal_only
        properties:
          id:
            type: string
            primary_key: true
          label:
            type: string

    relationships: []

    contracts:
      EmitInput:
        fields:
          entity_type:
            type: string
          entity_id:
            type: string
          label:
            type: string
            optional: true
      EntitySetOut:
        fields: {}
        allow_extra: true

    providers:
      emit_entities:
        kind: function
        contract_in: EmitInput
        contract_out: EntitySetOut
        ref: tests.support.workflow_test_providers.emit_entity_set
        version: "1.0.0"
        deterministic: true
        runtime: python

    workflows:
      emit_and_apply:
        type: canonical
        contract_in: EmitInput
        steps:
          - id: emitted
            provider: emit_entities
            input:
              entity_type: $input.entity_type
              entity_id: $input.entity_id
              label: $input.label
            as: emitted
          - id: apply_emitted
            apply_entities:
              entities_from: emitted
            as: apply_emitted
        returns: apply_emitted
      emit_and_apply_all:
        type: canonical
        contract_in: EmitInput
        steps:
          - id: emitted
            provider: emit_entities
            input:
              entity_type: $input.entity_type
              entity_id: $input.entity_id
              label: $input.label
            as: emitted
          - id: apply_emitted
            apply_all:
              entities_from:
                - emitted
            as: apply_emitted
        returns: apply_emitted
    """
)


def _instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    write_lock_for_instance(instance)
    return instance


def _run(
    instance: CruxibleInstance,
    workflow: str,
    entity_type: str,
    entity_id: str,
    *,
    mode: str,
):
    return execute_workflow(
        instance,
        instance.load_config(),
        workflow,
        {"entity_type": entity_type, "entity_id": entity_id, "label": "v1"},
        mode=mode,
    )


class TestWorkflowApplyMintOnly:
    """apply_entities routes the live write through the mint_only chokepoint."""

    def test_apply_entities_refuses_mint_only_in_preview(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        with pytest.raises(DirectWriteRefusedError) as exc:
            _run(instance, "emit_and_apply", "AuthToken", "tok-1", mode="preview")
        assert exc.value.type_name == "AuthToken"
        assert exc.value.source == "workflow_apply"
        assert instance.load_graph().get_entity("AuthToken", "tok-1") is None

    def test_apply_entities_refuses_mint_only_in_apply(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        with pytest.raises(DirectWriteRefusedError):
            _run(instance, "emit_and_apply", "AuthToken", "tok-2", mode="apply")
        # Refusal happens before commit, so live state must be untouched.
        assert instance.load_graph().get_entity("AuthToken", "tok-2") is None

    def test_apply_all_refuses_mint_only(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        with pytest.raises(DirectWriteRefusedError):
            _run(instance, "emit_and_apply_all", "AuthToken", "tok-3", mode="apply")
        assert instance.load_graph().get_entity("AuthToken", "tok-3") is None


class TestWorkflowApplyGovernedTypesStillApply:
    """Positive controls: workflow_apply is governed, so non-mint_only types pass."""

    def test_apply_entities_applies_direct_type(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        result = _run(instance, "emit_and_apply", "Widget", "widget-1", mode="apply")
        assert result.mode == "apply"
        applied = instance.load_graph().get_entity("Widget", "widget-1")
        assert applied is not None
        assert applied.properties["label"] == "v1"

    def test_apply_entities_applies_proposal_only_type(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        result = _run(instance, "emit_and_apply", "Proposal", "prop-1", mode="apply")
        assert result.mode == "apply"
        applied = instance.load_graph().get_entity("Proposal", "prop-1")
        assert applied is not None
        assert applied.properties["label"] == "v1"

    def test_apply_all_applies_direct_type(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        result = _run(instance, "emit_and_apply_all", "Widget", "widget-2", mode="apply")
        assert result.mode == "apply"
        assert instance.load_graph().get_entity("Widget", "widget-2") is not None
