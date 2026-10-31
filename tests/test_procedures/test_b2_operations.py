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
    REFUSAL,
    VALUE,
    Invoker,
    Operator,
    actor,
    author,
    author_line,
    author_procedure,
    build_files,
    contract_files,
    operation,
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
    assert invoker.contexts[0].operation == "compute"


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
    assert invoker.contexts[0].build_digest == provider_digest(first).tagged


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


def test_an_explicit_operation_binding_resolves_past_the_candidate_limit(tmp_path):
    """F11: an exact build#operation reference is a SQL seek, not a Python
    filter over the (candidate-limited) fitting set: it resolves even when
    more operations than the candidate limit fit the same signature."""

    build = operation_build(operations=tuple(operation(name=f"op{i:02d}") for i in range(22)))
    instance = world(tmp_path, build)
    author_procedure(instance, slotted_spec())
    author_line(instance, bindings={"compute": f"{build.identity.name}#op21"})
    assert accepted_line(instance).bindings["compute"].operation == "op21"


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


def test_a_direct_run_reaches_its_terminal_under_an_ephemeral_line(tmp_path):
    """F01/C4: a direct run has no accepted Line, so no Mandate covers it -- but
    it still runs its own terminal under an ephemeral Line, capped at propose.

    Before the fix, any terminal node (even rung-0 ``emit_capture``) made the
    Procedure ``unsupported_node`` for a direct run, before a run ever existed.
    """

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
    assert result.status == "succeeded", result
    assert result.terminal is None
    (digest,) = _produced_captures(instance, result.run_id)
    assert digest.startswith("sha256:")
    # A direct run still has no real Line: invoked-child delegation and the
    # Mandate authority lookup both read it as absent, exactly as before.
    from types import SimpleNamespace

    standalone = SimpleNamespace(
        line_identity=None,
        line_spec_digest=None,
        parent_binding=None,
        procedure_identity=result.procedure_identity,
        procedure_artifact_digest=result.procedure_artifact_digest,
    )
    assert authority_line(standalone) == (None, None)  # type: ignore[arg-type]


def test_a_direct_run_settle_terminal_is_capped_at_propose():
    """F01/C4: the ephemeral Line's effective rung permits propose, never settle."""

    from cruxible_core.procedures.egress import compute_ephemeral_run_effective_rung

    rung = compute_ephemeral_run_effective_rung(
        procedure_terminal_capability=3,
        procedure_definition_digest="sha256:" + "1" * 64,
        ephemeral_spec_digest="sha256:" + "2" * 64,
    )
    assert rung.effective_rung == 2
    assert rung.permits("emit_capture")
    assert rung.permits("post_inbox")
    assert rung.permits("propose_change_set")
    assert not rung.permits("settle_change_set")
    assert rung.limiting_term in {"mandate_grant", "line_requested_rung"}


def _attempt_started_payload(instance, run_id):  # type: ignore[no-untyped-def]
    from cruxible_core.exhaust.payloads import read_payload
    from cruxible_core.service.procedures.measurements import _journal
    from cruxible_core.storage.cas import BodyAccessContext

    journal, stream = _journal(instance)
    for stored in journal.select_records(stream, event_kind="attempt_started"):
        if stored.record.run_id == run_id:
            return read_payload(
                instance.body_store(),
                stored.record.payload_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
    raise AssertionError(f"no attempt_started event for run {run_id!r}")


def plain_spec(*, name="test.plain"):  # type: ignore[no-untyped-def]
    """One `project` step: no Call or Source node, so no provider is involved."""

    from cruxible_client.contracts.procedures.authored import ProcedureSpec

    return ProcedureSpec.model_validate(
        {
            "procedure": name,
            "input": {"value": "integer"},
            "output": {"value": "integer"},
            "steps": [{"project": "result", "fields": {"value": "$input.value"}}],
            "returns": "result",
            "budget": {"provider_calls": 0},
        }
    )


def test_a_plain_direct_run_also_records_its_ephemeral_spec_digest(tmp_path):
    """F12: the ordinary (no provider/Source node) direct-run branch carries its
    ephemeral spec digest too, and it is verified from retained journal data
    -- not merely spied on through a monkeypatched digest function."""

    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.procedures.line_specs import (
        EphemeralLineSpec,
        ephemeral_line_spec_digest,
    )
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequest,
        service_run_playbill_procedure,
    )

    instance = world(tmp_path)
    author_procedure(instance, plain_spec())
    result = service_run_playbill_procedure(
        instance,
        name="test.plain",
        request=ProcedureRunRequest(input={"value": 9}, evaluation_time=NOW),
        actor_context=actor(),
        provider_runtime_operator=Operator(Invoker()),  # type: ignore[arg-type]
    )
    assert result.status == "succeeded", result
    assert result.result == {"value": 9}
    payload = _attempt_started_payload(instance, result.run_id)
    assert isinstance(payload, dict)
    recorded_digest = payload["ephemeral_spec_digest"]
    assert isinstance(recorded_digest, str) and recorded_digest.startswith("sha256:")
    expected = ephemeral_line_spec_digest(
        EphemeralLineSpec(
            procedure=ArtifactPin(
                role="procedure",
                target=result.procedure_identity,
                artifact_digest=result.procedure_artifact_digest,
            ),
            parameters={"value": 9},
            bindings={},
            acquisition={},
        )
    )
    assert recorded_digest == expected


