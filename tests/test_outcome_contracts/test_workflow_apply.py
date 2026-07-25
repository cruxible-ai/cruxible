"""The outcome guard holds on the canonical workflow apply path too.

``requires_resolution_contract`` is the first store-backed entity-guard
condition, so the canonical apply path had to grow a store handle it never
needed before. A guard that only holds on direct writes is not a guard: a
canonical workflow would be the bypass.
"""
# mypy: disable-error-code=no-untyped-def

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import QueryExecutionError
from cruxible_core.service import (
    service_list_resolution_contracts,
    service_open_resolution_contract,
)
from cruxible_core.workflow.executor import execute_workflow
from tests.support.workflow_helpers import write_lock_for_instance
from tests.test_outcome_contracts.conftest import (
    CHECK_AT,
    EXPIRES_AT,
    GUARDED_CONFIG,
    actor,
    add_decision,
    query_measurement,
)

_ACCEPT_DECISION_WORKFLOW = dedent(
    """
    contracts:
      AcceptDecisionInput:
        fields:
          decision_id:
            type: string
          status:
            type: string

    workflows:
      accept_decision:
        type: canonical
        contract_in: AcceptDecisionInput
        steps:
          - id: decisions
            make_entities:
              entity_type: Decision
              items:
                - decision_id: $input.decision_id
                  status: $input.status
              entity_id: $item.decision_id
              properties:
                status: $item.status
            as: decisions
          - id: apply_decisions
            apply_entities:
              entities_from: decisions
            as: apply_decisions
        returns: apply_decisions
    """
)


@pytest.fixture
def workflow_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(GUARDED_CONFIG + _ACCEPT_DECISION_WORKFLOW)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    write_lock_for_instance(instance)
    add_decision(instance)
    return instance


def _open(instance: CruxibleInstance) -> str:
    return service_open_resolution_contract(
        instance,
        entity_type="Decision",
        entity_id="dd-1",
        description="Service stays healthy after the change",
        check_at=CHECK_AT,
        expires_at=EXPIRES_AT,
        measurement=query_measurement(),
        actor_context=actor("proposer"),
    ).contract.contract_id


def _status(instance: CruxibleInstance) -> str:
    entity = instance.load_graph().get_entity("Decision", "dd-1")
    assert entity is not None
    return str(entity.properties["status"])


def test_canonical_apply_refuses_without_a_contract(workflow_instance) -> None:
    with pytest.raises(QueryExecutionError, match="decision_acceptance_needs_contract"):
        execute_workflow(
            workflow_instance,
            workflow_instance.load_config(),
            "accept_decision",
            {"decision_id": "dd-1", "status": "accepted"},
            mode="apply",
        )
    assert _status(workflow_instance) == "proposed"


def test_canonical_apply_passes_and_activates_with_a_contract(workflow_instance) -> None:
    contract_id = _open(workflow_instance)
    result = execute_workflow(
        workflow_instance,
        workflow_instance.load_config(),
        "accept_decision",
        {"decision_id": "dd-1", "status": "accepted"},
        mode="apply",
    )
    assert result.mode == "apply"
    assert _status(workflow_instance) == "accepted"

    item = service_list_resolution_contracts(workflow_instance).items[0]
    assert item.contract.contract_id == contract_id
    assert item.activation is not None
    assert item.activation.acceptance_receipt_id is not None


def test_canonical_preview_consumes_nothing(workflow_instance) -> None:
    _open(workflow_instance)
    result = execute_workflow(
        workflow_instance,
        workflow_instance.load_config(),
        "accept_decision",
        {"decision_id": "dd-1", "status": "accepted"},
        mode="preview",
    )
    assert result.mode == "preview"
    assert _status(workflow_instance) == "proposed"
    assert service_list_resolution_contracts(workflow_instance).items[0].activation is None
