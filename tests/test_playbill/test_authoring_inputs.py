"""PC-G3b decision-only input lowering and ClaimType lint laws."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.authoring.inputs import (
    AuthoringInputError,
    CarriedContractInput,
    ClaimInput,
    LiteralObjectInput,
    ProcedureInput,
    SelfSourceInput,
    WorkingSelectionInput,
)
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicyV1,
    ClaimAttestationConsequenceRuleV1,
    ClaimEvidenceFreshnessV1,
    ClaimFreshnessDurationV1,
)
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.claim_retirement import ClaimRetireResultV1, service_retire_claim
from cruxible_core.playbill.claim_type_inputs import (
    ClaimTypeInputV1,
    claim_type_input_example,
    lint_claim_type_input,
    lower_claim_type_input,
)
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
)
from tests.test_playbill.test_authoring_procedures import AUTHORITY, _slot_definition
from tests.test_playbill.test_claim_retirement import _activate, _request
from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world


def _coordinator(instance) -> AuthoringIntentCoordinator:  # type: ignore[no-untyped-def]
    return AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: "9" * 32,
        ),
        claim_id_factory=lambda: "CLM-" + "8" * 32,
    )


def _claim_input(*, working: bool = False) -> ClaimInput:
    return ClaimInput(
        kind="claim",
        subject="project.work_item/wi-42",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="ready"),
        role="observation",
        rationale="The accepted work source reports ready.",
        source=(
            WorkingSelectionInput(kind="working_selection", source_id="repo.work-items")
            if working
            else SelfSourceInput(kind="self_source", body="status: ready\n")
        ),
        citation_role="evidence" if working else None,
    )


def test_input_create_binds_friendly_subject_to_the_stored_intent_base(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    view = coordinator.create_input(
        actor=actor,
        input=_claim_input(),
        canonical_timestamp=TIMESTAMP,
    )

    assert view.intent.base_coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert view.intent.payload.statement.subject.artifact_path == (
        "subjects/project.work_item/wi-42.yaml"
    )
    assert view.intent.payload.tag == "playbill-claim-authoring-payload-v1"
    assert "digest" not in _claim_input().model_dump_json()


def test_input_compile_typed_refuses_a_terminal_v3_claim_with_v2_backing(
    tmp_path: Path,
) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    retired = service_retire_claim(
        instance,
        claim_id=claim_id,
        request=_request(instance, mode="submit"),
        actor=actor,
    )
    assert isinstance(retired, ClaimRetireResultV1)
    _activate(instance, owner, retired)

    result = _coordinator(instance).compile_input(
        actor=actor,
        input=_claim_input().model_copy(update={"claim_id": claim_id}),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "refused"
    assert [item.code for item in result.frontier.diagnostics] == [
        "playbill.authoring.claim_terminal"
    ]


def test_create_and_compile_refuse_working_selection_with_bind_repair(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)

    with pytest.raises(AuthoringInputError) as raised:
        coordinator.create_input(
            actor=AuthenticatedActor(actor_id="owner"),
            input=_claim_input(working=True),
            canonical_timestamp=TIMESTAMP,
        )

    assert raised.value.field_path == "input.source"
    assert "authoring bind" in raised.value.repair


def test_tagless_procedure_input_injects_slot_tags_and_compiles(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinator = _coordinator(instance)
    definition = _slot_definition().model_dump(mode="json", by_alias=True)

    def remove_tags(value: object) -> object:
        if isinstance(value, dict):
            return {key: remove_tags(item) for key, item in value.items() if key != "tag"}
        if isinstance(value, list):
            return [remove_tags(item) for item in value]
        return value

    tagless = remove_tags(definition)
    assert isinstance(tagless, dict)
    for key in ("contract_in", "contract_out"):
        tagless[key] = {
            "kind": "slot",
            "slot_name": definition[key]["slot_name"],  # type: ignore[index]
        }
    result = coordinator.compile_input(
        actor=AuthenticatedActor(actor_id="owner"),
        input=ProcedureInput(
            kind="procedure",
            definition=tagless,
            authority=AUTHORITY,
            activation_policy="drain",
        ),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "passed", result.frontier.model_dump_json(indent=2)


def test_carried_contract_input_computes_exact_pins_and_procedure_v2(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinator = _coordinator(instance)
    definition = _slot_definition().model_dump(mode="json", by_alias=True)

    def remove_tags(value: object) -> object:
        if isinstance(value, dict):
            return {key: remove_tags(item) for key, item in value.items() if key != "tag"}
        if isinstance(value, list):
            return [remove_tags(item) for item in value]
        return value

    tagless = remove_tags(definition)
    assert isinstance(tagless, dict)
    tagless["contract_in"] = {
        "kind": "carried_contract",
        "name": "empty-input",
        "role": "contract-in",
    }
    tagless["contract_out"] = {
        "kind": "carried_contract",
        "name": "query-result",
        "role": "contract-out",
    }
    nodes = tagless["nodes"]
    assert isinstance(nodes, list)
    project = nodes[1]
    assert isinstance(project, dict)
    project["contract_out"] = {
        "kind": "carried_contract",
        "name": "query-result",
        "role": "contract-out",
    }
    result = coordinator.compile_input(
        actor=AuthenticatedActor(actor_id="owner"),
        input=ProcedureInput(
            kind="procedure",
            definition=tagless,
            authority=AUTHORITY,
            activation_policy="drain",
            contracts=(
                CarriedContractInput(name="empty-input", fields={}),
                CarriedContractInput(
                    name="query-result",
                    fields={"rows": PropertySchema(type="json")},
                ),
            ),
        ),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "passed", result.frontier.model_dump_json(indent=2)
    payload = (
        coordinator.list_pending(actor=AuthenticatedActor(actor_id="owner")).intents[0].payload
    )
    assert payload.tag == "playbill-procedure-authoring-payload-v2"
    encoded = payload.model_dump_json(by_alias=True)
    assert "playbill-procedure-owned-contract-v1" in encoded
    assert "carried_contract" in encoded
    assert "artifact_digest" not in encoded


def test_claim_type_example_is_tagless_and_source_intent_is_lint_only(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    example = claim_type_input_example()

    lint = lint_claim_type_input(
        instance,
        example,
        coordinate=instance.accepted_coordinate(),
    )

    encoded = example.model_dump_json()
    assert '"tag"' not in encoded
    assert "identity" not in json.loads(encoded)
    assert "evidence_freshness" not in json.loads(encoded)
    assert lint.warnings[0].code == (
        "playbill.claim_type.evidence_policy_admits_no_accepted_contract"
    )

    source_intent = ClaimTypeInputV1.model_validate(
        {
            **example.model_dump(mode="json"),
            "anticipated_source_ids": ["repo.work-items"],
        }
    )
    lowered = lower_claim_type_input(
        source_intent,
        tree=instance.tree_at(instance.accepted_coordinate().git_oid),
    )
    assert "anticipated_source_ids" not in lowered.model_dump(mode="json")

    with pytest.raises(ValidationError, match="byte-sorted and unique"):
        ClaimTypeInputV1.model_validate(
            {
                **example.model_dump(mode="json"),
                "anticipated_source_ids": ["repo.z", "repo.a"],
            }
        )


def test_claim_type_input_lowers_freshness_into_the_existing_v3_artifact() -> None:
    original = claim_type_input_example()
    freshness = ClaimEvidenceFreshnessV1(
        stale_after=ClaimFreshnessDurationV1(microseconds=2_592_000_000_000)
    )
    fresh = ClaimTypeInputV1.model_validate(
        {
            **original.model_dump(mode="json"),
            "evidence_freshness": freshness.model_dump(mode="json"),
        }
    )

    legacy = lower_claim_type_input(original, tree={})
    governed = lower_claim_type_input(fresh, tree={})

    assert legacy.artifact_format == "playbill-claim-type-v1"
    assert "evidence_freshness" not in original.model_dump(mode="json")
    assert governed.artifact_format == "playbill-claim-type-v3"
    assert governed.evidence_freshness == freshness


def test_claim_type_input_lowers_attestation_consequences_into_v4() -> None:
    original = claim_type_input_example()
    policy = ClaimAttestationConsequencePolicyV1(
        rules=(
            ClaimAttestationConsequenceRuleV1(
                rule_id="two-independent-unsure",
                stance="unsure",
                minimum_independent_control_components=2,
            ),
        )
    )
    governed = lower_claim_type_input(
        ClaimTypeInputV1.model_validate(
            {
                **original.model_dump(mode="json"),
                "attestation_consequence_policy": policy.model_dump(mode="json"),
            }
        ),
        tree={},
    )

    assert governed.artifact_format == "playbill-claim-type-v4"
    assert governed.evidence_freshness is None
    assert governed.attestation_consequence_policy == policy


def test_empty_claim_type_policy_warns_when_an_accepted_capture_contract_exists(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)

    lint = lint_claim_type_input(
        instance,
        claim_type_input_example(),
        coordinate=instance.accepted_coordinate(),
    )

    assert len(lint.warnings) == 1
    warning = lint.warnings[0]
    assert warning.code == "playbill.claim_type.evidence_policy_admits_no_accepted_contract"
    assert warning.field_path == "$.evidence_admission_policy.rules"
    assert warning.replacement_rule_fragment["capture_contract_digests"] == [
        warning.contract_digest
    ]


def test_claim_type_source_intent_produces_an_actionable_per_source_warning(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    input_value = ClaimTypeInputV1.model_validate(
        {
            **claim_type_input_example().model_dump(mode="json"),
            "anticipated_source_ids": ["corpus.runbook"],
        }
    )

    lint = lint_claim_type_input(
        instance,
        input_value,
        coordinate=instance.accepted_coordinate(),
    )

    source_warning = next(
        item
        for item in lint.warnings
        if item.code == "playbill.claim_type.anticipated_source_contract_omitted"
    )
    assert source_warning.source_id == "corpus.runbook"
    assert source_warning.contract_identity.endswith("corpus.runbook")
    assert source_warning.replacement_rule_fragment["capture_contract_digests"] == [
        source_warning.contract_digest
    ]