# --- F01 fix round 2: a direct run's propose/settle terminal really delivers -


def _self_sourced_subject_item(rationale):  # type: ignore[no-untyped-def]
    """A ``propose``/``settle`` candidate template: a self-sourced Subject.

    No Capture, Source or provider is cited: the item's own body is its
    evidence (``SelfSourceBody``), so a direct run with no provider/Source
    node at all can still reach a real propose/settle delivery (F01).
    """
    import base64

    return {
        "tag": "playbill-procedure-subject-proposal-item-v1",
        "subject_kind": "test.widget",
        "key": {},
        "rationale": rationale,
        "create_new": True,
        "source": {
            "tag": "playbill-self-source-body-v1",
            "content_base64": base64.b64encode(rationale.encode("utf-8")).decode("ascii"),
        },
    }


def _author_widget_kind(instance):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.authoring.inputs import SubjectKindInput

    author(
        instance,
        SubjectKindInput(kind="subject_kind", name="test.widget", minted=True),
        name="subject-kind",
    )


def _plain_terminal_spec(*, name, verb):  # type: ignore[no-untyped-def]
    """No Call or Source node: a pure ``project`` feeding a propose/settle terminal."""

    from cruxible_client.contracts.procedures.authored import ProcedureSpec

    return ProcedureSpec.model_validate(
        {
            "procedure": name,
            "input": {},
            "output": {"value": "integer"},
            "steps": [
                {"project": "shaped", "fields": {"value": 1}},
                {verb: (_self_sourced_subject_item(f"Plain direct {verb}."),)},
            ],
            "returns": "shaped",
            "budget": {"provider_calls": 0},
        }
    )


def _call_terminal_spec(*, name, verb):  # type: ignore[no-untyped-def]
    """A slotted Call precedes the propose/settle terminal (a real provider run)."""

    from cruxible_client.contracts.procedures.authored import ProcedureSpec

    return ProcedureSpec.model_validate(
        {
            "procedure": name,
            "input": {"value": "integer"},
            "output": {"value": "integer"},
            "slots": {"compute": {"in": "test.value@1", "out": "test.value@1", "effect": "pure"}},
            "steps": [
                {"call": "result", "slot": "compute", "input": {"value": "$input.value"}},
                {verb: (_self_sourced_subject_item(f"Called direct {verb}."),)},
            ],
            "returns": "result",
            "budget": {"provider_calls": 1},
        }
    )


