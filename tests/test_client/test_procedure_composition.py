"""Sequence authoring and inspection exercise the shared graph and SDK paths."""

from dataclasses import replace

import pytest

from cruxible_client.authoring.inputs import CarriedContractInput, lower_authoring_input
from cruxible_client.authoring.procedures import (
    Call,
    EmitCapture,
    Guard,
    Halt,
    Output,
    Previous,
    ProcedureCompositionError,
    Project,
    ProviderBinding,
    Sequence,
    Source,
    Transform,
)
from cruxible_client.authoring.sdk import Playbill
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.models import (
    GuardPredicateV1,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureHardCapsV3,
    TransformAdapterSpecV1,
)

EMPTY = CarriedContractInput(name="empty", fields={})
NUMBER = CarriedContractInput(name="number", fields={"count": PropertySchema(type="int")})
BUDGET = ProcedureBudgetV3(
    wall_clock=CanonicalDurationV1(microseconds=1_000_000),
    max_provider_calls=1,
    max_capture_bytes=1024,
)
CAPS = ProcedureHardCapsV3(
    max_wall_clock=BUDGET.wall_clock,
    max_provider_calls=1,
    max_capture_bytes=1024,
    max_items=100,
    max_repeat_attempts=1,
)
PROVIDER = ProviderBinding(
    provider="example",
    interface="example.call",
    interface_digest="sha256:" + "1" * 64,
    implementation_digest="sha256:" + "2" * 64,
)


def sequence(*steps, **kwargs):
    return Sequence(
        steps,
        name="demo",
        contract_in=EMPTY,
        contract_out=NUMBER,
        budget=BUDGET,
        hard_caps=CAPS,
        **kwargs,
    )


def test_sequence_wires_previous_and_uses_sdk_lowering_without_transport():
    blueprint = sequence(
        Project("first", fields={"count": 1}, contract_out=NUMBER),
        Call("second", contract_in=NUMBER, contract_out=NUMBER),
    )
    assert not blueprint.preview().ready_for_prepare
    assert "provider" not in blueprint.preview().nodes[1]
    bound = blueprint.bind(second=PROVIDER)
    assert blueprint.steps[1].provider is None
    preview = bound.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.edges == {"first": {"next": "second"}, "second": {}}
    assert preview.nodes[1]["input"] == "$steps.first"
    assert preview.terminals == ("second",)
    assert preview.pending_checks
    assert "sha256:" not in str(preview.nodes[0])  # No synthetic pins escape.
    draft = Playbill.procedure(object(), definition=bound)
    assert draft.payload == lower_authoring_input(bound.build())
    assert (
        draft.payload.definition["nodes"][1]["provider"]["resolution"] == "accepted_at_intent_base"
    )


def test_source_project_emit_matches_security_dogfood_shape():
    plan = sequence(
        Source("fetch", capture_contract="security.http", request={"url": "https://example.org"}),
        Project("summary", contract_out=NUMBER, fields={"count": 1}),
        EmitCapture("emit", capture_contract="security.evidence", input=Output("fetch")),
    ).bind(fetch=PROVIDER)
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.terminals == ("emit",)
    assert preview.nodes[-1]["input"] == "$steps.fetch"
    assert preview.returns == "summary"
    assert len(plan.build().contracts) == 2


def test_unbound_provider_refuses_before_authoring():
    plan = sequence(Call("call", contract_in=EMPTY, contract_out=NUMBER))
    with pytest.raises(ProcedureCompositionError, match="Bind an accepted"):
        Playbill.procedure(object(), definition=plan)
    with pytest.raises(ValueError, match="not a provider-backed"):
        plan.bind(typo=PROVIDER)


def test_existing_node_identities_survive_sequence_adoption():
    plan = sequence(
        Source("observation", node_id="fetch", next="retain", capture_contract="http"),
        Project("result", node_id="retain", next="emit", fields={"count": 1}, contract_out=NUMBER),
        EmitCapture("emit", capture_contract="evidence", input=Output("observation")),
    ).bind(observation=PROVIDER)
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.edges == {"fetch": {"next": "retain"}, "retain": {"next": "emit"}, "emit": {}}
    assert preview.nodes[0]["as"] == "observation"
    assert preview.nodes[1]["as"] == "result"
    assert preview.nodes[2]["input"] == "$steps.observation"
    assert preview.returns == "result"

    # Existing whole-output wiring still follows aliases, not node identities.
    calls = sequence(
        Project("value", node_id="produce", fields={"count": 1}, contract_out=NUMBER),
        Call(
            "answer", node_id="consume", contract_in=NUMBER, contract_out=NUMBER, provider=PROVIDER
        ),
    )
    assert calls.preview().ready_for_prepare
    assert calls.preview().nodes[1]["input"] == "$steps.value"
    assert (
        not replace(calls, steps=(calls.steps[0], replace(calls.steps[1], node_id="produce")))
        .preview()
        .ready_for_prepare
    )


