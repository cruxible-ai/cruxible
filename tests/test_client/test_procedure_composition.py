"""The thin typed builder states one ProcedureSpec; the shared lowering checks it."""

import pytest

from cruxible_client.authoring.inputs import lower_authoring_input
from cruxible_client.authoring.procedures import (
    Handle,
    ProcedureCompositionError,
    ProcedureLimits,
    Sequence,
    eq,
    gt,
    item,
    lower_procedure_spec,
)
from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.contracts.capabilities import Capability
from cruxible_client.contracts.procedures.authored import ProcedureSpecError
from cruxible_client.contracts.procedures.slots import ProcedureSlot
from cruxible_client.contracts.refs import Ref, step_ref
from cruxible_client.contracts.shapes import Contract
from tests.support.procedures import abort, emit, goto, guard, halt, halt_step, project

# Executor-only fixtures: these tests author Procedure step kinds the template
# closes at authoring (D-R37 h1) to cover their executor handlers (h3).
pytestmark = pytest.mark.executor_only

LIMITS = ProcedureLimits(wall_clock="PT60S", provider_calls=1, capture_bytes=1024)
CAPS = ProcedureLimits(
    wall_clock="PT60S", provider_calls=1, capture_bytes=1024, items=100, repeat_attempts=1
)
#: The one slot every sequence below declares for its call steps. A Line binds
#: a slot to an exact {build, operation} later (ruling C3).
PROVIDER_SLOT = "backend"
SLOTS = {PROVIDER_SLOT: ProcedureSlot(capability="test.procedure-composition@1")}
#: The capability the slot names, as an author supplies it to the builder.
CAPABILITY = Capability.model_validate(
    {
        "name": "test.procedure-composition",
        "version": 1,
        "description": "Answer the composition test's request.",
        "effect": "external_read",
        "in": "test.procedure-composition.request@1",
        "out": "test.procedure-composition.response@1",
    }
)


def sequence(**kwargs):
    kwargs.setdefault("hard_caps", CAPS)
    return Sequence("demo", input={}, output={"count": "int"}, **kwargs)


def test_a_sequence_states_one_spec_and_previews_it_with_the_shared_lowering():
    plan = sequence(slots=SLOTS)
    first = project(plan, "first", fields={"count": 1}, output={"count": "int"})
    plan.call("second", slot=PROVIDER_SLOT, input=first.whole)
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.edges == {"first": {"next": "second"}, "second": {}}
    assert preview.nodes[1]["slot"] == PROVIDER_SLOT
    assert preview.nodes[1]["input"] == "$steps.first"
    assert preview.terminals == ("second",)
    assert preview.pending_checks
    assert "sha256:" not in str(preview.nodes[0])  # No synthetic pins escape.
    # The builder constructs the spec directly: no graph, no decompile, no relower.
    built = plan.build()
    assert built.spec == plan.spec() == preview.spec
    assert built.spec.steps[1].input == step_ref("first")
    draft = Cruxible.procedure(object(), definition=plan)
    assert draft.payload == lower_authoring_input(built)
    current = lower_procedure_spec(built.spec)
    assert current.definition["nodes"][1]["slot"] == PROVIDER_SLOT
    assert current.definition["graph_format"] == 8


def test_handles_are_typed_refs_checked_against_the_fields_a_step_produces():
    out = Contract.model_validate(
        {
            "name": "test.procedure-composition.response",
            "version": 1,
            "fields": {"count": "int", "label": "string"},
        }
    )
    plan = sequence(
        slots=SLOTS,
        params={"limit": "int"},
        capabilities={"test.procedure-composition@1": CAPABILITY},
        contracts={"test.procedure-composition.response@1": out},
    )
    answer = plan.call("answer", slot=PROVIDER_SLOT, input={})
    assert answer["count"] == Ref.model_validate(
        {"$ref": "step", "step": "answer", "path": ["count"]}
    )
    with pytest.raises(KeyError, match="did you mean count"):
        answer["cuont"]
    shaped = project(plan, "shaped", fields={"count": answer["count"]}, output={"count": "int"})
    with pytest.raises(KeyError, match="no field 'label'"):
        shaped["label"]
    assert plan.params["limit"].graph_text() == "$params.limit"
    with pytest.raises(KeyError):
        plan.input["anything"]  # the declared input has no fields
    rows = plan.read("rows", query="test.rows")
    assert rows["truncated"].render() == "$steps.rows.truncated"
    assert isinstance(rows, Handle) and "__getattr__" not in vars(Handle)
    assert item("fields", "status").graph_text() == "$item.fields.status"