def _direct_propose_or_settle_delivers(tmp_path, *, verb, with_call):  # type: ignore[no-untyped-def]
    """Shared body: a direct run's terminal always delivers a real proposal.

    A ``propose`` terminal delivers under ``effective_authority == "propose"``.
    A ``settle`` terminal, which no accepted Line and so no Mandate could ever
    cover (ruling C4), falls back to the very same delivery -- never settling
    (``outcome == "proposed"``, ``accepted_git_oid`` is ``None``).
    """
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequest,
        service_run_playbill_procedure,
    )

    build = operation_build() if with_call else None
    instance = world(tmp_path, *(() if build is None else (build,)))
    _author_widget_kind(instance)
    name = f"test.{verb}-{'called' if with_call else 'plain'}-direct"
    spec = (
        _call_terminal_spec(name=name, verb=verb)
        if with_call
        else _plain_terminal_spec(name=name, verb=verb)
    )
    author_procedure(instance, spec)
    base = instance.accepted_coordinate()
    kwargs = {}
    if with_call:
        kwargs["provider_runtime_operator"] = Operator(Invoker())
    result = service_run_playbill_procedure(
        instance,
        name=name,
        request=ProcedureRunRequest(input={"value": 4} if with_call else {}, evaluation_time=NOW),
        actor_context=actor(),
        **kwargs,
    )
    assert result.status == "succeeded", result.terminal
    assert len(result.terminal_egress) == 1
    egress = result.terminal_egress[0]
    assert egress.kind == f"{verb}_change_set"
    assert egress.verdict == "delivered"
    assert egress.proposal_id is not None and egress.candidate_digest is not None
    assert egress.effective_authority == "propose"
    if verb == "settle":
        assert egress.capped_by is not None
        assert egress.settle_outcome == "proposed"
        assert egress.accepted_git_oid is None
    # Producing the proposal activated nothing: a direct run never settles.
    assert instance.accepted_coordinate() == base
    return instance, result


@pytest.mark.parametrize("with_call", [False, True])
def test_a_direct_run_propose_terminal_really_delivers_a_proposal(tmp_path, with_call):
    """F01: a self-sourced Subject proposal, with and without a preceding Call."""

    _direct_propose_or_settle_delivers(tmp_path, verb="propose", with_call=with_call)


@pytest.mark.parametrize("with_call", [False, True])
def test_a_direct_run_settle_terminal_falls_back_to_a_real_proposal(tmp_path, with_call):
    """F01/C4: a settle terminal in a direct run proposes -- it never settles."""

    _direct_propose_or_settle_delivers(tmp_path, verb="settle", with_call=with_call)


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
    assert [item.build_digest for item in invoker.contexts] == [provider_digest(second).tagged]


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
                    "call": "doc",
                    "captures": {"capture": "test.orders-v1"},
                    "slot": "read",
                    "input": {
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
    # The operation's own code, served under the one operation-refusal code.
    assert (state.terminal.code, state.terminal.detail_code) == (
        "operation_refused",
        "provider_declined",
    )
    assert state.terminal.details == {"reason": "x"}


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
        acquisition={"doc.capture": {"max_age": "P1D", "on_unavailable": on_unavailable}},
        bindings={"read": fetcher.identity.name},
        builds=(fetcher,),
    )
    assert accepted_line(instance, SOURCE_LINE).bindings["read"].build.target == fetcher.identity
    invoker = Invoker(refusal=("provider_declined", {"reason": "not today"}))
    state = run_sourced(instance, root, operator=Operator(invoker))
    assert len(invoker.contexts) == 1
    if on_unavailable == "refuse":
        assert state.status == "node_refused"
        assert (state.terminal.code, state.terminal.detail_code, state.terminal.node_id) == (
            "operation_refused",
            "provider_declined",
            "doc",
        )
    else:
        # Absorbed: the Source is omitted, and the run moves past it.
        assert getattr(state.terminal, "node_id", None) != "doc", state.terminal


def test_an_output_outside_the_operation_contract_is_refused(tmp_path):
    instance = slotted_world(tmp_path)
    author_line(instance)
    state, _ = run_line(instance, invoker=Invoker(output=lambda context: {"value": "seven"}))
    assert state.status == "node_refused"
    assert state.terminal.code == "contract_output_refused"


def _severity_claim_type():  # type: ignore[no-untyped-def]
    from cruxible_core.claims.claim_type_inputs import lower_claim_type_input
    from tests.core_support._claim_type_support import claim_type_input_example

    record = claim_type_input_example().model_copy(
        update={
            "predicate": "test.severity",
            "literal_schema": {"type": "string", "enum": ["low", "high"]},
        }
    )
    return lower_claim_type_input(record, tree={})


