"""B2: provider operations, slots, binding at Line acceptance, Mandate -> Line."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import pytest

from cruxible_client.contracts.procedures.line_specs import parse_line_spec
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    service_run_playbill_line,
)
from tests.support.operation_world import (
    LINE,
    NOW,
    Invoker,
    Operator,
    actor,
    author_line,
    author_procedure,
    operation_build,
    slotted_spec,
    world,
)


@dataclass(frozen=True)
class Clock:
    at: datetime

    def now(self) -> datetime:
        return self.at

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def run_line(instance, *, invoker=None, name=LINE):  # type: ignore[no-untyped-def]
    invoker = invoker or Invoker()
    state = service_run_playbill_line(
        instance,
        path_identity_digest=name,
        request=LineRunRequest(line=name),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=Clock(NOW),
        provider_runtime_operator=Operator(invoker),  # type: ignore[arg-type]
    )
    return state, invoker


def accepted_line(instance, name=LINE):  # type: ignore[no-untyped-def]
    path = f"lines/{name}.json"
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return parse_line_spec(tree[path], path=path)


def test_a_line_binds_its_slot_at_acceptance_and_runs_it(tmp_path):
    build = operation_build()
    instance = world(tmp_path, build)
    author_procedure(instance, slotted_spec())
    author_line(instance)
    line = accepted_line(instance)
    assert {key: item.operation for key, item in line.bindings.items()} == {"compute": "compute"}
    assert line.bindings["compute"].build.target == build.identity
    state, invoker = run_line(instance)
    assert state.status == "succeeded", state
    assert invoker.contexts[0].additive["operation"] == "compute"


# --- helpers -----------------------------------------------------------------


def install(instance, *builds):  # type: ignore[no-untyped-def]
    from tests.core_support._identity_support import accept_tree
    from tests.support.operation_world import build_files

    files: dict[str, bytes] = {}
    for build in builds:
        files.update(build_files(build))
    accept_tree(instance, files, name="install-" + builds[0].identity.name.split("/")[1][:12])


def author_mandate(instance, *, grants="propose", name="test.grant"):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
    from cruxible_client.contracts.procedures.artifacts import parse_procedure
    from tests.support.operation_world import PROCEDURE, author

    path = f"procedures/{PROCEDURE}.json"
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    caps = parse_procedure(tree[path], path=path).definition.hard_caps
    return author(
        instance,
        ProcedureMandateInput(
            kind="procedure_mandate",
            name=name,
            line_name=LINE,
            grants=grants,
            resource_ceiling=caps,
            namespace=("claims",),
            valid_from=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
        ),
        name="mandate-" + name,
    )


def accepted_mandate(instance, name="test.grant"):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.procedure_mandates import parse_procedure_mandate

    path = f"procedure-mandates/{name}.json"
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return parse_procedure_mandate(tree[path], path=path)


def line_digest(instance, name=LINE):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.procedures.line_specs import line_spec_digest

    return line_spec_digest(accepted_line(instance, name)).tagged


def slotted_world(tmp_path, *builds):  # type: ignore[no-untyped-def]
    instance = world(tmp_path, *(builds or (operation_build(),)))
    author_procedure(instance, slotted_spec())
    return instance


# --- 1. binding at acceptance; runs never refit -------------------------------


def test_installing_a_newer_build_never_changes_an_accepted_lines_binding(tmp_path):
    from cruxible_client.contracts.providers import provider_digest

    first, second = operation_build(build="a"), operation_build(build="d")
    instance = slotted_world(tmp_path, first)
    author_line(instance)
    install(instance, second)
    # The newer build serves the same signature; the Line still binds the first.
    assert accepted_line(instance).bindings["compute"].build.target == first.identity
    state, invoker = run_line(instance)
    assert state.status == "succeeded", state
    assert invoker.contexts[0].additive["build_digest"] == provider_digest(first).tagged


# --- 2. slot_ambiguous / slot_unfilled at Line acceptance ---------------------


def _refusal(call):  # type: ignore[no-untyped-def]
    from cruxible_core.errors import RequestRefusedError

    with pytest.raises(RequestRefusedError) as refused:
        call()
    return refused.value


def test_line_acceptance_refuses_ambiguous_and_unfilled_slots_listing_candidates(tmp_path):
    first, second = operation_build(build="a"), operation_build(build="d")
    instance = slotted_world(tmp_path, first, second)
    ambiguous = _refusal(lambda: author_line(instance))
    assert ambiguous.error_code == "cruxible.line.slot_ambiguous"
    assert first.identity.name in str(ambiguous) and second.identity.name in str(ambiguous)
    # Naming the build settles it.
    author_line(instance, bindings={"compute": first.identity.name})
    assert accepted_line(instance).bindings["compute"].build.target == first.identity

    (tmp_path / "other").mkdir()
    other = world(tmp_path / "other", operation_build(provider_id="other-provider"))
    author_procedure(other, slotted_spec(effect="external_read"))
    unfilled = _refusal(lambda: author_line(other))
    assert unfilled.error_code == "cruxible.line.slot_unfilled"
    assert "test.value@1 -> test.value@1 (external_read)" in str(unfilled)


# --- 3. provider move: Line successors and propose-only Mandate successors ----


def test_provider_move_carries_lines_and_their_mandates_propose_only(tmp_path):
    from cruxible_client.contracts.provider_installation import ProviderMoveRequest
    from cruxible_core.service.procedures.provider_builds import service_move_provider

    first, second = operation_build(build="a"), operation_build(build="d")
    instance = slotted_world(tmp_path, first)
    author_line(instance)
    author_mandate(instance)
    old_line = line_digest(instance)
    old_head = instance.accepted_coordinate().git_oid
    install(instance, second)
    moved = service_move_provider(
        instance,
        ProviderMoveRequest(provider="demo-provider", to=second.identity.name, dry_run=False),
        actor_id="owner",
        timestamp="2026-10-29T11:30:00.000000Z",
    )
    assert moved.status == "accepted", moved.detail
    assert [(item.identity, item.action) for item in moved.moved] == [(f"Line:{LINE}", "successor")]
    (carried,) = moved.automation_carries
    assert carried.kind == "mandate" and carried.new_grant == "propose"
    line = accepted_line(instance)
    assert line.bindings["compute"].build.target == second.identity
    assert line.lifecycle.predecessor_digest == old_line
    mandate = accepted_mandate(instance)
    assert mandate.line.artifact_digest == line_digest(instance)
    assert mandate.grants == "propose"
    # One change set: the Line and its Mandate moved in the same generation.
    tree_before = instance.tree_at(old_head)
    assert (
        tree_before[f"lines/{LINE}.json"]
        != instance.tree_at(instance.accepted_coordinate().git_oid)[f"lines/{LINE}.json"]
    )


# --- 4. a grant pins one Line version -----------------------------------------


def test_a_line_change_carries_its_mandate_and_the_old_grant_never_follows(tmp_path):
    from cruxible_client.contracts.procedure_mandates import (
        ProcedureMandateInvocation,
        evaluate_procedure_mandate,
        procedure_mandate_digest,
    )

    instance = slotted_world(tmp_path)
    author_line(instance)
    author_mandate(instance)
    granted = accepted_mandate(instance)
    d1 = line_digest(instance)
    # A parameter change (D2): lowering carries the Mandate as a propose-only successor.
    author_line(instance, parameters={"value": 2}, label="line-d2")
    d2 = line_digest(instance)
    assert d2 != d1
    carried = accepted_mandate(instance)
    assert carried.line.artifact_digest == d2
    assert carried.grants == "propose"
    assert carried.lifecycle.predecessor_digest == procedure_mandate_digest(granted).tagged

    # The D1 grant does not cover a run of D2.
    caps = granted.resource_ceiling
    verdict = evaluate_procedure_mandate(
        granted.model_copy(update={"grants": "settle"}),
        ProcedureMandateInvocation(
            line_identity=granted.line.target,
            line_artifact_digest=d2,
            requested_rung=3,
            requested_authority=caps,
            target_paths=("claims/x.json",),
            evaluation_time=NOW,
            accepted_mandate_digest=procedure_mandate_digest(
                granted.model_copy(update={"grants": "settle"})
            ).tagged,
        ),
    )
    assert "procedure_mandate_line_mismatch" in verdict.refusal_codes


def _submit(instance, tree, name):  # type: ignore[no-untyped-def]
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    base = instance.accepted_coordinate()
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-10-29T11:40:00.000000Z",
    )


def test_a_mandate_retargets_only_with_its_line_successor_and_only_to_propose(
    tmp_path, monkeypatch
):
    import cruxible_core.proposals.proposals as proposals
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_digest,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.line_specs import line_spec_digest, render_line_spec

    instance = slotted_world(tmp_path)
    author_line(instance)
    author_mandate(instance)
    line, mandate = accepted_line(instance), accepted_mandate(instance)
    successor = line.model_copy(
        update={
            "parameters": {"value": 3},
            "lifecycle": ArtifactLifecycle(predecessor_digest=line_spec_digest(line).tagged),
        }
    )
    retargeted = mandate.model_copy(
        update={
            "line": mandate.line.model_copy(
                update={"artifact_digest": line_spec_digest(successor).tagged}
            ),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(mandate).tagged
            ),
        }
    )
    tree = dict(instance.tree_at(instance.accepted_coordinate().git_oid))
    # A Line successor that leaves its live Mandate behind is refused.
    alone = {**tree, f"lines/{LINE}.json": render_line_spec(successor)}
    refused = _submit(instance, alone, "alone")
    assert refused.candidate is None
    assert {item.code for item in refused.evaluation.diagnostics} == {
        "cruxible.procedure_mandate.successor_pair_required"
    }
    # A carried Mandate that keeps settle is refused: the grant never follows a Line.
    paired = {**alone, "procedure-mandates/test.grant.json": render_procedure_mandate(retargeted)}
    parse = proposals.parse_procedure_mandate_any

    def as_settle(content, *, path):  # type: ignore[no-untyped-def]
        return parse(content, path=path).model_copy(update={"grants": "settle"})

    monkeypatch.setattr(proposals, "parse_procedure_mandate_any", as_settle)
    widened = _submit(instance, paired, "settle")
    assert widened.candidate is None
    assert "cruxible.procedure_mandate.carried_grant_must_propose" in {
        item.code for item in widened.evaluation.diagnostics
    }
    monkeypatch.setattr(proposals, "parse_procedure_mandate_any", parse)
    assert _submit(instance, paired, "propose").candidate is not None


# --- 5. direct runs: an ephemeral spec, capped below settle -------------------


def test_a_direct_run_records_its_ephemeral_spec_digest(tmp_path, monkeypatch):
    import cruxible_client.contracts.procedures.line_specs as line_specs
    from cruxible_client.contracts.provider_operations import LineBinding, binding_pin
    from cruxible_client.contracts.providers import provider_digest
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequest,
        service_run_playbill_procedure,
    )

    build = operation_build()
    instance = slotted_world(tmp_path, build)
    recorded: list[tuple[object, str]] = []
    digest = line_specs.ephemeral_line_spec_digest

    def record(spec):  # type: ignore[no-untyped-def]
        recorded.append((spec, digest(spec)))
        return recorded[-1][1]

    monkeypatch.setattr(line_specs, "ephemeral_line_spec_digest", record)
    result = service_run_playbill_procedure(
        instance,
        name="test.slotted",
        request=ProcedureRunRequest(input={"value": 4}, evaluation_time=NOW),
        actor_context=actor(),
        provider_runtime_operator=Operator(Invoker()),  # type: ignore[arg-type]
    )
    assert result.status == "succeeded", result
    assert result.result == {"value": 5}
    ((spec, spec_digest),) = recorded
    # The request fitted the slot; the spec records that exact build and operation.
    assert spec.bindings == {
        "compute": LineBinding(
            build=binding_pin(build.identity, provider_digest(build).tagged), operation="compute"
        )
    }
    assert spec.parameters == {"value": 4}
    assert spec_digest.startswith("sha256:")


def test_a_direct_run_never_reaches_mandate_authority(tmp_path):
    """A direct run has no Line, so no Mandate covers it: its terminals stay dark."""

    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from cruxible_core.procedures.nested import authority_line
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequest,
        service_run_playbill_procedure,
    )
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._light_capture_support import procedure_contract

    instance = world(tmp_path)
    contract = procedure_contract()
    accept_tree(
        instance,
        {capture_contract_path(contract.identity.name): render_capture_contract(contract)},
        name="capture-contract",
    )
    author_procedure(instance, emitting_spec())
    result = service_run_playbill_procedure(
        instance,
        name="test.slotted",
        request=ProcedureRunRequest(input={"value": 4}, evaluation_time=NOW),
        actor_context=actor(),
        provider_runtime_operator=Operator(Invoker()),  # type: ignore[arg-type]
    )
    assert result.status != "succeeded"
    assert result.terminal is not None
    from types import SimpleNamespace

    standalone = SimpleNamespace(
        line_spec_digest=None,
        procedure_identity=result.procedure_identity,
        procedure_artifact_digest=result.procedure_artifact_digest,
    )
    import cruxible_core.procedures.nested as nested

    assert nested.nested_admission(standalone) is None  # type: ignore[arg-type]
    assert authority_line(standalone) == (None, None)  # type: ignore[arg-type]


# --- 10. removed nouns are refused, naming their replacement ------------------


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        ("blueprints/demo.json", "Procedure"),
        ("source-acquisition-policies/default.json", "acquisition"),
        ("provider-interfaces/demo.json", "operation"),
    ],
)
def test_authored_removed_nouns_are_refused_naming_their_replacement(tmp_path, path, replacement):
    from cruxible_client.contracts.errors import ProposalAdmissionError
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    instance = world(tmp_path)
    base = instance.accepted_coordinate()
    tree = dict(instance.tree_at(base.git_oid))
    tree[path] = b'{"artifact_format": "removed"}\n'
    with pytest.raises(ProposalAdmissionError) as refused:
        instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=ProposalAdmissionRequest(
                target_ref="refs/proposals/owner/removed", proposed_base_oid=base.git_oid
            ),
            candidate_tree=tree,
            timestamp="2026-10-29T11:40:00.000000Z",
        )
    assert "removed" in str(refused.value) and replacement in str(refused.value)


# --- 6. an invoked child binds through its parent Line ------------------------


def test_an_invoked_child_uses_the_parent_lines_binding_under_its_node_path(tmp_path):
    from cruxible_client.contracts.procedures.authored import ProcedureSpec
    from cruxible_client.contracts.providers import provider_digest

    first, second = operation_build(build="a"), operation_build(build="d")
    instance = slotted_world(tmp_path, first, second)
    parent = ProcedureSpec.model_validate(
        {
            "procedure": "test.parent",
            "input": {"value": "integer"},
            "output": {"value": "integer"},
            "steps": [
                {"invoke": "test.slotted", "id": "child", "input": {"value": "$input.value"}},
                {"project": "finished", "fields": {"value": "$input.value"}},
            ],
            "returns": "finished",
            "budget": {"provider_calls": 1},
        }
    )
    author_procedure(instance, parent, name="parent")
    # The child's slot is keyed under the invoke node; the parent Line names its build.
    author_line(
        instance,
        name="test.parent-line",
        procedure="test.parent",
        bindings={"child.compute": second.identity.name},
    )
    line = accepted_line(instance, "test.parent-line")
    assert set(line.bindings) == {"child.compute"}
    assert line.bindings["child.compute"].build.target == second.identity
    state, invoker = run_line(instance, name="test.parent-line")
    assert state.status == "succeeded", state
    assert [item.additive["build_digest"] for item in invoker.contexts] == [
        provider_digest(second).tagged
    ]


# --- Source worlds: the core-seeded workspace.file operation ------------------

SOURCE_LINE = "test.sourced-line"


def sourced_spec(*, description=None, acquisition_defaults=None):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.procedures.authored import ProcedureSpec

    return ProcedureSpec.model_validate(
        {
            "procedure": "test.sourced",
            **({} if description is None else {"description": description}),
            **(
                {}
                if acquisition_defaults is None
                else {"acquisition_defaults": acquisition_defaults}
            ),
            "input": {},
            "output": {"length": "integer"},
            "slots": {
                "read": {
                    "in": "workspace.file.request@1",
                    "out": "workspace.file.result@1",
                    "effect": "pure",
                }
            },
            "steps": [
                {
                    "source": "doc",
                    "contract": "test.orders-v1",
                    "slot": "read",
                    "request": {
                        "logical_source": "commerce.production.orders",
                        "relative_path": "data/doc.json",
                        "coordinate_type": "database-snapshot-v1",
                        "coordinate": {"lsn": "workspace"},
                        "selector_type": "relation-primary-key-v1",
                        "selector": {"id": 7, "relation": "orders"},
                    },
                },
                {"project": "out", "fields": {"length": "$doc.source.byte_length"}},
            ],
            "returns": "out",
            "budget": {"provider_calls": 1, "capture_bytes": 65536},
        }
    )


def sourced_world(  # type: ignore[no-untyped-def]
    tmp_path, *, acquisition=None, acquisition_defaults=None, bindings=None, builds=()
):
    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._light_capture_support import record_contract

    instance = world(tmp_path, operation_build(), *builds)
    root = tmp_path / "workspace"
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data/doc.json").write_text('{"value": 7}')
    contract = record_contract()
    accept_tree(
        instance,
        {capture_contract_path(contract.identity.name): render_capture_contract(contract)},
        name="capture-contract",
    )
    author_procedure(instance, sourced_spec(acquisition_defaults=acquisition_defaults))
    author_line(
        instance,
        name=SOURCE_LINE,
        procedure="test.sourced",
        parameters={},
        acquisition=acquisition,
        bindings=bindings,
    )
    return instance, root


def run_sourced(instance, root, *, at=NOW, operator=None):  # type: ignore[no-untyped-def]
    from cruxible_core.documents.workspace_file import WorkspaceFileReader
    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

    return service_run_playbill_line(
        instance,
        path_identity_digest=SOURCE_LINE,
        request=LineRunRequest(line=SOURCE_LINE),
        actor_context=actor().model_copy(update={"timestamp": at}),
        caller_rung=2,
        daemon_clock=Clock(at),
        provider_runtime_operator=operator or ProviderRuntimeOperator(root.parent / "state"),
        workspace_file_reader=WorkspaceFileReader(
            instance_id=instance.descriptor.instance_id,
            operating_profile="local",
            attached_roots=(root,),
            managed_roots=(instance.root,),
        ),
    )


def invocation_receipts(instance, run_id):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.provider_execution import ProviderInvocationCompleted
    from cruxible_core.exhaust.payloads import read_payload
    from cruxible_core.service.procedures.measurements import _journal
    from cruxible_core.storage.cas import BodyAccessContext

    journal, stream = _journal(instance)
    found = []
    for stored in journal.select_records(stream, event_kind="provider_invocation_completed"):
        payload = read_payload(
            instance.body_store(),
            stored.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
        completed = ProviderInvocationCompleted.model_validate(payload)
        if completed.receipt.run_id == run_id:
            found.append(completed.receipt)
    return found


def replay(instance, state):  # type: ignore[no-untyped-def]
    """Rebuild the run's execution plan from its recorded bindings alone."""

    from cruxible_core.service.procedures.measurements import _replay_measured_run
    from cruxible_core.service.procedures.procedure_runs import (
        _accepted_procedure,
        load_playbill_procedure_run_grain,
    )

    grain = load_playbill_procedure_run_grain(instance, run_id=state.run_id)
    coordinate = instance.accepted_coordinate()
    accepted = _accepted_procedure(
        instance, name=state.procedure_identity.name, coordinate=coordinate
    )
    return grain, _replay_measured_run(
        instance, accepted=accepted, grain=grain, observation=coordinate
    )


