"""The supply-chain and case-law kits adopt the 0.3 decision/outcome loop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.config.composer import compose_config_files
from cruxible_core.config.loader import save_config
from cruxible_core.errors import DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.service import (
    service_add_entities,
    service_add_relationships,
    service_list_resolution_contracts,
    service_open_resolution_contract,
    service_query_surface,
)
from cruxible_core.temporal import utc_now

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AdoptionScenario:
    kit_id: str
    decision_type: str
    decision_id_key: str
    decision_id: str
    decision_properties: dict[str, Any]
    subject_type: str
    subject_id: str
    subject_properties: dict[str, Any]
    relationship_type: str
    open_query: str
    measurement_query: str
    measurement_params: dict[str, Any]


SCENARIOS = (
    AdoptionScenario(
        kit_id="supply-chain-blast-radius",
        decision_type="IncidentResponseDecision",
        decision_id_key="response_decision_id",
        decision_id="IRD-03",
        decision_properties={
            "title": "Hold shipments exposed by the rail disruption",
            "status": "proposed",
            "outcome_tracking": "required",
            "action": "hold_shipments",
            "rationale": "The accepted blast radius reaches in-flight shipments.",
        },
        subject_type="Incident",
        subject_id="INC-03",
        subject_properties={
            "title": "Rail disruption",
            "severity": "high",
            "scope_type": "geography",
            "scope_id": "TW",
            "status": "open",
            "reported_at": "2026-07-31",
            "closed_at": None,
            "summary": "Rail service is interrupted.",
        },
        relationship_type="incident_response_decision_for_incident",
        open_query="open_incident_response_decisions",
        measurement_query="incident_exposed_shipments",
        measurement_params={"incident_id": "INC-03"},
    ),
    AdoptionScenario(
        kit_id="case-law-monitoring",
        decision_type="MatterActionDecision",
        decision_id_key="action_decision_id",
        decision_id="MAD-03",
        decision_properties={
            "title": "Update the active brief after new adverse authority",
            "status": "proposed",
            "outcome_tracking": "required",
            "action": "update_brief",
            "rationale": "The new opinion limits authority cited in the current draft.",
        },
        subject_type="Matter",
        subject_id="MAT-03",
        subject_properties={
            "name": "Example matter",
            "matter_type": "litigation",
            "status": "active",
            "jurisdiction": "federal",
        },
        relationship_type="matter_action_decision_for_matter",
        open_query="open_matter_action_decisions",
        measurement_query="work_items_for_matter",
        measurement_params={"matter_id": "MAT-03"},
    ),
)


def _actor(actor_id: str) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org-domain-kit-test",
        operation_id=f"op-{actor_id}",
        timestamp=utc_now(),
    )


def _instance(tmp_path: Path, scenario: AdoptionScenario) -> CruxibleInstance:
    config = compose_config_files(
        base_path=REPO_ROOT / "kits" / "agent-operation" / "config.yaml",
        overlay_path=REPO_ROOT / "kits" / scenario.kit_id / "config.yaml",
    )
    config_path = tmp_path / "config.yaml"
    save_config(config, config_path)
    return CruxibleInstance.init(tmp_path / "instance", str(config_path))


def _decision(scenario: AdoptionScenario, *, status: str, outcome_tracking: str) -> EntityInstance:
    properties = {
        scenario.decision_id_key: scenario.decision_id,
        **scenario.decision_properties,
        "status": status,
        "outcome_tracking": outcome_tracking,
    }
    return EntityInstance(
        entity_type=scenario.decision_type,
        entity_id=scenario.decision_id,
        properties=properties,
    )


def _seed_subject_and_decision(
    instance: CruxibleInstance,
    scenario: AdoptionScenario,
) -> None:
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type=scenario.subject_type,
                entity_id=scenario.subject_id,
                properties={
                    f"{scenario.subject_type.lower()}_id": scenario.subject_id,
                    **scenario.subject_properties,
                },
            ),
            _decision(scenario, status="proposed", outcome_tracking="required"),
        ],
        actor_context=_actor("proposer"),
    )
    service_add_relationships(
        instance,
        [
            RelationshipInstance(
                from_type=scenario.decision_type,
                from_id=scenario.decision_id,
                relationship_type=scenario.relationship_type,
                to_type=scenario.subject_type,
                to_id=scenario.subject_id,
            )
        ],
        source="test",
        source_ref="domain-kit-03-adoption",
        actor_context=_actor("proposer"),
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.kit_id)
def test_domain_kit_forces_outcome_contract_before_acceptance(
    tmp_path: Path,
    scenario: AdoptionScenario,
) -> None:
    instance = _instance(tmp_path, scenario)
    _seed_subject_and_decision(instance, scenario)

    queued = service_query_surface(instance, scenario.open_query, {})
    assert {item.entity_id for item in queued.items} == {scenario.decision_id}

    with pytest.raises(DataValidationError, match="resolution contract"):
        service_add_entities(
            instance,
            [_decision(scenario, status="accepted", outcome_tracking="required")],
            actor_context=_actor("reviewer"),
        )

    now = utc_now()
    contract = service_open_resolution_contract(
        instance,
        entity_type=scenario.decision_type,
        entity_id=scenario.decision_id,
        description="The response leaves no unresolved operational work.",
        check_at=now + timedelta(days=7),
        expires_at=now + timedelta(days=30),
        measurement={
            "kind": "query",
            "query_name": scenario.measurement_query,
            "params": scenario.measurement_params,
            "expect": {"max_count": 0},
        },
        actor_context=_actor("proposer"),
    ).contract

    service_add_entities(
        instance,
        [_decision(scenario, status="accepted", outcome_tracking="required")],
        actor_context=_actor("reviewer"),
    )

    stored = instance.load_graph().get_entity(scenario.decision_type, scenario.decision_id)
    assert stored is not None
    assert stored.properties["status"] == "accepted"

    contracts = service_list_resolution_contracts(instance).items
    activated = [item for item in contracts if item.contract.contract_id == contract.contract_id]
    assert len(activated) == 1
    assert activated[0].activation is not None

    queued = service_query_surface(instance, scenario.open_query, {})
    assert queued.items == []


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.kit_id)
def test_domain_kit_cannot_disable_outcome_tracking_during_acceptance(
    tmp_path: Path,
    scenario: AdoptionScenario,
) -> None:
    instance = _instance(tmp_path, scenario)
    _seed_subject_and_decision(instance, scenario)

    with pytest.raises(DataValidationError, match="fixed when proposed"):
        service_add_entities(
            instance,
            [_decision(scenario, status="accepted", outcome_tracking="not_applicable")],
            actor_context=_actor("reviewer"),
        )

    stored = instance.load_graph().get_entity(scenario.decision_type, scenario.decision_id)
    assert stored is not None
    assert stored.properties["status"] == "proposed"
    assert stored.properties["outcome_tracking"] == "required"