def _severity_world(tmp_path):  # type: ignore[no-untyped-def]
    """A world whose ``compute`` slot's operation outputs ``enum_of<test.severity>``."""

    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_client.contracts.providers import (
        ProviderDistributionRef,
        ProviderImplementationManifest,
        ProviderLocalDistributionPin,
        ProviderLocalEnvBackendPin,
        ProviderRuntimeManifest,
    )
    from cruxible_client.contracts.shapes import (
        Contract,
        contract_digest,
        contract_path,
        render_contract,
    )
    from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
    from tests.core_support._identity_support import accept_tree

    severity_value = Contract.model_validate(
        {
            "name": "test.severity_value",
            "version": 1,
            "fields": {"severity": {"type": "enum_of:test.severity"}},
        }
    )
    served = (
        operation(
            "compute",
            signature=("test.value@1", "test.severity_value@1"),
        ),
    )
    manifest = ProviderRuntimeManifest(
        provider_id="demo-provider",
        distribution=ProviderDistributionRef(name="demo-provider", version="1.0.0"),
        supported_protocol_majors=(2,),
        implementations=(
            ProviderImplementationManifest(
                entrypoint="demo.runtime:Provider",
                backends=("local_env",),
                deterministic=True,
            ),
        ),
    )
    document = PackageRegistrationDocumentV1(schema_version=2, manifest=manifest, operations=served)

    def digests(ref):  # type: ignore[no-untyped-def]
        for contract in (VALUE, REFUSAL, severity_value):
            if (ref.name, ref.version) == (contract.name, contract.version):
                return contract_digest(contract).tagged
        return None

    build = document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name="demo-provider",
            version="1.0.0",
            filename="demo_provider-1.0.0-py3-none-any.whl",
            sha256="sha256:" + ("a" * 64),
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain="demo-provider",
        operations=document.pinned_operations(digests),
    )
    # The severity Contract and ClaimType must be accepted before the build
    # that pins them: the install law resolves every operation Contract pin
    # against what is already accepted. ``world()`` always seeds a default
    # build under the same provider identity, so this instance is built from
    # scratch instead of through it.
    from tests.core_support._support import initialize_local

    instance, _owner = initialize_local(tmp_path)
    severity_type = _severity_claim_type()
    accept_tree(
        instance,
        {
            **contract_files(),
            contract_path(severity_value.name, severity_value.version): render_contract(
                severity_value
            ),
            claim_type_path(severity_type.predicate): render_claim_type(severity_type),
        },
        name="severity",
    )
    accept_tree(instance, build_files(build), name="build")
    from cruxible_client.contracts.procedures.authored import ProcedureSpec

    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.slotted",
                "input": {"value": "integer"},
                "output": {"severity": "string"},
                "slots": {
                    "compute": {
                        "in": "test.value@1",
                        "out": "test.severity_value@1",
                        "effect": "pure",
                    }
                },
                "steps": [
                    {"call": "result", "slot": "compute", "input": {"value": "$input.value"}}
                ],
                "returns": "result",
                "budget": {"provider_calls": 1},
            }
        ),
    )
    author_line(instance)
    return instance


def test_an_accepted_enum_of_claim_type_member_is_not_refused(tmp_path):
    """F07: the executor resolves enum_of<ClaimType> members at the admitted
    coordinate; a declared member (``high``) must not refuse not_in_enum."""

    instance = _severity_world(tmp_path)
    state, _ = run_line(instance, invoker=Invoker(output=lambda context: {"severity": "high"}))
    assert state.status == "succeeded", state.terminal
    assert state.result == {"severity": "high"}


def test_an_unaccepted_enum_of_claim_type_member_is_refused(tmp_path):
    """The flip side of F07: a value outside the declared ClaimType members
    still refuses, so the resolver is not merely a pass-through."""

    instance = _severity_world(tmp_path)
    state, _ = run_line(
        instance, invoker=Invoker(output=lambda context: {"severity": "catastrophic"})
    )
    assert state.status == "node_refused"
    assert state.terminal.code == "contract_output_refused"