def test_auto_wiring_rejects_incompatible_carried_contracts():
    text = CarriedContractInput(name="text", fields={"count": PropertySchema(type="string")})
    plan = sequence(
        Project("first", fields={"count": 1}, contract_out=NUMBER),
        Call("second", contract_in=text, contract_out=NUMBER, provider=PROVIDER),
    )
    assert any(d.code == "contract_mismatch" and d.step == "second" for d in plan.preview().errors)
    with pytest.raises(ProcedureCompositionError):
        plan.build()


def test_literal_fields_use_existing_contract_validator():
    plan = sequence(Project("bad", fields={"count": "wrong"}, contract_out=NUMBER))
    assert any(d.code == "contract_value_invalid" for d in plan.preview().errors)


def test_transform_adapter_uses_value_contract_not_spec_envelope():
    plan = sequence(
        Transform(
            "adapt",
            transform_kind="adapter",
            contract_in=NUMBER,
            contract_out=NUMBER,
            spec=TransformAdapterSpecV1(value={"count": 2}),
        )
    )
    assert plan.preview().ready_for_prepare, plan.preview().errors


def test_existing_guard_branches_are_inspected_not_reimplemented():
    predicate = GuardPredicateV1(
        left=PredicateOperandV1(kind="step", alias="seed", path=("count",)),
        operator="gt",
        right=PredicateOperandV1(kind="literal", value=0),
    )
    plan = sequence(
        Project("seed", fields={"count": 1}, contract_out=NUMBER),
        Guard("check", predicate=predicate, on_true="yes", on_false="no"),
        Project("yes", fields={"count": 2}, contract_out=NUMBER, next="emit"),
        Halt("no"),
        EmitCapture("emit", capture_contract="evidence", input=Output("yes")),
        returns="yes",
    )
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.edges["check"] == {"on_false": "no", "on_true": "yes"}
    assert set(preview.terminals) == {"no", "emit"}


def test_guard_merge_rejects_branch_only_alias():
    predicate = GuardPredicateV1(
        left=PredicateOperandV1(kind="literal", value=True),
        operator="eq",
        right=PredicateOperandV1(kind="literal", value=True),
    )
    plan = sequence(
        Project("seed", fields={"count": 1}, contract_out=NUMBER),
        Guard("check", predicate=predicate, on_true="yes", on_false="emit"),
        Project("yes", fields={"count": 2}, contract_out=NUMBER),
        EmitCapture("emit", capture_contract="evidence", input=Previous()),
    )
    assert any("every path" in d.message for d in plan.preview().errors)


@pytest.mark.parametrize(
    "steps",
    [
        (
            Project("same", fields={"count": 1}, contract_out=NUMBER),
            Project("same", fields={"count": 2}, contract_out=NUMBER),
        ),
        (
            Project("first", fields={"count": 1}, contract_out=NUMBER),
            EmitCapture("emit", capture_contract="evidence"),
            Project("unreachable", fields={"count": 2}, contract_out=NUMBER),
        ),
        (Project("first", fields={"count": 1}, contract_out=NUMBER, next="missing"),),
    ],
)
def test_invalid_graphs_use_shared_validator(steps):
    with pytest.raises(ProcedureCompositionError):
        sequence(*steps).build()


def test_new_binding_does_not_mutate_existing_preview():
    plan = sequence(Call("call", contract_in=EMPTY, contract_out=NUMBER)).bind(call=PROVIDER)
    before = plan.preview().model_dump(mode="json")
    after = plan.bind(
        call=PROVIDER.model_copy(update={"implementation_digest": "sha256:" + "3" * 64})
    )
    assert plan.preview().model_dump(mode="json") == before
    assert after.preview().nodes != plan.preview().nodes


def test_conflicting_owned_contracts_refused():
    different = NUMBER.model_copy(update={"fields": {"count": PropertySchema(type="string")}})
    plan = sequence(Project("result", fields={"count": "one"}, contract_out=different))
    with pytest.raises(ProcedureCompositionError, match="Conflicting definitions"):
        plan.build()


def test_contract_budget_checked_by_existing_graph_model():
    plan = replace(
        sequence(Project("result", fields={"count": 1}, contract_out=NUMBER)),
        budget=BUDGET.model_copy(update={"max_provider_calls": 2}),
    )
    assert any("hard cap" in d.message for d in plan.preview().errors)