def test_source_project_emit_matches_security_dogfood_shape():
    plan = sequence(slots=SLOTS)
    fetch = plan.call(
        "fetch",
        captures={"capture": "security.http"},
        input={"url": "https://example.org"},
        slot=PROVIDER_SLOT,
    )
    project(plan, "summary", output={"count": "int"}, fields={"count": 1})
    emit(plan, "security.evidence", input=fetch.whole, result=step_ref("summary"), id="emit")
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.terminals == ("emit",)
    assert preview.nodes[-1]["input"] == "$steps.fetch"
    assert preview.returns == "summary"
    # The boundaries are co-proposed (input, output); the intermediate project pins none.
    assert set(lower_procedure_spec(plan.build().spec).generated) == {"input", "output"}


def test_undeclared_slot_refuses_before_authoring():
    plan = sequence(slots={})
    plan.call("call", slot="missing", input={})
    preview = plan.preview()
    assert not preview.ready_for_prepare
    assert any(d.code == "procedure_slot_undeclared" for d in preview.errors)
    with pytest.raises(ProcedureCompositionError):
        Cruxible.procedure(object(), definition=plan)


def test_existing_node_identities_survive_sequence_adoption():
    plan = sequence(slots=SLOTS)
    observation = plan.call(
        "observation",
        id="fetch",
        next="retain",
        captures={"capture": "http"},
        slot=PROVIDER_SLOT,
        input={},
    )
    project(plan, "result", id="retain", next="emit", fields={"count": 1})
    emit(plan, "evidence", input=observation.whole, result=step_ref("result"), id="emit")
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.edges == {"fetch": {"next": "retain"}, "retain": {"next": "emit"}, "emit": {}}
    assert preview.nodes[0]["as"] == "observation"
    assert preview.nodes[1]["as"] == "result"
    assert preview.nodes[2]["input"] == "$steps.observation"
    assert preview.returns == "result"

    # Whole-output wiring follows aliases, not node identities.
    calls = sequence(slots=SLOTS)
    value = project(calls, "value", id="produce", fields={"count": 1})
    calls.call("answer", id="consume", slot=PROVIDER_SLOT, input=value.whole)
    assert calls.preview().ready_for_prepare
    assert calls.preview().nodes[1]["input"] == "$steps.value"
    clash = sequence(slots=SLOTS)
    value = project(clash, "value", id="produce", fields={"count": 1})
    clash.call("answer", id="produce", slot=PROVIDER_SLOT, input=value.whole)
    assert not clash.preview().ready_for_prepare


def test_declared_fields_are_checked_against_their_literal_values():
    plan = sequence()
    project(plan, "bad", fields={"count": "wrong"}, output={"count": "int"})
    errors = plan.preview().errors
    assert [d.code for d in errors] == ["procedure_operand_type_mismatch"]