def test_an_undeclared_provider_refusal_is_an_undeclared_refusal(tmp_path):
    """F10: an operation that declares no refusals cannot have a provider
    refusal pass through as if it were a declared, typed domain refusal."""

    instance = _refusing_world(tmp_path, {})
    state, _ = run_line(instance, invoker=Invoker(refusal=("provider_declined", {"reason": "x"})))
    assert state.status != "succeeded"
    assert state.terminal is not None
    assert state.terminal.code == "undeclared_refusal"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # Only the executor observes a budget or an egress breach.
        ("budget_output_size", "provider_protocol_violation"),
        ("undeclared_egress", "provider_protocol_violation"),
        # The child harness itself observes an undelivered secret.
        ("unresolved_secret_ref", "unresolved_secret_ref"),
    ],
)
def test_an_envelope_runtime_code_is_believed_only_from_the_harness(tmp_path, code, expected):
    instance = _refusing_world(tmp_path, {"provider_declined": False})
    state, _ = run_line(instance, invoker=Invoker(refusal=(code, {})))
    assert state.status != "succeeded"
    assert state.terminal.code == expected


def _fetch_world(
    tmp_path,
    *,
    line_name="test.fetched-line",
    acquisition=None,
    request_url=None,
):  # type: ignore[no-untyped-def]
    """A world whose ``fetch`` slot is an ``external_read`` Source operation
    with a declared out Contract stricter than the generic Capture wire
    shape (F02): it accepts only ``replayability: exact``.

    ``request_url``, when given, adds a ``url`` field (declared on the
    request Contract, and named in the authored Source's own request) so a
    Line's ``acquisition.origins`` (F05) has something real to check.
    """

    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from cruxible_client.contracts.procedures.authored import ProcedureSpec
    from cruxible_client.contracts.providers import (
        ProviderDistributionRef,
        ProviderImplementationManifest,
        ProviderLocalDistributionPin,
        ProviderLocalEnvBackendPin,
        ProviderRuntimeManifest,
    )
    from cruxible_client.contracts.shapes import (
        Contract,
        contract_digest,
        contract_path,
        render_contract,
    )
    from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._light_capture_support import record_contract
    from tests.core_support._support import initialize_local

    request_fields = {"query": {"type": "string"}}
    if request_url is not None:
        request_fields["url"] = {"type": "string"}
    fetch_request = Contract.model_validate(
        {"name": "demo.fetch.request", "version": 1, "fields": request_fields}
    )
    fetch_result = Contract.model_validate(
        {
            "name": "demo.fetch.result",
            "version": 1,
            "fields": {
                "source_identity": {"type": "string"},
                "coordinate_type": {"type": "string"},
                "coordinate": {"type": "string"},
                "selector_type": {"type": "string"},
                "selector": {"type": "string"},
                "replayability": {"type": "enum[exact]"},
                "content_base64": {"type": "string"},
                "byte_length": {"type": "int{0..}"},
                "bytes_digest": {"type": "string"},
                "observed_at": {"type": "datetime"},
            },
        }
    )
    served = (
        operation(
            "fetch",
            effect="external_read",
            signature=("demo.fetch.request@1", "demo.fetch.result@1"),
        ),
    )
    manifest = ProviderRuntimeManifest(
        provider_id="fetcher",
        distribution=ProviderDistributionRef(name="fetcher", version="1.0.0"),
        supported_protocol_majors=(2,),
        implementations=(
            ProviderImplementationManifest(
                entrypoint="demo.runtime:Provider",
                backends=("local_env",),
                deterministic=True,
            ),
        ),
    )
    document = PackageRegistrationDocumentV1(schema_version=2, manifest=manifest, operations=served)

    def digests(ref):  # type: ignore[no-untyped-def]
        for item in (VALUE, REFUSAL, fetch_request, fetch_result):
            if (ref.name, ref.version) == (item.name, item.version):
                return contract_digest(item).tagged
        return None

    build = document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name="fetcher",
            version="1.0.0",
            filename="fetcher-1.0.0-py3-none-any.whl",
            sha256="sha256:" + ("a" * 64),
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain="fetcher",
        operations=document.pinned_operations(digests),
    )
    # The request/result Contracts and CaptureContract must be accepted
    # before the build that pins them; see test_an_accepted_enum_of_...
    # above for why this cannot go through world()'s default-build path.
    instance, _owner = initialize_local(tmp_path)
    # "exact", matching the only replayability the operation's own out
    # Contract permits (F02) -- so a wire-valid output can also satisfy the
    # CaptureContract's own replay promise, not only the operation's.
    capture_contract = record_contract(name="test.events-v1", replay="exact")
    accept_tree(
        instance,
        {
            **contract_files(),
            contract_path(fetch_request.name, fetch_request.version): render_contract(
                fetch_request
            ),
            contract_path(fetch_result.name, fetch_result.version): render_contract(fetch_result),
            capture_contract_path(capture_contract.identity.name): render_capture_contract(
                capture_contract
            ),
        },
        name="fetch-contracts",
    )
    accept_tree(instance, build_files(build), name="fetch-build")
    request = {"query": "orders"}
    if request_url is not None:
        request["url"] = request_url
    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.fetched",
                "input": {},
                "output": {"ok": "bool"},
                "slots": {
                    "fetch": {
                        "in": "demo.fetch.request@1",
                        "out": "demo.fetch.result@1",
                        "effect": "external_read",
                    }
                },
                "steps": [
                    {
                        "call": "doc",
                        "captures": {"capture": capture_contract.identity.name},
                        "slot": "fetch",
                        "input": request,
                    },
                    {"project": "out", "fields": {"ok": True}},
                ],
                "returns": "out",
                "budget": {"provider_calls": 1, "capture_bytes": 65536},
            }
        ),
    )
    author_line(
        instance,
        name=line_name,
        procedure="test.fetched",
        parameters={},
        acquisition=acquisition,
    )
    return instance


