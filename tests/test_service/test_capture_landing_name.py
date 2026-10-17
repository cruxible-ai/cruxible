"""Prepare and runtime boundaries for authoring-only CaptureContract names."""

from datetime import timedelta

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.authoring.inputs import TriggerInput
from cruxible_client.contracts.authoring.models import (
    CaptureLandingByNameSchedule,
    TriggerAuthoringPayload,
)
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT as CONTRACT,
)
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.line_dispatch import LineEvaluateRequest
from cruxible_client.contracts.triggers import CaptureLandingSchedule, parse_trigger, trigger_path
from cruxible_core.authoring import lowering
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.service.procedures.line_dispatch import service_evaluate_line
from tests.core_support._identity_support import OWNER
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME


def test_decision_input_prepare_calls_the_same_resolver_and_explains_the_pin(tmp_path, monkeypatch):
    instance, line, _procedure = line_world(
        tmp_path, CaptureLandingSchedule(event=SELECTOR), triggers=()
    )
    calls = []
    resolve = lowering._resolve_trigger_authoring_schedule

    def tracked(schedule, **kwargs):
        calls.append(schedule)
        return resolve(schedule, **kwargs)

    monkeypatch.setattr(lowering, "_resolve_trigger_authoring_schedule", tracked)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    result = coordinator.compile_input(
        actor=OWNER,
        input=TriggerInput.model_validate(
            {
                "kind": "trigger",
                "name": "on-feed",
                "line_name": line.identity.name,
                "schedule": {"kind": "capture_landing", "capture_contract": CONTRACT.identity.name},
            }
        ),
        canonical_timestamp="2026-10-17T12:00:00.000000Z",
    )
    assert result.verdict == "passed", result.frontier
    assert calls and all(isinstance(schedule, CaptureLandingByNameSchedule) for schedule in calls)
    assert result.resolved_trigger_contracts[0].trigger_name == "on-feed"
    assert result.resolved_trigger_contracts[0].event == SELECTOR
    from cruxible_client.contracts import AuthoringPreflightResult

    served = AuthoringPreflightResult.model_validate(result.model_dump(mode="json"))
    assert served.resolved_trigger_contracts == result.resolved_trigger_contracts


def test_stored_named_trigger_keeps_its_digest_after_a_contract_successor(tmp_path):
    instance, line, procedure, owner = line_world(
        tmp_path,
        CaptureLandingSchedule(event=SELECTOR),
        triggers=(),
        with_owner=True,
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path, content, _ = lowering._render_trigger_member(
        TriggerAuthoringPayload(
            name="on-feed",
            line_name=line.identity.name,
            schedule=CaptureLandingByNameSchedule(capture_contract=CONTRACT.identity.name),
        ),
        tree=tree,
    )
    tree[path] = content
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-24T15:01:00.000000Z", proposal_name="named"
    )
    successor = CONTRACT.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=capture_contract_digest(CONTRACT).tagged
            ),
            "evidence_kinds": ("revised-evidence",),
        }
    )
    new_digest = capture_contract_digest(successor).tagged
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[capture_contract_path(CONTRACT.identity.name)] = render_capture_contract(successor)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-24T15:02:00.000000Z", proposal_name="contract"
    )
    accepted = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert accepted[trigger_path("on-feed")] == content
    assert parse_trigger(content, path=path).schedule.event == SELECTOR
    capture(instance, procedure, digest=new_digest, partition="run:new", at=READ_TIME)
    old = capture(instance, procedure, partition="run:old", at=READ_TIME + timedelta(seconds=1))
    result = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(until=READ_TIME + timedelta(seconds=2), dry_run=True),
        actor=None,
        now=READ_TIME + timedelta(seconds=2),
    )
    assert result.status == "met", result
    assert [item.binding.event.record_digest for item in result.occurrences] == [old.record_digest]
