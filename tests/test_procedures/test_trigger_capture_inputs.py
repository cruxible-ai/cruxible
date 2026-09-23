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
    LineSpecV5,
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


def world(tmp_path, *, window=False, line_budget=None, with_owner=False, **kwargs):
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
    if line_budget is not None:
        line = line.model_copy(
            update={"budgets": {**line.budgets, "max_capture_bytes": line_budget}}
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
    return (instance, root, line, owner) if with_owner else (instance, root, line)


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
        refused = run_line(instance, line, event, at=now)
        assert refused.status == "admission_refused"
        assert refused.terminal.code == "trigger_capture_invalid"
        assert not refused.terminal.retryable
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
        with pytest.raises(PlaybillExecutionError, match="bound read budget") as error:
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
        assert error.value.refusal_code == "trigger_capture_over_budget"
        assert not error.value.retryable
        assert error.value.details["limiting_budget"] == "line"
        return
    refused = run_line(instance, line, event, at=now)
    assert refused.run_id is None and refused.status == "admission_refused", refused
    assert refused.terminal.code == (
        "trigger_capture_stale" if failure == "stale" else "trigger_capture_unavailable"
    )
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
        max_authority="observe",
        trigger_policy=line.trigger_policy,
        trigger_input=SOURCE_ALIAS,
    )
    path, raw, _ = _render_line_member(payload, tree=tree)
    authored = parse_line_spec(raw, path=path)
    assert isinstance(authored, LineSpecV5) and authored.trigger_input == SOURCE_ALIAS
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


@pytest.mark.parametrize("failure", ["boundary", "head_moved", "unexpected"])
def test_trigger_input_reservations_release_on_failed_admission(tmp_path, monkeypatch, failure):
    import cruxible_core.service.procedures.procedure_runs as runs
    from cruxible_core.procedures.execution import ProcedureBoundaryRefused
    from cruxible_core.storage.material_reservations import ProcedureMaterialReservationStore

    instance, root, line = world(tmp_path)
    produced, _ = _run(instance, root)
    event = event_from(produced)
    store = ProcedureMaterialReservationStore(instance.body_store().reservation_root)
    original_activate = runs._activate_writer

    def fail(*args, **kwargs):
        assert store.active(), "failure must happen after material is reserved"
        if failure == "boundary":
            raise ProcedureBoundaryRefused("test", "boundary refused")
        if failure == "head_moved":
            raise runs.ProcedureRunNotCurrent("accepted coordinate advanced")
        raise RuntimeError("unexpected failure")

    if failure == "head_moved":

        def activate(*args, **kwargs):
            original_activate(*args, **kwargs)
            fail()

        monkeypatch.setattr(runs, "_activate_writer", activate)
    else:
        monkeypatch.setattr(runs, "service_execute_direct_procedure", fail)
    if failure == "boundary":
        assert run_line(instance, line, event).status == "admission_refused"
    else:
        with pytest.raises((RuntimeError, runs.ProcedureRunNotCurrent)):
            run_line(instance, line, event)
    assert not store.active()
    journal, _ = _journal(instance)
    assert len(journal.select_records(_stream(instance), event_kind="admission_bound")) == 1


