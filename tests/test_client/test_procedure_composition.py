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
    Sequence,
    Source,
    Transform,
    lower_procedure_spec,
)
from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.models import (
    GuardPredicate,
    PredicateOperand,
    ProcedureBudget,
    ProcedureHardCaps,
    TransformAdapterSpec,
)
from cruxible_client.contracts.provider_operations import ProcedureSlot
from tests.support.procedures import RUN_WALL_CLOCK

EMPTY = CarriedContractInput(name="empty", fields={})
NUMBER = CarriedContractInput(name="number", fields={"count": PropertySchema(type="int")})
BUDGET = ProcedureBudget(
    wall_clock=RUN_WALL_CLOCK,
    max_provider_calls=1,
    max_capture_bytes=1024,
)
CAPS = ProcedureHardCaps(
    max_wall_clock=BUDGET.wall_clock,
    max_provider_calls=1,
    max_capture_bytes=1024,
    max_items=100,
    max_repeat_attempts=1,
)
#: The one slot every sequence below declares for its Call/Source steps. A
#: Line binds a slot to an exact {build, operation} later (ruling C3);
#: nothing about which Provider serves it is an authoring-time concern any
#: more (ProviderBinding/.bind()/ProviderInterface discovery are gone, B2
#: noun removal), so every sequence here declares it directly.
PROVIDER_SLOT = "backend"
SLOTS = {
    PROVIDER_SLOT: ProcedureSlot(
        **{
            "in": "test.procedure-composition.request@1",
            "out": "test.procedure-composition.response@1",
            "effect": "external_read",
        }
    )
}


def _node_field(node, name):
    """A preview node is a typed model when it fully validates, else a dict."""

    return node["as" if name == "as_" else name] if isinstance(node, dict) else getattr(node, name)


def sequence(*steps, **kwargs):
    if any(isinstance(step, (Call, Source)) for step in steps):
        kwargs.setdefault("slots", SLOTS)
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
    plan = sequence(
        Project("first", fields={"count": 1}, contract_out=NUMBER),
        Call("second", slot=PROVIDER_SLOT),
    )
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.edges == {"first": {"next": "second"}, "second": {}}
    second = preview.nodes[1]
    assert second.slot == PROVIDER_SLOT
    assert second.input == "$steps.first"
    assert preview.terminals == ("second",)
    assert preview.pending_checks
    assert "sha256:" not in str(preview.nodes[0])  # No synthetic pins escape.
    draft = Cruxible.procedure(object(), definition=plan)
    assert draft.payload == lower_authoring_input(plan.build())

    current = lower_authoring_input(lower_procedure_spec(plan.build().spec))
    assert current.definition["nodes"][1]["slot"] == PROVIDER_SLOT
    assert current.definition["graph_format"] == 7


def test_source_project_emit_matches_security_dogfood_shape():
    plan = sequence(
        Source(
            "fetch",
            capture_contract="security.http",
            request={"url": "https://example.org"},
            slot=PROVIDER_SLOT,
        ),
        Project("summary", contract_out=NUMBER, fields={"count": 1}),
        EmitCapture("emit", capture_contract="security.evidence", input=Output("fetch")),
    )
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.terminals == ("emit",)
    assert _node_field(preview.nodes[-1], "input") == "$steps.fetch"
    assert preview.returns == "summary"
    assert plan.build().spec is not None
    assert len(lower_procedure_spec(plan.build().spec).contracts) == 2


def test_undeclared_slot_refuses_before_authoring():
    plan = Sequence(
        (Call("call", slot="missing"),),
        name="demo",
        contract_in=EMPTY,
        contract_out=NUMBER,
        budget=BUDGET,
        hard_caps=CAPS,
    )
    preview = plan.preview()
    assert not preview.ready_for_prepare
    assert any(d.code == "slot_undeclared" for d in preview.errors)
    with pytest.raises(ProcedureCompositionError):
        Cruxible.procedure(object(), definition=plan)