def test_an_external_read_sources_output_is_checked_against_its_operation_contract(
    tmp_path,
):
    """F02: an external-read Source's output is validated against its
    operation's own out Contract, not only the generic Capture-wire shape."""

    instance = _fetch_world(tmp_path)

    def bad_output(context):  # type: ignore[no-untyped-def]
        return {
            "source_identity": "demo.events",
            "coordinate_type": "database-snapshot-v1",
            "coordinate": "workspace",
            "selector_type": "relation-primary-key-v1",
            "selector": "orders",
            # Wire-shape valid (Literal["exact", "attested_only"]); the
            # operation's own out Contract permits only "exact" (F02).
            "replayability": "attested_only",
            "content_base64": "e30=",
            "byte_length": 2,
            "bytes_digest": "sha256:" + "0" * 64,
            "observed_at": "2026-10-29T12:00:00Z",
        }

    state, _ = run_line(instance, invoker=Invoker(output=bad_output), name="test.fetched-line")
    assert state.status == "node_refused", state.terminal
    assert state.terminal.code == "contract_output_refused"
    assert "replayability" in state.terminal.message


def _fetch_output(*, byte_length=2, content_base64="e30=", coordinate_type="database-snapshot-v1"):  # type: ignore[no-untyped-def]
    import base64
    import hashlib

    content = base64.b64decode(content_base64, validate=True)
    assert len(content) == byte_length

    def output(context):  # type: ignore[no-untyped-def]
        return {
            "source_identity": "demo.events",
            "coordinate_type": coordinate_type,
            "coordinate": "workspace",
            "selector_type": "relation-primary-key-v1",
            "selector": "orders",
            "replayability": "exact",
            "content_base64": content_base64,
            "byte_length": byte_length,
            "bytes_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "observed_at": "2026-10-29T12:00:00Z",
        }

    return output


def test_a_real_oversized_read_routes_through_on_oversized_not_a_protocol_violation(tmp_path):
    """F05: a real external Source over its CaptureContract's byte budget
    (4,096 here, Codex's reproduction: 5,012 read bytes) reaches the typed
    ``on_oversized`` decision -- refuse by default, omit when the Line's
    acquisition says so -- never a generic ``provider_protocol_violation``.
    """
    import base64

    oversized = base64.b64encode(b"x" * 5_012).decode("ascii")

    (tmp_path / "refuse").mkdir()
    (tmp_path / "omit").mkdir()
    refusing = _fetch_world(tmp_path / "refuse", line_name="test.fetched-refuse")
    state, _ = run_line(
        refusing,
        invoker=Invoker(output=_fetch_output(byte_length=5_012, content_base64=oversized)),
        name="test.fetched-refuse",
    )
    assert state.status == "node_refused", state.terminal
    assert state.terminal.code == "cruxible.acquisition.oversized"

    omitting = _fetch_world(
        tmp_path / "omit",
        line_name="test.fetched-omit",
        acquisition={"doc.capture": {"on_oversized": "omit"}},
    )
    state, _ = run_line(
        omitting,
        invoker=Invoker(output=_fetch_output(byte_length=5_012, content_base64=oversized)),
        name="test.fetched-omit",
    )
    # Absorbed: the oversized Source is omitted, and the run moves past it.
    assert getattr(state.terminal, "node_id", None) != "doc", state.terminal