@pytest.mark.parametrize("failure", ["stale", "missing_envelope", "missing_body"])
def test_unusable_occurrence_closes_without_starving_later_capture(tmp_path, failure):
    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_client.contracts.line_dispatch import (
        LineEvaluateRequestV1,
        LineTriggerCheckRequestV1,
    )
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore
    from cruxible_core.service.procedures.line_dispatch import service_evaluate_line
    from cruxible_core.service.procedures.line_triggers import service_check_line_trigger

    instance, root, line = world(tmp_path)
    actor = _actor(instance)
    first, _ = _run(instance, root)
    digest = first.source_observations[0].capture_digest
    bodies = instance.body_store()
    if failure == "missing_body":
        envelope = parse_capture_envelope(
            bodies.read(digest, access=BodyAccessContext(principal_id="test", can_read_body=True))
        )
        digest = envelope.commitment.digest
    original_bytes = bodies.read(
        digest, access=BodyAccessContext(principal_id="test", can_read_body=True)
    )
    if failure != "stale":
        bodies.erase(digest)
    later = NOW + (timedelta(hours=2) if failure == "stale" else timedelta(seconds=10))
    (root / RELATIVE_PATH).write_text('{"severity":"low"}')
    _run(instance, root, evaluation_time=later)
    now = later + timedelta(seconds=2)
    evaluated = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequestV1(since=NOW, until=now),
        actor=actor,
        now=now,
    )
    assert len(evaluated.occurrences) == 2
    first_id = next(
        o.occurrence_id for o in evaluated.occurrences if o.binding.event == event_from(first)
    )
    rejected = service_dispatch_line(
        instance, line.identity.name, LineDispatchRequestV1(), actor=actor, now=now, caller_rung=2
    ).items[0]
    assert rejected.occurrence_id == first_id
    assert rejected.status == "rejected"
    assert rejected.refusal.code == (
        "trigger_capture_stale" if failure == "stale" else "trigger_capture_unavailable"
    )
    assert not rejected.refusal.retryable
    assert rejected.detail == rejected.refusal.message
    assert rejected.refusal.repair.hand_edit.required_change
    LineDispatchStore(instance).path.unlink()
    # Re-evaluation cannot silently revive closed work, even after rebuilding SQLite.
    repeated = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequestV1(since=NOW, until=now),
        actor=actor,
        now=now,
    )
    closed = next(o for o in repeated.occurrences if o.occurrence_id == first_id)
    assert closed.dispatch_status == "rejected"
    assert not closed.pending
    admitted = service_dispatch_line(
        instance, line.identity.name, LineDispatchRequestV1(), actor=actor, now=now, caller_rung=2
    ).items[0]
    assert admitted.status == "admitted" and admitted.occurrence_id != first_id
    check = service_check_line_trigger(
        instance, line.identity.name, LineTriggerCheckRequestV1(), now=now
    )
    assert (
        next(o for o in check.occurrences if o.occurrence_id == first_id).dispatch_status
        == "rejected"
    )
    if failure != "stale":
        bodies.store(original_bytes)
        retry = service_dispatch_line(
            instance,
            line.identity.name,
            LineDispatchRequestV1(occurrence_id=first_id, retry=True),
            actor=actor,
            now=now,
            caller_rung=2,
        )
        assert retry.items[0].status == "admitted"


def test_over_budget_occurrence_closes_then_requires_successor_for_retry(tmp_path):
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.line_dispatch import LineEvaluateRequestV1
    from cruxible_client.contracts.procedures.line_specs import line_spec_digest
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore
    from cruxible_core.service.procedures.line_dispatch import service_evaluate_line

    instance, root, line, owner = world(
        tmp_path,
        contents=b'{"severity":"' + b"h" * 3000 + b'"}',
        line_budget=1024,
        with_owner=True,
    )
    actor = _actor(instance)
    first, _ = _run(instance, root)
    first_event = event_from(first)
    (root / RELATIVE_PATH).write_text('{"severity":"low"}')
    _run(instance, root, evaluation_time=NOW + timedelta(seconds=10))
    now = NOW + timedelta(seconds=12)
    evaluated = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequestV1(since=NOW, until=now),
        actor=actor,
        now=now,
    )
    first_id = next(
        o.occurrence_id for o in evaluated.occurrences if o.binding.event == first_event
    )
    rejected = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(),
        actor=actor,
        now=now,
        caller_rung=2,
    ).items[0]
    assert rejected.occurrence_id == first_id and rejected.status == "rejected"
    assert rejected.refusal.code == "trigger_capture_over_budget"
    assert not rejected.refusal.retryable
    assert rejected.refusal.details["effective_max_bytes"] == 1024
    assert rejected.refusal.details["limiting_budget"] == "line"
    LineDispatchStore(instance).path.unlink()
    small = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(),
        actor=actor,
        now=now,
        caller_rung=2,
    ).items[0]
    assert small.status == "admitted" and small.occurrence_id != first_id
    retry_request = LineDispatchRequestV1(occurrence_id=first_id, retry=True)
    assert (
        service_dispatch_line(
            instance,
            line.identity.name,
            retry_request,
            actor=actor,
            now=now,
            caller_rung=2,
        )
        .items[0]
        .status
        == "rejected"
    )
    successor = line.model_copy(
        update={
            "budgets": {**line.budgets, "max_capture_bytes": 4096},
            "lifecycle": ArtifactLifecycle(predecessor_digest=line_spec_digest(line).tagged),
        }
    )
    _accept_more(
        instance,
        owner,
        {line_spec_path(line.identity.name): render_line_spec(successor)},
        name="larger-line-budget",
    )
    retried = service_dispatch_line(
        instance,
        line.identity.name,
        retry_request,
        actor=actor,
        now=now,
        caller_rung=2,
    ).items[0]
    assert retried.status == "admitted", retried
    assert admission(instance, retried.run_id).admission.trigger_binding.event == first_event