def test_discovery_requires_explicit_choice_when_multiple_providers_exist():
    from cruxible_client.contracts import (
        PlaybillProviderInterfaceEntry,
        PlaybillProviderInterfaceImplementation,
    )

    entry = PlaybillProviderInterfaceEntry(
        tag="playbill-provider-interface-entry-v1",
        identity="ProviderInterface:web.fetch",
        artifact_digest="sha256:" + "a" * 64,
        artifact_kind="ProviderInterface",
        pin_role="provider-interface",
        interface_digest=PROVIDER.interface_digest,
        vocabulary_digest="sha256:" + "b" * 64,
        classifier_digest="sha256:" + "c" * 64,
        effect_class="external_read",
        classifier_status="installed",
        interface_basis="accepted_registration",
        providers=[
            PlaybillProviderInterfaceImplementation(
                provider_identity="Provider:" + name,
                provider_artifact_digest="sha256:" + "d" * 64,
                implementation_digest=PROVIDER.implementation_digest,
            )
            for name in ("first", "second")
        ],
    )
    with pytest.raises(ValueError, match="exactly one"):
        ProviderBinding.from_interface(entry)
    binding = ProviderBinding.from_interface(entry, provider="second")
    assert binding.provider == "Provider:second"
    assert binding.effect_class == "external_read"
    from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
    from cruxible_client.contracts.provider_contracts import ProviderOperationContractV1

    typed_entry = entry.model_copy(
        update={
            "operation_contract": ProviderOperationContractV1(
                input=ContractSchema(fields={"url": PropertySchema(type="string")}),
                output="playbill-provider-result-to-external-capture-v1",
            )
        }
    )
    selected = ProviderBinding.from_interface(typed_entry, provider="second")
    assert selected.input(url="https://example.test").url == "https://example.test"
    with pytest.raises(ValueError, match="url"):
        selected.input(url=False)
    with pytest.raises(ValueError, match="no declared"):
        binding.input()


def test_sdk_changeset_carries_procedure_line_and_mandate_together():
    from datetime import UTC, datetime

    from cruxible_client.authoring.inputs import ProcedureMandateInputV1
    from cruxible_client.authoring.sdk import ChangeSetDraft
    from cruxible_client.contracts.authoring.models import authoring_member_identity

    pb = object.__new__(Playbill)  # These public builders do not require transport.
    plan = sequence(Project("result", fields={"count": 1}, contract_out=NUMBER))
    changes = (
        ChangeSetDraft(pb)
        .procedure(definition=plan)
        .line(name="demo", procedure="demo", acquisition_policy="demo", max_authority="observe")
    )
    changes.procedure_mandate(
        ProcedureMandateInputV1(
            kind="procedure_mandate",
            name="demo",
            procedure_name="demo",
            grants="propose",
            resource_ceiling=CAPS,
            namespace=("captures",),
            valid_from=datetime(2026, 9, 19, tzinfo=UTC),
            expires_at=datetime(2026, 10, 19, tzinfo=UTC),
        )
    )
    compiled = changes._compiled()
    assert {authoring_member_identity(m) for m in compiled.payload.members} == {
        "Procedure:demo",
        "Line:demo",
        "ProcedureMandate:demo",
    }


def test_sequence_through_sdk_authoring_acceptance_and_existing_executor(tmp_path):
    from datetime import UTC, datetime

    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.governance.actor_context import GovernedActorContext
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.service.authoring.documents import (
        service_activate_playbill_proposal,
        service_submit_playbill_approval,
    )
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequestV2,
        service_run_playbill_procedure,
    )
    from tests.core_support._support import initialize_local
    from tests.test_ledger.test_activation import _sign

    instance, owner = initialize_local(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    plan = sequence(Project("result", fields={"count": 7}, contract_out=NUMBER))
    draft = Playbill.procedure(object(), definition=plan)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    compiled = coordinator.compile(
        actor=actor, payload=draft.payload, canonical_timestamp="2026-09-19T12:00:00.000000Z"
    )
    assert compiled.verdict == "passed", compiled.frontier
    intent = coordinator.list_pending(actor=actor).intents[0]
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    proposal = submitted.status.proposal_id
    approval = _sign(
        owner, submitted.status.candidate_digest, instance.accepted_coordinate().semantic_root
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    service_activate_playbill_proposal(instance, proposal_id=proposal, activated_by="owner")
    now = datetime(2026, 9, 19, 12, 5, tzinfo=UTC)
    result = service_run_playbill_procedure(
        instance,
        name="demo",
        request=ProcedureRunRequestV2(evaluation_time=now, input={}),
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="owner",
            org_id=instance.descriptor.instance_id,
            operation_id="sequence-dogfood",
            timestamp=now,
        ),
    )
    assert result.status == "succeeded", result
    assert result.result == {"count": 7}
