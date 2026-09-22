"""Real retained Source Capture -> pending Line -> exact admitted Source input."""

from datetime import timedelta

import pytest

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.captures import capture_contract_digest
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.line_dispatch import (
    LineDispatchRequestV1,
    LineListenRequestV1,
)
from cruxible_client.contracts.procedures.line_specs import (
    CaptureLandingTriggerPolicyV2,
    LineSpecV4,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.windows import CaptureEventSelectorV1
from cruxible_core.exhaust.records import parse_journal_payload
from cruxible_core.procedures.execution import parse_admission_payload
from cruxible_core.service.procedures.line_dispatch import (
    service_dispatch_line,
    service_listen_line,
    service_match_listening_lines,
)
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequestV1,
    _journal,
    _stream,
    service_run_playbill_line,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures.test_procedure_source_runs import (
    NOW,
    RELATIVE_PATH,
    SOURCE_ALIAS,
    _accept_more,
    _actor,
    _line_mandate,
    _run,
    _served_line,
    _TestClock,
    _world,
    capture_contract,
)


def world(tmp_path, *, window=False, **kwargs):
    instance, owner, procedure, root, policy = _world(tmp_path, **kwargs)
    contract = kwargs.get("contract", capture_contract())
    selector = CaptureEventSelectorV1(
        capture_contract_identity=contract.identity,
        capture_contract_digest=capture_contract_digest(contract).tagged,
    )
    original = _served_line(procedure, policy)
    trigger = CaptureLandingTriggerPolicyV2(event=selector)
    if window:
        from cruxible_client.contracts.procedures.line_specs import WindowCloseTriggerPolicyV2
        from cruxible_client.contracts.procedures.windows import CaptureEventWindowV1

        trigger = WindowCloseTriggerPolicyV2(
            window=CaptureEventWindowV1(event=selector, duration_seconds=60)
        )
    line = LineSpecV4.model_validate(
        {
            **original.model_dump(mode="python"),
            "artifact_format": "playbill-line-v4",
            "trigger_policy": trigger,
            "trigger_input": SOURCE_ALIAS,
            "pins": tuple(
                sorted(
                    (
                        *original.pins,
                        ArtifactPin(
                            role="trigger-capture-contract",
                            target=selector.capture_contract_identity,
                            artifact_digest=selector.capture_contract_digest,
                        ),
                    ),
                    key=lambda p: (p.role, p.target.qualified, p.artifact_digest),
                )
            ),
        }
    )
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_path,
        render_procedure_mandate,
    )

    mandate = _line_mandate(procedure)
    _accept_more(
        instance,
        owner,
        {
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="trigger-source-line",
    )
    return instance, root, line


def event_from(state):
    assert state.status == "succeeded", state
    return next(o.capture_event for o in state.outcomes if o.event_kind == "produced_capture")


def run_line(instance, line, event, at=NOW + timedelta(seconds=2)):
    return service_run_playbill_line(
        instance,
        path_identity_digest=line.identity.name,
        request=LineRunRequestV1(line=line.identity.name, trigger_event=event),
        actor_context=_actor(instance),
        caller_rung=2,
        daemon_clock=_TestClock(at),
        # Deliberately no Provider runtime or workspace reader: this input is retained.
    )


def admission(instance, run_id):
    journal, _ = _journal(instance)
    records = journal.select_records(_stream(instance), run_id=run_id, event_kind="admission_bound")
    assert len(records) == 1
    raw = instance.body_store().read(
        records[0].record.payload_digest,
        access=BodyAccessContext(principal_id="test", can_read_body=True),
    )
    return parse_admission_payload(parse_journal_payload(raw))


@pytest.mark.parametrize("privacy", ["direct_allowed", "pseudonymous_required"])
def test_trigger_capture_is_consumed_without_provider_or_workspace_read(
    tmp_path, monkeypatch, privacy
):
    from cruxible_core.procedures.execution import ProcedureExecutor
    from cruxible_core.procedures.terminal_dependencies import (
        admitted_capture_token,
        produced_capture_token,
    )

    seeded = {}
    original_seed = ProcedureExecutor._seed_state

    def observe_seed(self, prepared):
        state = original_seed(self, prepared)
        if prepared.admission.landed_capture_inputs:
            seeded[prepared.admission.run_id] = state
        return state

    monkeypatch.setattr(ProcedureExecutor, "_seed_state", observe_seed)
    contract = capture_contract()
    contract = contract.model_copy(
        update={
            "retention_erasure_policy": contract.retention_erasure_policy.model_copy(
                update={"selector_privacy": privacy}
            )
        }
    )
    instance, root, line = world(tmp_path, contract=contract)
    produced, _ = _run(instance, root)
    event = event_from(produced)
    (root / RELATIVE_PATH).write_text('{"severity":"low"}')
    later, _ = _run(instance, root, evaluation_time=NOW + timedelta(seconds=1))
    event_from(later)
    consumed = run_line(instance, line, event)
    assert consumed.status == "succeeded", consumed.model_dump(mode="json")
    assert consumed.result == {"severity": "high"}
    bound = admission(instance, consumed.run_id)
    assert (
        bound.admission.landed_capture_inputs[0].capture_digest
        == produced.source_observations[0].capture_digest
    )
    assert bound.admission.trigger_binding.event == event
    assert bound.acquisition_plan.external_occurrences == ()
    assert len(bound.admission_material_manifest.members) == 1
    assert bound.admission.selection_decision.decisions[0].disposition == "selected"
    capture_digest = produced.source_observations[0].capture_digest
    state = seeded[consumed.run_id]
    assert admitted_capture_token(capture_digest) in state.provenance["result"].whole
    assert produced_capture_token(capture_digest) not in state.provenance["result"].whole
    assert state.facts[capture_digest].acquisition_input_name == SOURCE_ALIAS
    from cruxible_core.procedures.terminal_dependencies import (
        build_terminal_item_manifest,
        derive_terminal_item_facts,
    )

    tokens = state.provenance["result"].whole
    manifest = build_terminal_item_manifest(
        tokens, run_id=consumed.run_id, terminal_node_id="return", item_key="result"
    )
    facts = derive_terminal_item_facts(
        tokens,
        manifest=manifest,
        child_index=0,
        facts=state.facts,
        outcomes=tuple(state.outcomes.values()),
    )
    assert facts.selector_privacy == privacy
    assert facts.source_coverage[0].input_name == SOURCE_ALIAS
    assert facts.source_coverage[0].disposition == "consumed"
    assert facts.source_coverage[0].capture_digests == (capture_digest,)


def test_pending_dispatch_and_restart_keep_the_same_capture_binding(tmp_path):
    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.procedures.procedure_runs import service_get_playbill_procedure_run
    from cruxible_core.storage.material_reservations import ProcedureMaterialReservationStore

    instance, root, line = world(tmp_path)
    actor = _actor(instance)
    service_listen_line(
        instance,
        line.identity.name,
        LineListenRequestV1(action="start"),
        actor=actor,
        now=NOW - timedelta(seconds=1),
        daemon_id="test",
    )
    producer, _ = _run(instance, root)
    event = event_from(producer)
    now = NOW + timedelta(seconds=2)
    service_match_listening_lines(instance, actor=actor, now=now, daemon_id="test")
    (root / RELATIVE_PATH).unlink()
    dispatched = service_dispatch_line(
        instance, line.identity.name, LineDispatchRequestV1(), actor=actor, now=now, caller_rung=2
    )
    assert len(dispatched.items) == 1 and dispatched.items[0].status == "admitted", dispatched
    run_id = dispatched.items[0].run_id
    result = service_get_playbill_procedure_run(instance, run_id=run_id)
    assert result.status == "succeeded" and result.result == {"severity": "high"}
    original = admission(instance, run_id)
    assert not ProcedureMaterialReservationStore(instance.body_store().reservation_root).active()
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    # Even loss of the original Capture after completion cannot cause a retry to acquire a new one.
    reopened.body_store().erase(original.admission.landed_capture_inputs[0].capture_digest)
    replay = run_line(reopened, line, event, at=now + timedelta(days=1))
    assert replay.run_id == run_id and replay.result == result.result
    assert admission(reopened, run_id) == original


@pytest.mark.parametrize(
    "failure", ["missing_envelope", "missing_body", "stale", "wrong_event", "budget"]
)
def test_trigger_input_refuses_before_admission_without_refetch(tmp_path, failure):
    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_core.service.procedures.procedure_runs import procedure_line_partition

    instance, root, line = world(tmp_path)
    produced, _ = _run(instance, root)
    event = event_from(produced)
    digest = produced.source_observations[0].capture_digest
    bodies = instance.body_store()
    now = NOW + timedelta(seconds=2)
    if failure == "missing_envelope":
        bodies.erase(digest)
    elif failure == "missing_body":
        envelope = parse_capture_envelope(
            bodies.read(digest, access=BodyAccessContext(principal_id="test", can_read_body=True))
        )
        bodies.erase(envelope.commitment.digest)
    elif failure == "stale":
        now += timedelta(hours=2)
    elif failure == "wrong_event":
        # A forged event coordinate cannot redirect a valid Capture binding.
        from cruxible_client.contracts.procedures.windows import TriggerEventReferenceV1

        event = TriggerEventReferenceV1(
            **{**event.model_dump(), "record_digest": "sha256:" + "a" * 64}
        )
        with pytest.raises(PlaybillExecutionError, match="retained record"):
            run_line(instance, line, event, at=now)
        return
    else:
        # Exercise the admission budget independently of acquisition-time provider caps.
        from cruxible_client.contracts.procedures.windows import LineTriggerBindingV1
        from cruxible_core.service.procedures.procedure_runs import (
            _accepted_procedure,
        )
        from cruxible_core.service.procedures.trigger_inputs import bind_trigger_capture
        from tests.test_procedures.test_procedure_source_runs import _policy

        accepted = _accepted_procedure(
            instance, coordinate=instance.accepted_coordinate(), name=line.procedure.target.name
        )
        contract = capture_contract()
        with pytest.raises(PlaybillExecutionError, match="over budget"):
            bind_trigger_capture(
                instance,
                line=line,
                procedure=accepted,
                binding=LineTriggerBindingV1(kind="capture_landing", event=event),
                contracts={capture_contract_digest(contract).tagged: contract},
                policy=_policy(),
                evaluation_time=now,
                max_bytes=1,
            )
        return
    refused = run_line(instance, line, event, at=now)
    assert refused.run_id is None and refused.status == "admission_refused", refused
    journal, _ = _journal(instance)
    assert not journal.select_records(
        _stream(instance),
        partition_id=procedure_line_partition(line.identity),
        event_kind="admission_bound",
    )


def test_late_event_window_consumes_its_anchor_capture(tmp_path):
    instance, root, line = world(tmp_path, window=True)
    producer, _ = _run(instance, root)
    event = event_from(producer)
    result = run_line(instance, line, event, at=NOW + timedelta(minutes=5))
    assert result.status == "succeeded", result
    bound = admission(instance, result.run_id).admission
    assert bound.trigger_binding.window.starts_at == NOW
    assert bound.trigger_binding.window.ends_at == NOW + timedelta(seconds=60)
    assert (
        bound.landed_capture_inputs[0].capture_digest
        == producer.source_observations[0].capture_digest
    )


def test_line_input_authoring_law_and_frozen_compiler_boundary(tmp_path):
    from pydantic import ValidationError

    from cruxible_client.contracts.authoring.models import LineAuthoringPayloadV1
    from cruxible_client.contracts.errors import ProjectionFormatError
    from cruxible_client.contracts.procedures.line_specs import (
        ManualTriggerPolicyV1,
        evaluate_line_spec_law,
        parse_line_spec,
    )
    from cruxible_core.authoring.lowering import _render_line_member
    from cruxible_core.compiler.compiler import (
        SOURCE_CHECKED_COMPILER,
        artifact_kinds_for_compiler,
        projection_registry_for_compiler,
    )
    from cruxible_core.compiler.projection_artifacts import parse_projection_tree
    from cruxible_core.service.procedures.procedure_runs import _accepted_procedure

    instance, _, line = world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    # Authoring uses the same optional binding and emits the successor only when requested.
    payload = LineAuthoringPayloadV1(
        name="second-consumer",
        procedure_name=line.procedure.target.name,
        acquisition_policy_name=line.acquisition_policy.target.name,
        requested_terminal_rung=1,
        trigger_policy=line.trigger_policy,
        trigger_input=SOURCE_ALIAS,
    )
    path, raw, _ = _render_line_member(payload, tree=tree)
    authored = parse_line_spec(raw, path=path)
    assert isinstance(authored, LineSpecV4) and authored.trigger_input == SOURCE_ALIAS
    with pytest.raises(ValidationError, match="Capture event trigger"):
        LineSpecV4.model_validate(
            {**line.model_dump(mode="python"), "trigger_policy": ManualTriggerPolicyV1()}
        )
    accepted = _accepted_procedure(
        instance, coordinate=instance.accepted_coordinate(), name=line.procedure.target.name
    )
    for bad in (
        line.model_copy(update={"trigger_input": "result"}),
        line.model_copy(
            update={
                "trigger_policy": CaptureLandingTriggerPolicyV2(
                    event=line.trigger_policy.event.model_copy(
                        update={"capture_contract_digest": "sha256:" + "b" * 64}
                    )
                )
            }
        ),
    ):
        verdict = evaluate_line_spec_law(
            bad,
            path=line_spec_path(bad.identity.name),
            procedure=accepted,
            interface_digests={},
            predecessor=None,
        )
        assert verdict.verdict == "refused"
        assert verdict.diagnostics[0].code == "playbill.line.trigger_input_mismatch"
    with pytest.raises(ProjectionFormatError, match="Line v4 requires"):
        parse_projection_tree(
            {line_spec_path(line.identity.name): render_line_spec(line)},
            registry=projection_registry_for_compiler(SOURCE_CHECKED_COMPILER),
            artifact_kinds=artifact_kinds_for_compiler(SOURCE_CHECKED_COMPILER),
        )