def test_guard_branches_are_typed_actions_inspected_by_the_shared_graph_law():
    plan = sequence()
    seed = project(plan, "seed", fields={"count": 1}, output={"count": "int"})
    guard(plan, gt(seed["count"], 0), then=goto("yes"), else_=goto("no"), id="check")
    yes = project(plan, "yes", fields={"count": 2}, next="emit")
    halt_step(plan, None, id="no")
    emit(plan, "evidence", input=yes.whole, result=yes.whole, id="emit")
    plan.returns(yes)
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.edges["check"] == {"on_false": "no", "on_true": "yes"}
    assert set(preview.terminals) == {"no", "emit"}
    # An abort arm carries its typed diagnostic into the stored guard.
    aborting = sequence()
    seed = project(aborting, "seed", fields={"count": 1}, output={"count": "int"})
    guard(aborting, gt(seed["count"], 0), else_=abort("empty", "Nothing to count"))
    project(aborting, "result", fields={"count": seed["count"]})
    guard_node = aborting.preview().nodes[1]
    assert (guard_node["on_false"], guard_node["refusal_code"]) == ("$abort", "empty")
    halting = sequence()
    seed = project(halting, "seed", fields={"count": 1}, output={"count": "int"})
    guard(halting, gt(seed["count"], 0), else_=halt("empty", message="Nothing remains"))
    project(halting, "result", fields={"count": seed["count"]})
    assert halting.preview().nodes[-1] == {
        "kind": "halt",
        "node_id": "step_2_halt",
        "reason": "empty",
        "message": "Nothing remains",
    }


def test_guard_merge_rejects_branch_only_alias():
    plan = sequence()
    project(plan, "seed", fields={"count": 1}, output={"count": "int"})
    guard(plan, eq(True, True), then=goto("yes"), else_=goto("emit"), id="check")
    yes = project(plan, "yes", fields={"count": 2})
    emit(plan, "evidence", input=yes.whole, result=yes.whole, id="emit")
    assert any("every path" in d.message for d in plan.preview().errors)


def _duplicate(plan):
    project(plan, "same", fields={"count": 1})
    project(plan, "same", fields={"count": 2})


def _unreachable(plan):
    first = project(plan, "first", fields={"count": 1})
    emit(plan, "evidence", input=first.whole, result=first.whole)
    project(plan, "unreachable", fields={"count": 2})


def _missing_target(plan):
    project(plan, "first", fields={"count": 1}, next="missing")


@pytest.mark.parametrize("compose", [_duplicate, _unreachable, _missing_target])
def test_invalid_graphs_use_shared_validator(compose):
    plan = sequence()
    compose(plan)
    with pytest.raises(ProcedureCompositionError):
        plan.build()


def test_a_returned_record_that_breaks_the_declared_output_is_refused():
    plan = sequence()
    project(
        plan,
        "first",
        fields={"count": 1},
        output={"count": "int"},
    )
    second = project(
        plan,
        "second",
        fields={"count": "x"},
        output={"count": "string"},
    )
    plan.returns(second)
    # The returned record is typed as it flows; it must fit the declared output.
    with pytest.raises(ProcedureCompositionError, match="does not fit the declared output"):
        plan.build()


def test_a_sequence_declares_hard_caps_only():
    # The operating budget is the Line's, within these caps (B4-0 limits).
    plan = sequence(hard_caps=LIMITS)
    project(plan, "result", fields={"count": 1})
    lowered = lower_procedure_spec(plan.build().spec).definition
    assert "budget" not in lowered
    assert lowered["hard_caps"]["max_provider_calls"] == LIMITS.provider_calls


def test_sdk_changeset_carries_procedure_line_and_mandate_together():
    from datetime import UTC, datetime

    from cruxible_client.authoring.inputs import ProcedureMandateInput
    from cruxible_client.authoring.sdk import ChangeSetDraft
    from cruxible_client.contracts.authoring.models import authoring_member_identity

    pb = object.__new__(Cruxible)  # These public builders do not require transport.
    plan = sequence()
    project(plan, "result", fields={"count": 1})
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
    plan = sequence()
    project(plan, "result", fields={"count": 7})
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