def test_an_oversized_and_schema_invalid_result_reports_the_schema_violation(tmp_path):
    """F05b fix round 3: classifying a result as oversized must never
    bypass an independent Capture-contract violation. Codex's reproduction:
    an external Source naming an unadmitted coordinate schema
    (``http-request-v1`` against the fixture's ``database-snapshot``
    CaptureContract) fails on schema admission at a small size; at 5,012
    bytes with ``on_oversized: omit`` the old code skipped straight to the
    typed oversized omission instead of reporting that same violation."""

    import base64

    oversized = base64.b64encode(b"x" * 5_012).decode("ascii")

    (tmp_path / "small-bad-schema").mkdir()
    (tmp_path / "oversized-bad-schema").mkdir()

    # Control: the same bad coordinate schema at two bytes already refuses
    # on schema admission, never reaching size at all.
    small = _fetch_world(tmp_path / "small-bad-schema", line_name="test.fetched-small-bad-schema")
    state, _ = run_line(
        small,
        invoker=Invoker(output=_fetch_output(coordinate_type="http-request-v1")),
        name="test.fetched-small-bad-schema",
    )
    assert state.status != "succeeded", state.terminal
    assert state.terminal.code == "provider_protocol_violation"

    # Oversized AND the same bad schema, with on_oversized: omit: the schema
    # violation must still be reported, not silently omitted as oversized.
    big = _fetch_world(
        tmp_path / "oversized-bad-schema",
        line_name="test.fetched-oversized-bad-schema",
        acquisition={"doc.capture": {"on_oversized": "omit"}},
    )
    state, _ = run_line(
        big,
        invoker=Invoker(
            output=_fetch_output(
                byte_length=5_012, content_base64=oversized, coordinate_type="http-request-v1"
            )
        ),
        name="test.fetched-oversized-bad-schema",
    )
    assert state.status != "succeeded", state.terminal
    assert state.terminal.code == "provider_protocol_violation"


def test_a_line_acquisition_origin_outside_the_permitted_set_refuses(tmp_path):
    """F05/§4.1,§4.4: ``acquisition.origins`` is a hard eligibility gate core
    enforces on the Source's ``url`` field, before any provider call."""

    (tmp_path / "bad-origin").mkdir()
    instance = _fetch_world(
        tmp_path / "bad-origin",
        line_name="test.fetched-bad-origin",
        acquisition={"doc.capture": {"origins": ["https://permitted.example"]}},
        request_url="https://not-permitted.example/feed",
    )
    state, _ = run_line(instance, invoker=Invoker(), name="test.fetched-bad-origin")
    assert state.status == "node_refused", state.terminal
    assert state.terminal.code == "cruxible.acquisition.origin_ineligible"


def test_a_line_acquisition_origin_inside_the_permitted_set_is_not_refused(tmp_path):
    """The flip side of the gate above: a request naming a permitted origin
    never reaches ``cruxible.acquisition.origin_ineligible`` -- the request
    the Source actually sends (not merely the authored template) is checked.
    """

    (tmp_path / "good-origin").mkdir()
    instance = _fetch_world(
        tmp_path / "good-origin",
        line_name="test.fetched-good-origin",
        acquisition={"doc.capture": {"origins": ["https://permitted.example"]}},
        request_url="https://permitted.example/feed",
    )
    state, _ = run_line(
        instance, invoker=Invoker(output=_fetch_output()), name="test.fetched-good-origin"
    )
    assert getattr(state.terminal, "code", None) != "cruxible.acquisition.origin_ineligible"


