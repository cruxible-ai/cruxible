"""Accepted Line trigger checks share exact binding semantics with admission."""

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.acquisition_policies import (
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.line_dispatch import LineTriggerCheckRequestV1
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
    CaptureLandingTriggerPolicyV2,
    LineSpecV3,
    WindowCloseTriggerPolicyV2,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import ProcedureDefinitionV4
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelectorV1,
    CaptureEventWindowV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.exhaust import ProcedureExhaustWriter
from cruxible_core.service.procedures.line_triggers import service_check_line_trigger
from cruxible_core.service.procedures.procedure_runs import (
    PROCEDURE_RUN_FENCING_TOKEN,
    _activate_writer,
    _journal,
    _stream,
)
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_independent_resolution_contracts import contract_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor, _slotless_procedure
from tests.test_server.test_playbill_line_run_refusals import (
    _acquisition_policy,
    _line_mandate,
    _served_line,
)

SELECTOR = CaptureEventSelectorV1(
    capture_contract_identity=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity,
    capture_contract_digest=capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged,
)


def line_world(tmp_path, trigger, *, with_owner=False):
    instance, owner, contract = contract_world(tmp_path)
    base = _slotless_procedure("trigger-method").procedure
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
    policy = _acquisition_policy("trigger-policy")
    base_line = _served_line("trigger-test", accepted=accepted, policy=policy)
    pins = tuple(
        sorted(
            (
                *base_line.pins,
                ArtifactPin(
                    role="trigger-capture-contract",
                    target=SELECTOR.capture_contract_identity,
                    artifact_digest=SELECTOR.capture_contract_digest,
                ),
            ),
            key=lambda p: (p.role, p.target.qualified, p.artifact_digest),
        )
    )
    if trigger.kind not in {"capture_landing", "window_close"} or (
        trigger.kind == "window_close" and trigger.window.kind == "fixed"
    ):
        pins = base_line.pins
    if trigger.kind == "cadence":
        pins = tuple(
            sorted(
                (
                    *pins,
                    ArtifactPin(
                        role="trigger-cadence-policy",
                        target=ArtifactIdentity(kind="Policy", name="cadence"),
                        artifact_digest=trigger.cadence_policy_digest,
                    ),
                ),
                key=lambda p: (p.role, p.target.qualified, p.artifact_digest),
            )
        )
    line = LineSpecV3.model_validate(
        {
            **base_line.model_dump(mode="python"),
            "pins": pins,
            "artifact_format": "playbill-line-v3",
            "provider_implementation_closures": (),
            "trigger_policy": trigger,
        }
    )
    mandate = _line_mandate(accepted)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name)] = (
        render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
    )
    tree.update(
        {
            accepted.path: render_procedure(procedure),
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
            line_spec_path(line.identity.name): render_line_spec(line),
        }
    )
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:01:00.000000Z", proposal_name="trigger"
    )
    return (instance, line, accepted, owner) if with_owner else (instance, line, accepted)


def capture(
    instance,
    procedure,
    *,
    at=READ_TIME,
    partition="run:anchor",
    digest=SELECTOR.capture_contract_digest,
    observed_at="2000-01-01T00:00:00Z",
):
    journal, _ = _journal(instance)
    stream = _stream(instance)
    _activate_writer(journal, stream, partition)
    return ProcedureExhaustWriter(
        journal=journal, bodies=instance.body_store(), fencing_token=PROCEDURE_RUN_FENCING_TOKEN
    ).append(
        stream=stream,
        partition_id=partition,
        event_kind="produced_capture",
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        procedure_artifact_digest=procedure.artifact_digest,
        definition_digest=procedure.procedure.definition_digest,
        actor_context=_actor(instance),
        recorded_at=at,
        run_id="RUN-" + partition,
        payload={
            "tag": "playbill-procedure-produced-capture-v1",
            "capture_contract_digest": digest,
            "observed_at": observed_at,
        },
    )


def test_indexed_capture_check_is_read_only_scoped_and_paginated(tmp_path: Path, monkeypatch):
    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
    assert (
        service_check_line_trigger(
            instance, line.identity.name, LineTriggerCheckRequestV1(), now=READ_TIME
        ).status
        == "not_met"
    )
    capture(instance, procedure, digest="sha256:" + "b" * 64)
    first = capture(instance, procedure)
    second = capture(instance, procedure, at=READ_TIME + timedelta(seconds=1))
    journal, _ = _journal(instance)
    before = journal.read_head(_stream(instance), first.record.partition_id)
    until = READ_TIME + timedelta(seconds=2)
    request = LineTriggerCheckRequestV1(until=until, limit=1)
    result = service_check_line_trigger(instance, line.identity.name, request, now=until)
    assert result.status == "incomplete" and result.cursor
    assert result.occurrences[0].binding.event.record_digest == second.record_digest
    page = service_check_line_trigger(
        instance,
        line.identity.name,
        request.model_copy(update={"cursor": result.cursor}),
        now=until,
    )
    assert page.status == "met" and page.cursor is None
    assert page.occurrences[0].binding.event.record_digest == first.record_digest
    assert not page.occurrences[0].pending and page.occurrences[0].admitted_run_id is None
    assert journal.read_head(_stream(instance), first.record.partition_id) == before
    assert not (instance.root / instance.descriptor.storage.exhaust / "line-dispatch").exists()
    # Warm checks may not fall back to enumerating all partitions or parsing their logs.
    monkeypatch.setattr(type(journal), "all_records", lambda *a: pytest.fail("full journal scan"))
    monkeypatch.setattr(type(journal), "partition_ids", lambda *a: pytest.fail("partition walk"))
    assert (
        service_check_line_trigger(
            instance, line.identity.name, LineTriggerCheckRequestV1(until=until), now=until
        ).status
        == "met"
    )


def test_window_eligibility_uses_fixed_event_boundary_and_missing_evidence_is_incomplete(tmp_path):
    policy = WindowCloseTriggerPolicyV2(
        window=CaptureEventWindowV1(event=SELECTOR, duration_seconds=60)
    )
    instance, line, procedure = line_world(tmp_path, policy)
    stored = capture(instance, procedure)
    before = service_check_line_trigger(
        instance,
        line.identity.name,
        LineTriggerCheckRequestV1(),
        now=READ_TIME + timedelta(seconds=59),
    )
    assert before.status == "not_met"
    end = READ_TIME + timedelta(seconds=60)
    after = service_check_line_trigger(
        instance,
        line.identity.name,
        LineTriggerCheckRequestV1(since=end, until=end + timedelta(seconds=1)),
        now=end + timedelta(days=1),
    )
    assert after.status == "met"
    assert after.occurrences[0].binding.window.ends_at == end
    journal, _ = _journal(instance)
    path = journal._record_log_path_for_testing(_stream(instance), stored.record.partition_id)
    path.unlink()
    assert (
        service_check_line_trigger(
            instance, line.identity.name, LineTriggerCheckRequestV1(), now=end
        ).status
        == "incomplete"
    )