def test_line_v5_states_its_authority_as_a_verb_and_defaults_to_the_procedure(tmp_path):
    import json

    from cruxible_client.contracts.authoring.models import LineAuthoringPayloadV1
    from cruxible_client.contracts.procedures.line_specs import (
        AUTHORITY_RUNG,
        ManualTriggerPolicyV1,
        line_requested_rung,
        parse_line_spec,
    )
    from cruxible_core.authoring.lowering import _render_line_member
    from cruxible_core.service.procedures.procedure_runs import _accepted_procedure

    instance, _, line = world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    accepted = _accepted_procedure(
        instance, coordinate=instance.accepted_coordinate(), name=line.procedure.target.name
    )
    capability = accepted.procedure.definition.terminal_capability
    path, raw, _ = _render_line_member(
        LineAuthoringPayloadV1(
            name="manual-consumer",
            procedure_name=line.procedure.target.name,
            acquisition_policy_name=line.acquisition_policy.target.name,
            trigger_policy=ManualTriggerPolicyV1(),
        ),
        tree=tree,
    )
    manual = parse_line_spec(raw, path=path)
    assert isinstance(manual, LineSpecV5) and manual.trigger_input is None
    wire = json.loads(raw)
    assert "requested_terminal_rung" not in wire and wire["max_authority"] == manual.max_authority
    assert AUTHORITY_RUNG[manual.max_authority] == capability == line_requested_rung(manual)

    # The law's capability check reads the same internal value the verb maps to.
    above = next(verb for verb, rung in AUTHORITY_RUNG.items() if rung > capability)
    assert line_requested_rung(manual.model_copy(update={"max_authority": above})) > capability


def test_a_revision_30_line_v4_survives_the_revision_31_succession(tmp_path):
    from cruxible_client.contracts.authoring.models import LineAuthoringPayloadV1
    from cruxible_client.contracts.errors import ProjectionFormatError
    from cruxible_client.contracts.procedures.line_specs import (
        line_requested_rung,
        line_spec_digest,
        parse_line_spec,
    )
    from cruxible_core.authoring.lowering import _render_line_member
    from cruxible_core.compiler.compiler import (
        AUTHORITY_VERBS_COMPILER,
        TRIGGER_CAPTURE_COMPILER,
        artifact_kinds_for_compiler,
        projection_registry_for_compiler,
    )
    from cruxible_core.compiler.projection_artifacts import parse_projection_tree

    instance, _, line = world(tmp_path)
    path = line_spec_path(line.identity.name)
    content = render_line_spec(line)
    # The v4 wire is unchanged: numeric rung, required trigger input, same digest.
    reparsed = parse_line_spec(content, path=path)
    assert isinstance(reparsed, LineSpecV4) and reparsed == line
    assert line_spec_digest(reparsed) == line_spec_digest(line)
    assert line_requested_rung(reparsed) == line.requested_terminal_rung
    for compiler in (TRIGGER_CAPTURE_COMPILER, AUTHORITY_VERBS_COMPILER):
        parse_projection_tree(
            {path: content},
            registry=projection_registry_for_compiler(compiler),
            artifact_kinds=artifact_kinds_for_compiler(compiler),
        )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    v5_path, v5, _ = _render_line_member(
        LineAuthoringPayloadV1(
            name="verb-line",
            procedure_name=line.procedure.target.name,
            acquisition_policy_name=line.acquisition_policy.target.name,
            trigger_policy=line.trigger_policy,
        ),
        tree=tree,
    )
    with pytest.raises(ProjectionFormatError, match="Line v5 requires compiler revision 31"):
        parse_projection_tree(
            {v5_path: v5},
            registry=projection_registry_for_compiler(TRIGGER_CAPTURE_COMPILER),
            artifact_kinds=artifact_kinds_for_compiler(TRIGGER_CAPTURE_COMPILER),
        )