# --- 7. executor parity at the provider boundary ------------------------------


def test_an_ordinary_call_records_operation_fields_and_replays(tmp_path):
    from cruxible_client.contracts.provider_operations import operation_signature_digest
    from cruxible_client.contracts.providers import provider_digest

    build = operation_build()
    instance = slotted_world(tmp_path, build)
    author_line(instance)
    state, _ = run_line(instance)
    assert state.status == "succeeded", state
    (receipt,) = invocation_receipts(instance, state.run_id)
    (served,) = build.operations
    assert receipt.operation == "compute"
    assert receipt.operation_signature_digest == operation_signature_digest(served.signature())
    assert receipt.provider_artifact_digest == provider_digest(build).tagged
    grain, replayed = replay(instance, state)
    (binding,) = grain.provider_bindings
    assert (binding.slot, binding.operation) == ("compute", "compute")
    assert replayed.procedure.pins == state.receipt.validated_pins


def test_a_captured_source_call_records_operation_fields_and_replays(tmp_path):
    from cruxible_client.contracts.workspace_file import workspace_file_signature_digest

    instance, root = sourced_world(tmp_path)
    state = run_sourced(instance, root)
    assert state.status == "succeeded", state.terminal
    assert state.result == {"length": len('{"value": 7}')}
    (receipt,) = invocation_receipts(instance, state.run_id)
    assert receipt.operation == "workspace.file"
    assert receipt.operation_signature_digest == workspace_file_signature_digest()
    (observed,) = state.source_observations
    assert observed.capture_digest is not None
    _grain, replayed = replay(instance, state)
    assert replayed.procedure.pins == state.receipt.validated_pins


