"""Independent tests bind historical Claims without owning a Procedure."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelectorV1,
    CaptureEventWindowV1,
    FixedWindowV1,
    TriggerEventReferenceV1,
    bind_observation_window,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.resolution_contracts import (
    ClaimVersionReferenceV1,
    ResolutionContractV1,
    render_resolution_contract,
    resolution_contract_digest,
    resolution_contract_path,
)
from cruxible_client.contracts.resolution_rules import (
    PredictionEqualityRuleV1,
    PredictionObservationSelectorV1,
)
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world
from tests.test_indexes.test_resolution_contracts import _accept_tree


def contract_world(tmp_path: Path):
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    at = instance.accepted_coordinate()
    path = claim_path(claim_id)
    claim = parse_claim(instance.blob_at(at.git_oid, path), path=path)
    contract = ResolutionContractV1(
        identity=ArtifactIdentity(kind="ResolutionContract", name="status-test"),
        hypothesis=ClaimVersionReferenceV1(
            identity=claim.identity,
            artifact_digest=claim_artifact_digest(claim).tagged,
            statement_digest=claim_statement_digest(claim.statement).tagged,
            coordinate=AcceptedCoordinate.from_internal(at),
        ),
        observation=PredictionObservationSelectorV1(
            subject=claim.statement.subject, predicate=claim.statement.predicate
        ),
        rule=PredictionEqualityRuleV1(),
        window=FixedWindowV1(
            starts_at=datetime(2026, 8, 29, tzinfo=timezone.utc), duration_seconds=86400
        ),
    )
    return instance, owner, contract


def accept_contract(instance, owner, contract, name="resolution"):
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[resolution_contract_path(contract.identity.name)] = render_resolution_contract(contract)
    _accept_tree(instance, owner, tree, timestamp="2026-08-28T15:01:00.000000Z", proposal_name=name)


def test_contract_acceptance_projection_and_history(tmp_path: Path) -> None:
    instance, owner, contract = contract_world(tmp_path)
    accept_contract(instance, owner, contract)
    first = instance.accepted_coordinate()
    from cruxible_client.contracts.resolution_contracts import ResolutionContractsRequestV1
    from cruxible_core.service.procedures.resolution_contracts import service_resolution_contracts

    request = ResolutionContractsRequestV1(hypothesis=contract.hypothesis)
    view = service_resolution_contracts(instance, request)
    assert tuple(item.contract for item in view.contracts) == (contract,)
    from cruxible_client.contracts.errors import PlaybillFormatError

    with pytest.raises(PlaybillFormatError, match="exact Claim"):
        service_resolution_contracts(
            instance,
            request.model_copy(
                update={
                    "hypothesis": contract.hypothesis.model_copy(
                        update={"artifact_digest": "sha256:" + "f" * 64}
                    )
                }
            ),
        )
    with instance.bind_accepted_projection(first) as projection:
        row = projection.typed.envelope(contract.identity.qualified)
        assert row.artifact_digest == resolution_contract_digest(contract).tagged
        assert projection.typed.dependency_state(contract.identity.qualified).pins == ()
        stored = projection.typed.connection.execute(
            "SELECT hypothesis_identity,hypothesis_artifact_digest,rule_kind "
            "FROM resolution_contracts"
        ).fetchone()
        assert tuple(stored) == (
            contract.hypothesis.identity.qualified,
            contract.hypothesis.artifact_digest,
            "equality",
        )
    retired = contract.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=resolution_contract_digest(contract).tagged
            )
        }
    )
    accept_contract(instance, owner, retired, "retire-resolution")
    historical = service_resolution_contracts(
        instance, request.model_copy(update={"at": AcceptedCoordinate.from_internal(first)})
    )
    assert tuple(item.contract for item in historical.contracts) == (contract,)
    assert service_resolution_contracts(instance, request).contracts[0].contract == retired
    from cruxible_client.contracts.errors import PlaybillExecutionError

    future = contract.hypothesis.model_copy(
        update={"coordinate": AcceptedCoordinate.from_internal(instance.accepted_coordinate())}
    )
    with pytest.raises(PlaybillExecutionError, match="outside"):
        service_resolution_contracts(
            instance,
            ResolutionContractsRequestV1(
                hypothesis=future, at=AcceptedCoordinate.from_internal(first)
            ),
        )
    assert instance.blob_at(
        first.git_oid, resolution_contract_path(contract.identity.name)
    ) == render_resolution_contract(contract)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        row = projection.typed.connection.execute(
            "SELECT revision,lifecycle FROM resolution_contracts"
        ).fetchone()
        assert tuple(row) == (2, "retired")


def test_windows_keep_event_time_independent_of_execution_time() -> None:
    instant = datetime(2026, 9, 1, tzinfo=timezone.utc)
    fixed = FixedWindowV1(starts_at=instant, duration_seconds=86400)
    selector = CaptureEventSelectorV1(
        capture_contract_identity=ArtifactIdentity(kind="CaptureContract", name="anchor"),
        capture_contract_digest="sha256:" + "a" * 64,
    )
    policy = CaptureEventWindowV1(event=selector, duration_seconds=86400)
    event = TriggerEventReferenceV1(
        run_id="run-1", partition_id="direct:test", sequence=1, record_digest="sha256:" + "b" * 64
    )
    bound = bind_observation_window(policy, event=event, event_time=instant)
    assert bound.starts_at == instant
    assert bound.ends_at == datetime(2026, 9, 2, tzinfo=timezone.utc)
    assert bind_observation_window(fixed).ends_at == bound.ends_at
    with pytest.raises(ValueError, match="waiting"):
        bind_observation_window(policy)
    with pytest.raises(ValueError, match="cannot accept"):
        bind_observation_window(fixed, event=event, event_time=instant)


def test_contract_refuses_wrong_hypothesis_version_and_predecessor(tmp_path: Path) -> None:
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    instance, owner, contract = contract_world(tmp_path)
    wrong = contract.model_copy(
        update={
            "hypothesis": contract.hypothesis.model_copy(
                update={"artifact_digest": "sha256:" + "c" * 64}
            )
        }
    )
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree[resolution_contract_path(contract.identity.name)] = render_resolution_contract(wrong)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/wrong-hypothesis", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-08-28T15:01:00.000000Z",
    )
    assert result.candidate is None
    assert instance.accepted_coordinate() == base
    accept_contract(instance, owner, contract)
    changed = contract.model_copy(update={"outcome_class": "changed"})
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree[resolution_contract_path(contract.identity.name)] = render_resolution_contract(changed)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/missing-predecessor", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-08-28T15:02:00.000000Z",
    )
    assert result.candidate is None
    assert instance.accepted_coordinate() == base


def test_run_binds_contract_and_replays_exact_history(tmp_path: Path) -> None:
    from cruxible_client.contracts.resolution_contracts import ResolutionContractReferenceV1
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequestV2,
        service_get_playbill_procedure_run,
        service_run_playbill_procedure,
    )
    from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor, _world

    instance, owner, procedure = _world(tmp_path)
    at = instance.accepted_coordinate()
    path, raw = next(
        (p, b) for p, b in instance.tree_at(at.git_oid).items() if p.startswith("claims/")
    )
    claim = parse_claim(raw, path=path)
    hypothesis = ClaimVersionReferenceV1(
        identity=claim.identity,
        artifact_digest=claim_artifact_digest(claim).tagged,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        coordinate=AcceptedCoordinate.from_internal(at),
    )

    def make(name):
        return ResolutionContractV1(
            identity=ArtifactIdentity(kind="ResolutionContract", name=name),
            hypothesis=hypothesis,
            observation=PredictionObservationSelectorV1(
                subject=claim.statement.subject, predicate=claim.statement.predicate
            ),
            rule=PredictionEqualityRuleV1(),
            window=FixedWindowV1(starts_at=READ_TIME, duration_seconds=86400),
        )

    from cruxible_client.contracts.procedures.artifacts import (
        procedure_artifact_digest,
        procedure_path,
        render_procedure,
    )
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3

    definition = procedure.definition.model_copy(update={"name": "second-method"})
    other_method = procedure.model_copy(
        update={
            "identity": ArtifactIdentity(kind="Procedure", name="second-method"),
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v3(definition).tagged,
        }
    )
    one, two = make("one"), make("two")
    tree = instance.tree_at(at.git_oid)
    tree[procedure_path(other_method.identity.name)] = render_procedure(other_method)
    for c in (one, two):
        tree[resolution_contract_path(c.identity.name)] = render_resolution_contract(c)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-24T15:30:00.000000Z", proposal_name="contracts"
    )
    basis = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    refs = [
        ResolutionContractReferenceV1(
            identity=c.identity,
            artifact_digest=resolution_contract_digest(c).tagged,
            coordinate=basis,
        )
        for c in (one, two)
    ]
    results = []
    for ref in refs:
        request = ProcedureRunRequestV2(
            evaluation_time=READ_TIME, input={}, resolution_contract=ref
        )
        first = service_run_playbill_procedure(
            instance, name=procedure.identity.name, request=request, actor_context=_actor(instance)
        )
        assert first.status == "succeeded"
        assert first.investigation.contract == ref
        assert (
            service_run_playbill_procedure(
                instance,
                name=procedure.identity.name,
                request=request,
                actor_context=_actor(instance),
            )
            == first
        )
        assert service_get_playbill_procedure_run(instance, run_id=first.run_id) == first
        results.append(first)
    assert results[0].run_id != results[1].run_id
    assert results[0].semantic_replay_key_digest != results[1].semantic_replay_key_digest
    other_run = service_run_playbill_procedure(
        instance,
        name=other_method.identity.name,
        request=ProcedureRunRequestV2(
            evaluation_time=READ_TIME, input={}, resolution_contract=refs[0]
        ),
        actor_context=_actor(instance),
    )
    assert other_run.status == "succeeded"
    assert other_run.investigation == results[0].investigation
    assert other_run.run_id != results[0].run_id

    # A new version of the same method must retain the question, while getting
    # its own execution identity. Historical replay still uses the old method.
    definition = procedure.definition.model_copy(
        update={"budget": procedure.definition.budget.model_copy(update={"max_items": 90})}
    )
    successor = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v3(definition).tagged,
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_artifact_digest(procedure).tagged
            ),
        }
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[procedure_path(successor.identity.name)] = render_procedure(successor)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-24T16:05:00.000000Z", proposal_name="method-v2"
    )
    revised_run = service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=ProcedureRunRequestV2(
            evaluation_time=READ_TIME.replace(minute=10), input={}, resolution_contract=refs[0]
        ),
        actor_context=_actor(instance),
    )
    assert revised_run.status == "succeeded"
    assert revised_run.procedure_artifact_digest == procedure_artifact_digest(successor).tagged
    assert revised_run.procedure_artifact_digest != results[0].procedure_artifact_digest
    assert revised_run.investigation == results[0].investigation
    assert revised_run.semantic_replay_key_digest != results[0].semantic_replay_key_digest
    assert revised_run.run_id != results[0].run_id
    assert service_get_playbill_procedure_run(instance, run_id=results[0].run_id) == results[0]
    retired = one.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=resolution_contract_digest(one).tagged
            )
        }
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[resolution_contract_path(one.identity.name)] = render_resolution_contract(retired)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-24T17:00:00.000000Z", proposal_name="retire-test"
    )
    assert service_get_playbill_procedure_run(instance, run_id=results[0].run_id) == results[0]
    from cruxible_client.contracts.errors import PlaybillExecutionError

    with pytest.raises(PlaybillExecutionError, match="current live"):
        service_run_playbill_procedure(
            instance,
            name=procedure.identity.name,
            request=ProcedureRunRequestV2(
                evaluation_time=READ_TIME, input={}, resolution_contract=refs[0]
            ),
            actor_context=_actor(instance),
        )


def test_capture_trigger_uses_exact_retained_event_not_git_changes(tmp_path: Path) -> None:
    from datetime import timedelta

    from cruxible_client.contracts.errors import PlaybillExecutionError
    from cruxible_core.exhaust.writer import ProcedureExhaustWriter
    from cruxible_core.service.procedures.procedure_runs import (
        PROCEDURE_RUN_FENCING_TOKEN,
        _activate_writer,
        _journal_for_write,
        _stream,
    )
    from cruxible_core.service.procedures.resolution_contracts import (
        bind_window,
        capture_event_time,
    )
    from tests.test_procedures.test_procedure_run_surface import (
        READ_TIME,
        _actor,
        _slotless_procedure,
    )

    instance, owner, contract = contract_world(tmp_path)
    accept_contract(instance, owner, contract)
    procedure = _slotless_procedure("capture-anchor").procedure
    selector = CaptureEventSelectorV1(
        capture_contract_identity=ArtifactIdentity(kind="CaptureContract", name="anchor"),
        capture_contract_digest="sha256:" + "a" * 64,
    )
    journal, _ = _journal_for_write(instance)
    stream = _stream(instance)
    _activate_writer(journal, stream, "run:anchor")
    writer = ProcedureExhaustWriter(
        journal=journal, bodies=instance.body_store(), fencing_token=PROCEDURE_RUN_FENCING_TOKEN
    )
    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest

    stored = writer.append(
        stream=stream,
        partition_id="run:anchor",
        event_kind="produced_capture",
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        procedure_artifact_digest=procedure_artifact_digest(procedure).tagged,
        definition_digest=procedure.definition_digest,
        actor_context=_actor(instance),
        recorded_at=READ_TIME,
        run_id="RUN-anchor",
        payload={
            "tag": "playbill-procedure-produced-capture-v1",
            "capture_contract_digest": selector.capture_contract_digest,
            "observed_at": "2000-01-01T00:00:00Z",
        },
    )
    ref = TriggerEventReferenceV1(
        run_id="RUN-anchor",
        partition_id="run:anchor",
        sequence=stored.record.sequence,
        record_digest=stored.record_digest,
    )
    assert capture_event_time(instance, selector, ref, now=READ_TIME) == READ_TIME
    policy = CaptureEventWindowV1(event=selector, duration_seconds=86400)
    assert bind_window(instance, policy, ref, now=READ_TIME).ends_at == READ_TIME + timedelta(
        days=1
    )
    assert bind_window(
        instance, policy, ref, now=READ_TIME + timedelta(days=3)
    ).ends_at == READ_TIME + timedelta(days=1)
    with pytest.raises(PlaybillExecutionError, match="selector"):
        capture_event_time(
            instance,
            CaptureEventSelectorV1(
                capture_contract_identity=ArtifactIdentity(kind="CaptureContract", name="anchor"),
                capture_contract_digest="sha256:" + "b" * 64,
            ),
            ref,
            now=READ_TIME,
        )
    with pytest.raises(PlaybillExecutionError):
        capture_event_time(
            instance,
            selector,
            ref.model_copy(update={"record_digest": "sha256:" + "c" * 64}),
            now=READ_TIME,
        )

    with pytest.raises(PlaybillExecutionError, match="has not occurred"):
        capture_event_time(instance, selector, ref, now=READ_TIME - timedelta(seconds=1))
    from cruxible_client.contracts.procedures.windows import LineTriggerBindingV1
    from cruxible_client.contracts.resolution_contracts import ResolutionContractReferenceV1
    from cruxible_core.service.procedures.resolution_contracts import bind_investigation

    reference = ResolutionContractReferenceV1(
        identity=contract.identity,
        artifact_digest=resolution_contract_digest(contract).tagged,
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    now = contract.window.starts_at + timedelta(days=1)
    # The event starts the Line, while the test retains its independent calendar window.
    bound = bind_investigation(
        instance,
        reference,
        event=ref,
        now=now,
        trigger_binding=LineTriggerBindingV1(kind="capture_landing", event=ref),
    )
    assert bound.window == bind_observation_window(contract.window)
    with pytest.raises(ValueError, match="cannot accept"):
        bind_investigation(instance, reference, event=ref, now=now)


@pytest.mark.parametrize("restart", [False, True], ids=["same-instance", "fresh-process"])
def test_window_line_runs_once_and_replays_its_original_investigation(
    tmp_path: Path, restart: bool
) -> None:
    import json
    import subprocess
    import sys
    from datetime import timedelta
    from types import SimpleNamespace

    from cruxible_client.contracts.acquisition_policies import (
        acquisition_policy_path,
        render_acquisition_policy,
    )
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.artifacts import (
        AcceptedProcedureV1,
        ProcedureArtifactV2,
        procedure_artifact_digest,
        procedure_path,
        render_procedure,
    )
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
    from cruxible_client.contracts.procedures.line_specs import (
        LineSpecV3,
        WindowCloseTriggerPolicyV2,
        line_identity_digest,
        line_spec_path,
        render_line_spec,
    )
    from cruxible_client.contracts.procedures.models import ProcedureDefinitionV4
    from cruxible_client.contracts.resolution_contracts import ResolutionContractReferenceV1
    from cruxible_core.service.procedures import procedure_runs
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequestV1,
        service_get_playbill_procedure_run,
        service_run_playbill_line,
    )
    from tests.test_procedures.test_procedure_run_surface import _actor, _slotless_procedure
    from tests.test_server.test_playbill_line_run_refusals import (
        _acquisition_policy,
        _line_mandate,
        _served_line,
    )

    instance, owner, contract = contract_world(tmp_path)
    base = _slotless_procedure("window-method").procedure
    definition = ProcedureDefinitionV4.model_validate(
        {**base.definition.model_dump(mode="python"), "graph_format": 4}
    )
    procedure = ProcedureArtifactV2.model_validate(
        {
            **base.model_dump(mode="python"),
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v4(definition).tagged,
        }
    )
    accepted = AcceptedProcedureV1(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    policy = _acquisition_policy("window-policy")
    line = LineSpecV3.model_validate(
        {
            **_served_line("window-test", accepted=accepted, policy=policy).model_dump(
                mode="python"
            ),
            "artifact_format": "playbill-line-v3",
            "provider_implementation_closures": (),
            "trigger_policy": WindowCloseTriggerPolicyV2(window=contract.window),
        }
    )
    mandate = _line_mandate(accepted)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(
        {
            accepted.path: render_procedure(procedure),
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
            line_spec_path(line.identity.name): render_line_spec(line),
            resolution_contract_path(contract.identity.name): render_resolution_contract(contract),
        }
    )
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:01:00.000000Z", proposal_name="window"
    )
    ref = ResolutionContractReferenceV1(
        identity=contract.identity,
        artifact_digest=resolution_contract_digest(contract).tagged,
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    line_id = line_identity_digest(line.identity)

    def run(instant, reference=ref):
        return service_run_playbill_line(
            instance,
            path_identity_digest=line_id,
            request=LineRunRequestV1(line_identity_digest=line_id, resolution_contract=reference),
            actor_context=_actor(instance),
            caller_rung=3,
            daemon_clock=SimpleNamespace(now=lambda: instant),
        )

    end = contract.window.starts_at + timedelta(seconds=contract.window.duration_seconds)
    early = run(end - timedelta(seconds=1))
    assert early.status == "admission_refused"
    assert early.terminal.code == "occurrence_not_due"
    first = run(end + timedelta(hours=1))
    assert first.status == "succeeded", first
    assert first.investigation.contract == ref
    assert first.trigger_binding.window == first.investigation.window
    assert first.investigation.window.ends_at == end

    def journal_snapshot():
        journal, _ = procedure_runs._journal(instance)
        stream = procedure_runs._stream(instance)
        return tuple(
            (
                partition,
                tuple(
                    (r.record.event_kind, r.record_digest)
                    for r in journal.all_records(stream, partition)
                ),
            )
            for partition in journal.partition_ids(stream)
        )

    before = journal_snapshot()
    assert sum(kind == "admission_bound" for _, records in before for kind, _ in records) == 1
    if restart:
        # A new interpreter cannot inherit any daemon object or in-memory memo.
        # Only the invocation, trust root and retained managed directory cross
        # this boundary; the original run/binding is reconstructed from storage.
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from cruxible_client.contracts.types import PlaybillTrustRoot
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.procedures import procedure_runs

args = json.load(sys.stdin)
instance = PlaybillInstance.open(
    Path(args["root"]), trust_root=PlaybillTrustRoot.model_validate(args["trust_root"])
)
request = procedure_runs.LineRunRequestV1.model_validate(args["request"])

def unexpected_execution(*args, **kwargs):
    raise AssertionError("restarting a retained occurrence must not execute it again")

procedure_runs.service_execute_direct_procedure = unexpected_execution
result = procedure_runs.service_run_playbill_line(
    instance,
    path_identity_digest=request.line,
    request=request,
    actor_context=GovernedActorContext.model_validate(args["actor"]),
    caller_rung=3,
    daemon_clock=SimpleNamespace(now=lambda: datetime.fromisoformat(args["now"])),
)
assert procedure_runs.service_get_playbill_procedure_run(instance, run_id=result.run_id) == result
print(result.model_dump_json())
""",
            ],
            input=json.dumps(
                {
                    "root": str(instance.root),
                    "trust_root": instance.trust_root.model_dump(mode="json"),
                    "request": LineRunRequestV1(
                        line_identity_digest=line_id, resolution_contract=ref
                    ).model_dump(mode="json"),
                    "actor": _actor(instance).model_dump(mode="json"),
                    "now": (end + timedelta(days=2)).isoformat(),
                }
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == first.model_dump(mode="json")
    else:
        assert run(end + timedelta(days=2)) == first
    assert journal_snapshot() == before
    assert service_get_playbill_procedure_run(instance, run_id=first.run_id) == first
    # A retry cannot quietly drop or replace the investigation.
    from cruxible_client.contracts.errors import PlaybillExecutionError

    with pytest.raises(PlaybillExecutionError, match="original investigation"):
        run(end + timedelta(days=2), None)
    assert journal_snapshot() == before