def test_claims_step_is_terminal_and_derives_maximum_authority():
    from cruxible_client.contracts.procedures.authored import ClaimsSubjectSpec, ClaimTemplateSpec

    plan = sequence(slots=SLOTS)
    fetched = plan.call(
        "result",
        captures={"capture": "security.http"},
        input={"url": "https://example.org"},
        slot=PROVIDER_SLOT,
    )
    entries = plan.extract(
        "entries",
        capture=fetched["capture"],
        records="entries",
    )
    project(plan, "summary", output={"count": "int"}, fields={"count": 1})
    plan.claims(
        (ClaimTemplateSpec(type="catalog.part.in_stock", value=True),),
        for_each=entries["records"],
        subject=ClaimsSubjectSpec(kind="catalog.part", key={"slug": "part"}),
        id="settle",
    )
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.terminals == ("settle",)
    assert preview.nodes[-1]["kind"] == "claims"
    assert lower_procedure_spec(plan.build().spec).definition["terminal_capability"] == 3
    for retired in (plan.propose, plan.settle):
        with pytest.raises(
            ProcedureSpecError, match=r"Use the claims node via \.claims\(\)\."
        ) as refused:
            retired()
        assert refused.value.code == "cruxible.authoring.retired_node"
        assert refused.value.field_path == retired.__name__


def test_a_post_inbox_step_reading_an_unproduced_output_is_refused():
    plan = sequence()
    project(plan, "result", fields={"count": 1})
    plan.inbox(
        "test.inbox-note@1",
        fields={"note": step_ref("missing", "count")},
        max_items=5,
        id="inbox",
    )
    preview = plan.preview()
    assert not preview.ready_for_prepare
    assert [error.code for error in preview.errors] == ["procedure_ref_unknown"]
    assert "missing" in preview.errors[0].message
    with pytest.raises(ProcedureCompositionError) as caught:
        plan.build()
    assert caught.value.preview.errors == preview.errors


def test_a_valid_post_inbox_step_ends_its_path_and_preserves_its_bounds():
    from cruxible_client.contracts.line_inbox import INBOX_RUN_ITEM_CAP_MAX

    plan = sequence()
    listed = project(
        plan,
        "listed",
        fields={"items": [{"note": "a"}]},
    )
    project(plan, "result", fields={"count": 1})
    plan.inbox(
        "test.inbox-note@1",
        for_each=listed["items"],
        fields={"note": item("note")},
        dedupe_key=("note",),
        max_items=5,
        id="inbox",
    )
    preview = plan.preview()
    assert preview.terminals == ("inbox",)
    assert preview.authority == "observe"
    node = lower_procedure_spec(plan.build().spec).definition["nodes"][-1]
    assert node == {
        "kind": "post_inbox",
        "node_id": "inbox",
        "contract": "test.inbox-note@1",
        "for_each": "$steps.listed.items",
        "fields": {"note": "$item.note"},
        "dedupe_key": ["note"],
        "max_items": 5,
    }

    unbounded = sequence()
    unbounded.inbox("test.inbox-note@1", fields={"note": "x"}, id="inbox")
    assert "max_items" not in lower_procedure_spec(unbounded.build().spec).definition["nodes"][0]
    over = sequence()
    over.inbox(
        "test.inbox-note@1", fields={"note": "x"}, max_items=INBOX_RUN_ITEM_CAP_MAX + 1, id="inbox"
    )
    assert [d.code for d in over.preview().errors] == ["graph_invalid"]


def test_claims_templates_lower_typed_subject_keys_and_values():
    from cruxible_client.contracts.procedures.authored import ClaimsSubjectSpec, ClaimTemplateSpec

    plan = sequence(slots=SLOTS)
    fetched = plan.call(
        "parts",
        captures={"capture": "security.http"},
        input={"url": "https://example.org"},
        slot=PROVIDER_SLOT,
    )
    entries = plan.extract(
        "entries",
        capture=fetched["capture"],
        records="entries",
    )
    project(plan, "summary", output={"count": "int"}, fields={"count": 1})
    plan.claims(
        (ClaimTemplateSpec(type="catalog.part.in_stock", value=item("fields", "in_stock")),),
        for_each=entries["records"],
        subject=ClaimsSubjectSpec(kind="catalog.part", key={"slug": item("key", "part_id")}),
        id="write",
    )
    node = lower_procedure_spec(plan.build().spec).definition["nodes"][-1]
    assert node["kind"] == "claims"
    assert node["subject"]["key"]["slug"] == "$item.key.part_id"
    assert node["claims"][0]["value"] == "$item.fields.in_stock"
    assert "candidate_templates" not in node