def _refusing_world(tmp_path, refusals):  # type: ignore[no-untyped-def]
    from tests.support.operation_world import operation

    build = operation_build(operations=(operation(refusals=refusals),))
    instance = slotted_world(tmp_path, build)
    author_line(instance)
    return instance


@pytest.mark.parametrize("absorbable", [True, False])
def test_a_declared_typed_refusal_is_a_node_refusal_with_its_code(tmp_path, absorbable):
    instance = _refusing_world(tmp_path, {"provider_declined": absorbable})
    state, _ = run_line(instance, invoker=Invoker(refusal=("provider_declined", {"reason": "x"})))
    assert state.status == "node_refused"
    assert state.terminal.code == "provider_declined"


def test_a_typed_refusal_detail_outside_its_contract_is_refused(tmp_path):
    instance = _refusing_world(tmp_path, {"provider_declined": False})
    state, _ = run_line(instance, invoker=Invoker(refusal=("provider_declined", {"reason": 3})))
    assert state.status == "node_refused"
    assert state.terminal.code == "contract_output_refused"
    assert "test.refusal@1" in state.terminal.message


@pytest.mark.parametrize("on_unavailable", ["omit", "refuse"])
def test_an_absorbable_source_refusal_is_absorbed_only_under_omit(tmp_path, on_unavailable):
    from tests.support.operation_world import operation

    fetcher = operation_build(
        provider_id="fetcher",
        operations=(
            operation(
                "workspace.file",
                refusals={"provider_declined": True},
                signature=("workspace.file.request@1", "workspace.file.result@1"),
            ),
        ),
    )
    instance, root = sourced_world(
        tmp_path,
        acquisition={"doc": {"max_age": "P1D", "on_unavailable": on_unavailable}},
        bindings={"read": fetcher.identity.name},
        builds=(fetcher,),
    )
    assert accepted_line(instance, SOURCE_LINE).bindings["read"].build.target == fetcher.identity
    invoker = Invoker(refusal=("provider_declined", {"reason": "not today"}))
    state = run_sourced(instance, root, operator=Operator(invoker))
    assert len(invoker.contexts) == 1
    if on_unavailable == "refuse":
        assert state.status == "node_refused"
        assert (state.terminal.code, state.terminal.node_id) == ("provider_declined", "doc")
    else:
        # Absorbed: the Source is omitted, and the run moves past it.
        assert getattr(state.terminal, "node_id", None) != "doc", state.terminal