def test_existing_node_identities_survive_sequence_adoption():
    plan = sequence(
        Source(
            "observation",
            node_id="fetch",
            next="retain",
            capture_contract="http",
            slot=PROVIDER_SLOT,
        ),
        Project("result", node_id="retain", next="emit", fields={"count": 1}, contract_out=NUMBER),
        EmitCapture("emit", capture_contract="evidence", input=Output("observation")),
    )
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.edges == {"fetch": {"next": "retain"}, "retain": {"next": "emit"}, "emit": {}}
    assert _node_field(preview.nodes[0], "as_") == "observation"
    assert _node_field(preview.nodes[1], "as_") == "result"
    assert _node_field(preview.nodes[2], "input") == "$steps.observation"
    assert preview.returns == "result"

    # Existing whole-output wiring still follows aliases, not node identities.
    calls = sequence(
        Project("value", node_id="produce", fields={"count": 1}, contract_out=NUMBER),
        Call("answer", node_id="consume", slot=PROVIDER_SLOT),
    )
    assert calls.preview().ready_for_prepare
    assert calls.preview().nodes[1].input == "$steps.value"
    assert (
        not replace(calls, steps=(calls.steps[0], replace(calls.steps[1], node_id="produce")))
        .preview()
        .ready_for_prepare
    )


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
            spec=TransformAdapterSpec(value={"count": 2}),
        )
    )
    assert plan.preview().ready_for_prepare, plan.preview().errors


