"""Exact prediction selectors bind a version; authored Trigger selectors bind a name."""

from datetime import timedelta

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.procedures.windows import (
    CaptureEventNameSelector,
    CaptureEventNameWindow,
    CaptureEventSelector,
    CaptureEventWindow,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.triggers import CaptureLandingSchedule
from cruxible_core.exhaust.payloads import read_payload
from cruxible_core.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.service.procedures.procedure_runs import (
    PROCEDURE_RUN_FENCING_TOKEN,
    _activate_writer,
    _journal,
    _stream,
)
from cruxible_core.service.procedures.resolution_contracts import (
    TriggerCaptureRefused,
    bind_window,
    read_capture_event,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_line_triggers import SELECTOR, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor
from tests.test_service.test_capture_selector_sources import _landing, _reference


@pytest.fixture(scope="module")
def retained(tmp_path_factory):
    instance, _line, procedure, owner = line_world(
        tmp_path_factory.mktemp("exact-event-binding"),
        CaptureLandingSchedule(event=SELECTOR),
        with_owner=True,
    )
    first = foreign_source_capture_contract("selector.feed")
    second = first.model_copy(
        update={
            "selection_budget": first.selection_budget.model_copy(
                update={"max_bytes": first.selection_budget.max_bytes + 1}
            ),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=capture_contract_digest(first).tagged
            ),
        }
    )
    events = []
    for version, contract in enumerate((first, second)):
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        tree[capture_contract_path(contract.identity.name)] = render_capture_contract(contract)
        _accept_tree(
            instance,
            owner,
            tree,
            timestamp=f"2026-08-24T15:0{version + 1}:00.000000Z",
            proposal_name=f"selector-contract-{version}",
        )
        events.append(
            _landing(
                instance,
                procedure,
                "sarif.log",
                partition=f"run:contract-{version}",
                at=READ_TIME + timedelta(seconds=version),
                contract=contract,
            )
        )
    selectors = tuple(
        CaptureEventSelector(
            capture_contract_identity=contract.identity,
            capture_contract_digest=capture_contract_digest(contract).tagged,
            logical_sources=("sarif.log",),
        )
        for contract in (first, second)
    )
    assert selectors[0].capture_contract_identity == selectors[1].capture_contract_identity
    assert selectors[0].capture_contract_digest != selectors[1].capture_contract_digest

    # A real retained frame with the same otherwise-valid produced payload,
    # but a non-Capture record kind, isolates the reader's record-kind guard.
    payload = read_payload(
        instance.body_store(),
        events[0].record.payload_digest,
        access=BodyAccessContext(principal_id="fixture", can_read_body=True),
    )
    journal, _ = _journal(instance)
    stream = _stream(instance)
    _activate_writer(journal, stream, "run:wrong-kind")
    non_capture = ProcedureExhaustWriter(
        journal=journal,
        bodies=instance.body_store(),
        fencing_token=PROCEDURE_RUN_FENCING_TOKEN,
    ).append(
        stream=stream,
        partition_id="run:wrong-kind",
        event_kind="attempt_started",
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        procedure_artifact_digest=procedure.artifact_digest,
        definition_digest=procedure.procedure.definition_digest,
        actor_context=_actor(instance),
        recorded_at=READ_TIME,
        run_id=events[0].record.run_id,
        payload=payload,
    )
    return instance, tuple(events), selectors, non_capture


@pytest.mark.parametrize("version", [0, 1], ids=["retained-predecessor", "current-successor"])
def test_exact_selector_refuses_another_accepted_version_while_name_matches_both(retained, version):
    instance, events, selectors, _non_capture = retained
    event, exact = events[version], selectors[version]
    wrong_version = selectors[1 - version]
    reference = _reference(event)
    now = READ_TIME + timedelta(days=1)

    record, payload = read_capture_event(instance, exact, reference, now=now)
    assert record == event.record
    assert payload["capture_contract_digest"] == exact.capture_contract_digest
    with pytest.raises(TriggerCaptureRefused, match="CaptureContract selector") as refused:
        read_capture_event(instance, wrong_version, reference, now=now)
    assert refused.value.refusal_code == "trigger_capture_invalid"

    name = CaptureEventNameSelector(
        capture_contract_identity=exact.capture_contract_identity,
        logical_sources=exact.logical_sources,
    )
    assert read_capture_event(instance, name, reference, now=now) == (record, payload)
    for window in (
        CaptureEventWindow(event=exact, duration_seconds=60),
        CaptureEventNameWindow(event=name, duration_seconds=60),
    ):
        bound = bind_window(instance, window, reference, now=now)
        assert bound.starts_at == record.recorded_at
        assert bound.ends_at == record.recorded_at + timedelta(seconds=60)
    with pytest.raises(TriggerCaptureRefused, match="CaptureContract selector"):
        bind_window(
            instance,
            CaptureEventWindow(event=wrong_version, duration_seconds=60),
            reference,
            now=now,
        )


@pytest.mark.parametrize("kind", ["exact", "name"])
@pytest.mark.parametrize("failure", ["record", "run", "kind", "identity", "source", "time"])
def test_both_selector_types_preserve_retained_event_integrity_guards(retained, kind, failure):
    instance, events, selectors, non_capture = retained
    exact = selectors[0]
    selector = (
        exact
        if kind == "exact"
        else CaptureEventNameSelector(
            capture_contract_identity=exact.capture_contract_identity,
            logical_sources=exact.logical_sources,
        )
    )
    reference = _reference(events[0])
    now = READ_TIME + timedelta(seconds=2)
    match, code = "retained record", "trigger_capture_invalid"
    if failure == "record":
        reference = reference.model_copy(update={"record_digest": events[1].record_digest})
    elif failure == "run":
        reference = reference.model_copy(update={"run_id": events[1].record.run_id})
        match = "exact run"
    elif failure == "kind":
        reference = _reference(non_capture)
        match = "produced Capture"
    elif failure == "identity":
        selector = selector.model_copy(
            update={
                "capture_contract_identity": ArtifactIdentity(kind="CaptureContract", name="other")
            }
        )
        match = "CaptureContract selector"
    elif failure == "source":
        selector = selector.model_copy(update={"logical_sources": ("github.repo.snapshot",)})
        match = "logical_sources selector"
    elif failure == "time":
        now = READ_TIME - timedelta(seconds=1)
        match, code = "has not occurred", "trigger_capture_not_yet_observed"
    with pytest.raises(TriggerCaptureRefused, match=match) as refused:
        read_capture_event(instance, selector, reference, now=now)
    assert refused.value.refusal_code == code