def test_an_output_outside_the_operation_contract_is_refused(tmp_path):
    instance = slotted_world(tmp_path)
    author_line(instance)
    state, _ = run_line(instance, invoker=Invoker(output=lambda context: {"value": "seven"}))
    assert state.status == "node_refused"
    assert state.terminal.code == "contract_output_refused"


# --- 8. exact producer versions -----------------------------------------------


def test_a_capture_keeps_verifying_across_procedure_and_line_succession(tmp_path):
    from cruxible_client.contracts.cas_contracts import BodyAccessContext
    from cruxible_core.service.evidence.capture_reads import verify_accepted_capture

    instance, root = sourced_world(tmp_path)
    state = run_sourced(instance, root)
    assert state.status == "succeeded", state.terminal
    (observed,) = state.source_observations
    digest = observed.capture_digest
    access = BodyAccessContext(principal_id="reader", can_read_body=True)
    before = instance.body_store().read(digest, access=access)

    def verified():  # type: ignore[no-untyped-def]
        result = verify_accepted_capture(
            instance, instance.accepted_coordinate(), digest, access=access
        )
        assert not isinstance(result, str), result
        return result

    verified()
    # A Procedure successor (a new digest) at the head: the Capture still names
    # the exact version that produced it, which history retains.
    author_procedure(instance, sourced_spec(description="A changed description."), name="v2")
    verified()
    # A Line successor over the new Procedure (re-authored, so a new intent).
    author_line(
        instance,
        name=SOURCE_LINE,
        procedure="test.sourced",
        parameters={},
        max_authority="observe",
        label="line-v2",
    )
    line = accepted_line(instance, SOURCE_LINE)
    assert line.lifecycle.predecessor_digest is not None
    verified()
    # The Line runs again against the new version; the old Capture still verifies.
    from datetime import timedelta

    again = run_sourced(instance, root, at=NOW + timedelta(minutes=1))
    assert again.status == "succeeded", again.terminal
    verified()
    assert instance.body_store().read(digest, access=access) == before