def test_existing_guard_branches_are_inspected_not_reimplemented():
    predicate = GuardPredicate(
        left=PredicateOperand(kind="step", alias="seed", path=("count",)),
        operator="gt",
        right=PredicateOperand(kind="literal", value=0),
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
    predicate = GuardPredicate(
        left=PredicateOperand(kind="literal", value=True),
        operator="eq",
        right=PredicateOperand(kind="literal", value=True),
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


def test_sdk_changeset_carries_procedure_line_and_mandate_together():
    from datetime import UTC, datetime

    from cruxible_client.authoring.inputs import ProcedureMandateInput
    from cruxible_client.authoring.sdk import ChangeSetDraft
    from cruxible_client.contracts.authoring.models import authoring_member_identity

    pb = object.__new__(Cruxible)  # These public builders do not require transport.
    plan = sequence(Project("result", fields={"count": 1}, contract_out=NUMBER))
    changes = (
        ChangeSetDraft(pb)
        .procedure(definition=plan)
        .line(name="demo", procedure="demo", max_authority="observe")
    )
    changes.procedure_mandate(
        ProcedureMandateInput(
            kind="procedure_mandate",
            name="demo",
            line_name="demo",
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
        ProcedureRunRequest,
        service_run_playbill_procedure,
    )
    from tests.core_support._support import initialize_local
    from tests.test_ledger.test_activation import _sign

    instance, owner = initialize_local(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    plan = sequence(Project("result", fields={"count": 7}, contract_out=NUMBER))
    draft = Cruxible.procedure(object(), definition=plan)
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
        request=ProcedureRunRequest(evaluation_time=now, input={}),
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


def test_a_settle_step_ends_its_path_and_derives_settle_authority():
    from cruxible_client.authoring.procedures import SettleChangeSet
    from tests.test_procedures.test_procedure_proposal_delivery import item_template

    plan = sequence(
        Source(
            "result",
            capture_contract="security.http",
            request={"url": "https://example.org"},
            slot=PROVIDER_SLOT,
        ),
        Project("summary", contract_out=NUMBER, fields={"count": 1}),
        SettleChangeSet("settle", candidate_templates=(item_template(),)),
    )
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.terminals == ("settle",)
    assert preview.nodes[-1].kind == "settle_change_set"
    assert lower_procedure_spec(plan.build().spec).definition["terminal_capability"] == 3


def test_a_post_inbox_step_ends_its_path_and_is_checked_like_any_step():
    """The original fixture's nonexistent items field must be refused."""

    from cruxible_client.authoring.procedures import PostInbox

    plan = sequence(
        Project("result", fields={"count": 1}, contract_out=NUMBER),
        PostInbox("inbox", input={"items": Output("result", "items")}, max_items=5),
    )
    preview = plan.preview()
    assert not preview.ready_for_prepare
    assert [error.code for error in preview.errors] == ["procedure_ref_unknown"]
    assert preview.errors[0].step == "inbox"
    assert "$.steps[0].inbox.items" in preview.errors[0].message
    assert "Unknown reference 'items'" in preview.errors[0].message
    with pytest.raises(ProcedureCompositionError) as caught:
        plan.build()
    assert caught.value.preview.errors == preview.errors


def test_a_valid_post_inbox_step_ends_its_path_and_preserves_its_bounds():
    from cruxible_client.authoring.procedures import PostInbox
    from cruxible_client.contracts.line_inbox import INBOX_RUN_ITEM_CAP_MAX

    plan = sequence(
        Project("result", fields={"count": 1}, contract_out=NUMBER),
        PostInbox("inbox", input={"items": [{"count": Output("result", "count")}]}, max_items=5),
    )
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.terminals == ("inbox",)
    assert preview.authority == "observe"
    node = lower_procedure_spec(plan.build().spec).definition["nodes"][-1]
    assert node == {
        "kind": "post_inbox",
        "node_id": "inbox",
        "input": {"items": [{"count": "$steps.result.count"}]},
        "max_items": 5,
    }
    assert (
        "max_items"
        not in lower_procedure_spec(sequence(PostInbox("inbox")).build().spec).definition["nodes"][
            0
        ]
    )

    successor = sequence(PostInbox("inbox", next="after"), Halt("after"))
    assert "invalid_next" in [d.code for d in successor.preview().errors]
    over = sequence(PostInbox("inbox", max_items=INBOX_RUN_ITEM_CAP_MAX + 1))
    assert [d.code for d in over.preview().errors] == ["graph_invalid"]


def test_typed_item_builders_emit_the_frozen_tags_and_item_references():
    """Authoring a keyed claim item needs no hand-written internal tag."""

    from cruxible_client.authoring.procedures import (
        Item,
        LiteralClaimObject,
        ProcedureClaimStatement,
        ProcedureKeyedClaimProposalItem,
        SettleChangeSet,
        SubjectKeyReference,
        WorkspaceFileSourceRequestV2,
        _template,
    )

    item = ProcedureKeyedClaimProposalItem(
        statement=ProcedureClaimStatement(
            subject=SubjectKeyReference(subject_kind="catalog.part", key=Item("part_id")),
            predicate="catalog.part.in_stock",
            object=LiteralClaimObject(value=Item("in_stock")),
            role="observation",
        ),
        rationale="The stock file lists the part.",
    )
    lowered = _template(item, None)
    assert lowered["tag"] == "playbill-procedure-claim-proposal-item-v3"
    assert lowered["statement"]["tag"] == "playbill-procedure-claim-statement-v1"
    assert lowered["statement"]["subject"]["key"] == "$item.part_id"
    assert lowered["statement"]["object"] == {"kind": "literal", "value": "$item.in_stock"}
    assert Item() == "$item"
    request = _template(
        WorkspaceFileSourceRequestV2(
            logical_source="workspace.stock",
            relative_path="stock.yaml",
            coordinate_type="workspace-file-v1",
            coordinate={},
            selector_type="whole-file-v1",
            selector={},
        ),
        None,
    )
    assert request["tag"] == "playbill-workspace-file-source-request-v2"

    plan = sequence(
        Source(
            "result",
            capture_contract="security.http",
            request={"url": "https://example.org"},
            slot=PROVIDER_SLOT,
        ),
        Project("summary", contract_out=NUMBER, fields={"count": 1}),
        SettleChangeSet("settle", candidate_templates=(item,)),
    )
    assert plan.preview().ready_for_prepare
    (node,) = [
        n
        for n in lower_procedure_spec(plan.build().spec).definition["nodes"]
        if n["kind"] == "settle_change_set"
    ]
    assert node["candidate_templates"][0]["tag"] == "playbill-procedure-claim-proposal-item-v3"