def test_a_scheme_relative_url_under_restricted_origins_refuses_before_any_call(tmp_path):
    """F05a fix round 3: a scheme-relative value (``//evil.example/feed``)
    cannot be proven to belong to the permitted origin set, so a restricted
    Line must refuse it exactly like a disallowed origin -- never dispatch
    the provider call and then rely on a later failure to undo it."""

    (tmp_path / "scheme-relative-origin").mkdir()
    instance = _fetch_world(
        tmp_path / "scheme-relative-origin",
        line_name="test.fetched-scheme-relative",
        acquisition={"doc.capture": {"origins": ["https://permitted.example"]}},
        request_url="//evil.example/feed",
    )
    invoker = Invoker()
    state, _ = run_line(instance, invoker=invoker, name="test.fetched-scheme-relative")
    assert state.status == "node_refused", state.terminal
    assert state.terminal.code == "cruxible.acquisition.origin_ineligible"
    assert len(invoker.contexts) == 0


# --- 8. exact producer versions -----------------------------------------------


def _line_input(instance, name, **updates):  # type: ignore[no-untyped-def]
    """Reauthor an exact Line with its existing parameters and provider builds."""
    from cruxible_client.contracts.authoring.inputs import LineInput

    line = accepted_line(instance, name)
    return LineInput.model_validate(
        {
            "kind": "line",
            "name": name,
            "procedure_name": line.procedure.target.name,
            "bindings": {
                slot: f"{binding.build.target.name}#{binding.operation}"
                for slot, binding in line.bindings.items()
            },
            "parameters": line.parameters,
            "max_authority": line.max_authority,
            "acquisition": line.acquisition,
            "budgets": line.budgets,
            "occurrence_epoch": line.occurrence_epoch,
            "trigger_input": line.trigger_input,
            **updates,
        }
    )


def _succeed_procedure_and_line(instance, spec, line):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.authoring.inputs import ChangeSetInput, ProcedureInput

    author(
        instance,
        ChangeSetInput(
            kind="change_set",
            members=(
                ProcedureInput(kind="procedure", spec=spec),
                _line_input(instance, line),
            ),
        ),
        name="producer-v2",
    )


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
    _succeed_procedure_and_line(
        instance, sourced_spec(description="A changed description."), SOURCE_LINE
    )
    verified()
    # A separate Line budget change also leaves the old producer verifiable.
    line = accepted_line(instance, SOURCE_LINE)
    author(
        instance,
        _line_input(
            instance,
            SOURCE_LINE,
            budgets={
                **line.budgets,
                "max_capture_bytes": line.budgets["max_capture_bytes"] - 1,
            },
        ),
        name="line-v2",
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
        acquisition_defaults={"doc.capture": {"max_age": "P1D"}},
        acquisition={"doc.capture": {"max_age": "PT1S", "on_unavailable": "refuse"}},
    )
    line = accepted_line(instance, SOURCE_LINE)
    assert line.acquisition["doc.capture"].max_age.microseconds == 1_000_000
    # The Line's override replaces the Procedure's default for that alias.
    rules = resolve_acquisition_rules(
        ("doc.capture",),
        defaults={"doc.capture": AcquisitionDefault.model_validate({"max_age": "P1D"})},
        overrides=line.acquisition,
    )
    assert rules["doc.capture"].max_age.microseconds == 1_000_000
    # The same eligibility law the policy's max_age used: an older Capture is stale.
    stale = SimpleNamespace(observed_at=NOW - timedelta(seconds=10))
    fresh = SimpleNamespace(observed_at=NOW)
    assert (
        capture_selection_failure(rules["doc.capture"], stale, evaluation_time=NOW)
        == ACQUISITION_STALE
    )
    assert capture_selection_failure(rules["doc.capture"], fresh, evaluation_time=NOW) is None


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
    assert invoker.contexts[0].operation == "compute"


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
    _succeed_procedure_and_line(instance, emitting_spec(description="A new description."), LINE)
    verifies()
    # A Line successor over it, then a provider move of that Line.
    author(instance, _line_input(instance, LINE, parameters={"value": 2}), name="line-v2")
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