# --- 9. Line acquisition max_age ----------------------------------------------


def test_a_line_acquisition_max_age_violation_refuses_like_the_old_policy(tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace

    from cruxible_client.contracts.procedures.acquisition_rules import (
        AcquisitionDefault,
        resolve_acquisition_rules,
    )
    from cruxible_core.procedures.acquisition import (
        ACQUISITION_STALE,
        capture_selection_failure,
    )

    instance, _root = sourced_world(
        tmp_path,
        acquisition_defaults={"doc": {"max_age": "P1D"}},
        acquisition={"doc": {"max_age": "PT1S", "on_unavailable": "refuse"}},
    )
    line = accepted_line(instance, SOURCE_LINE)
    assert line.acquisition["doc"].max_age.microseconds == 1_000_000
    # The Line's override replaces the Procedure's default for that alias.
    rules = resolve_acquisition_rules(
        ("doc",),
        defaults={"doc": AcquisitionDefault.model_validate({"max_age": "P1D"})},
        overrides=line.acquisition,
    )
    assert rules["doc"].max_age.microseconds == 1_000_000
    # The same eligibility law the policy's max_age used: an older Capture is stale.
    stale = SimpleNamespace(observed_at=NOW - timedelta(seconds=10))
    fresh = SimpleNamespace(observed_at=NOW)
    assert capture_selection_failure(rules["doc"], stale, evaluation_time=NOW) == ACQUISITION_STALE
    assert capture_selection_failure(rules["doc"], fresh, evaluation_time=NOW) is None


# --- 11. a format-7 Procedure with slots: acceptance and execution -------------


def test_a_format_7_slotted_procedure_is_accepted_and_executes(tmp_path):
    from cruxible_client.contracts.procedures.artifacts import parse_procedure

    instance = slotted_world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = "procedures/test.slotted.json"
    procedure = parse_procedure(tree[path], path=path)
    assert procedure.artifact_format == "cruxible-procedure-v3"
    assert int(procedure.definition.graph_format) == 7
    assert set(procedure.definition.slots) == {"compute"}
    (node,) = procedure.definition.nodes
    assert node.slot == "compute"
    author_line(instance)
    state, invoker = run_line(instance)
    assert state.status == "succeeded", state
    assert state.result == {"value": 2}
    assert invoker.contexts[0].interface_id == "compute"


def _produced_captures(instance, run_id):  # type: ignore[no-untyped-def]
    from cruxible_core.exhaust.payloads import read_payload
    from cruxible_core.service.procedures.measurements import _journal
    from cruxible_core.storage.cas import BodyAccessContext

    journal, stream = _journal(instance)
    found = []
    for stored in journal.select_records(stream, event_kind="produced_capture"):
        payload = read_payload(
            instance.body_store(),
            stored.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
        if isinstance(payload, dict) and stored.record.run_id == run_id:
            found.append(str(payload["capture_digest"]))
    return found


def emitting_spec(*, description=None):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.procedures.authored import ProcedureSpec

    return ProcedureSpec.model_validate(
        {
            **slotted_spec(description=description).model_dump(mode="json", by_alias=True),
            "steps": [
                {"call": "result", "slot": "compute", "input": {"value": "$input.value"}},
                {
                    "emit": "test.procedure-output",
                    "input": {"value": "$input.value"},
                    "result": {"value": "$result.value"},
                },
            ],
            "budget": {"provider_calls": 1, "capture_bytes": 65536},
        }
    )


def test_a_procedure_produced_capture_verifies_at_its_exact_producer_version(tmp_path):
    """F08: the Capture names the Procedure version that made it, never the head."""

    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from cruxible_client.contracts.cas_contracts import BodyAccessContext
    from cruxible_core.service.evidence.capture_reads import verify_accepted_capture
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._light_capture_support import procedure_contract

    first, second = operation_build(build="a"), operation_build(build="d")
    instance = world(tmp_path, first)
    contract = procedure_contract()
    accept_tree(
        instance,
        {capture_contract_path(contract.identity.name): render_capture_contract(contract)},
        name="capture-contract",
    )
    author_procedure(instance, emitting_spec())
    author_line(instance)
    state, _ = run_line(instance)
    assert state.status == "succeeded", state.terminal
    (digest,) = _produced_captures(instance, state.run_id)
    access = BodyAccessContext(principal_id="reader", can_read_body=True)
    before = instance.body_store().read(digest, access=access)

    def verifies() -> None:
        result = verify_accepted_capture(
            instance, instance.accepted_coordinate(), digest, access=access
        )
        assert not isinstance(result, str), result

    verifies()
    # A Procedure successor: the head digest is no longer the producer's.
    author_procedure(instance, emitting_spec(description="A new description."), name="v2")
    verifies()
    # A Line successor over it, then a provider move of that Line.
    author_line(instance, max_authority="observe", label="line-v2")
    verifies()
    from cruxible_client.contracts.provider_installation import ProviderMoveRequest
    from cruxible_core.service.procedures.provider_builds import service_move_provider

    install(instance, second)
    moved = service_move_provider(
        instance,
        ProviderMoveRequest(provider="demo-provider", to=second.identity.name, dry_run=False),
        actor_id="owner",
        timestamp="2026-10-29T11:30:00.000000Z",
    )
    assert moved.status == "accepted", moved.detail
    verifies()
    assert instance.body_store().read(digest, access=access) == before
