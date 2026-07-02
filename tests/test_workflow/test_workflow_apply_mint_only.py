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
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.temporal import utc_now
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
          first_only:
            type: string
            optional: true
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
          properties:
            type: json
            optional: true
          entities:
            type: json
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
              properties: $input.properties
              entities: $input.entities
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
              properties: $input.properties
              entities: $input.entities
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
    label: str = "v1",
    properties: dict[str, object] | None = None,
    entities: list[dict[str, object]] | None = None,
    actor_context: GovernedActorContext | None = None,
):
    return execute_workflow(
        instance,
        instance.load_config(),
        workflow,
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "label": label,
            "properties": properties or {},
            "entities": entities or [],
        },
        mode=mode,
        actor_context=actor_context,
    )


def _actor_context(actor_id: str) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org_test",
        operation_id=f"op_{actor_id}",
        timestamp=utc_now(),
    )


def _entity_write_flags(result, entity_type: str, entity_id: str) -> list[bool]:
    return [
        bool(node.detail["is_update"])
        for node in result.receipt.nodes
        if node.node_type == "entity_write"
        and node.entity_type == entity_type
        and node.entity_id == entity_id
    ]


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

    def test_apply_all_refuses_mint_only_in_preview(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        with pytest.raises(DirectWriteRefusedError):
            _run(instance, "emit_and_apply_all", "AuthToken", "tok-3", mode="preview")
        assert instance.load_graph().get_entity("AuthToken", "tok-3") is None

    def test_apply_all_refuses_mint_only(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        with pytest.raises(DirectWriteRefusedError):
            _run(instance, "emit_and_apply_all", "AuthToken", "tok-4", mode="apply")
        assert instance.load_graph().get_entity("AuthToken", "tok-4") is None


class TestWorkflowApplyGovernedTypesStillApply:
    """Positive controls: workflow_apply is governed, so non-mint_only types pass."""

    def test_apply_entities_applies_direct_type(self, tmp_path: Path) -> None:
        instance = _instance(tmp_path)
        result = _run(instance, "emit_and_apply", "Widget", "widget-1", mode="apply")
        assert result.mode == "apply"
        applied = instance.load_graph().get_entity("Widget", "widget-1")
        assert applied is not None
        assert applied.properties["label"] == "v1"

    def test_apply_entities_duplicate_id_uses_live_update_state(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _instance(tmp_path)
        result = _run(
            instance,
            "emit_and_apply",
            "Widget",
            "widget-dupe",
            mode="apply",
            entities=[
                {
                    "entity_type": "Widget",
                    "entity_id": "widget-dupe",
                    "properties": {
                        "id": "widget-dupe",
                        "label": "first",
                        "first_only": "survives",
                    },
                },
                {
                    "entity_type": "Widget",
                    "entity_id": "widget-dupe",
                    "properties": {"id": "widget-dupe", "label": "second"},
                },
            ],
        )

        preview = result.apply_previews["apply_emitted"]
        assert preview["create_count"] == 1
        assert preview["update_count"] == 1
        assert preview["noop_count"] == 0
        assert result.output == preview
        applied = instance.load_graph().get_entity("Widget", "widget-dupe")
        assert applied is not None
        assert applied.properties["label"] == "second"
        assert applied.properties["first_only"] == "survives"
        assert _entity_write_flags(result, "Widget", "widget-dupe") == [False, True]

    def test_apply_entities_second_run_updates_existing_entity(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _instance(tmp_path)
        first = _run(
            instance,
            "emit_and_apply",
            "Widget",
            "widget-existing",
            mode="apply",
            properties={
                "id": "widget-existing",
                "label": "v1",
                "first_only": "kept",
            },
            actor_context=_actor_context("usr_first"),
        )
        assert first.output["create_count"] == 1
        assert _entity_write_flags(first, "Widget", "widget-existing") == [False]

        second = _run(
            instance,
            "emit_and_apply",
            "Widget",
            "widget-existing",
            mode="apply",
            properties={"id": "widget-existing", "label": "v2"},
            actor_context=_actor_context("usr_second"),
        )

        assert second.output["create_count"] == 0
        assert second.output["update_count"] == 1
        assert _entity_write_flags(second, "Widget", "widget-existing") == [True]
        applied = instance.load_graph().get_entity("Widget", "widget-existing")
        assert applied is not None
        assert applied.properties["label"] == "v2"
        assert applied.properties["first_only"] == "kept"
        metadata = applied.metadata.to_metadata_dict()
        assert metadata["actor_context"]["actor_id"] == "usr_second"
        assert metadata["actor_context"]["operation_id"] == "op_usr_second"

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
