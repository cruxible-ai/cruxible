"""Contract names are authoring conveniences; accepted Triggers stay exact."""

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.authoring.models import (
    CaptureContractAuthoringPayload,
    CaptureEventByNameWindow,
    CaptureLandingByNameSchedule,
    ChangeSetAuthoringPayload,
    TriggerAuthoringPayload,
    WindowCloseAuthoringSchedule,
)
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT as CONTRACT,
)
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelector,
    CaptureEventWindow,
)
from cruxible_client.contracts.triggers import (
    CaptureLandingSchedule,
    WindowCloseSchedule,
    parse_trigger,
    trigger_schedule_pins,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import AuthoringLoweringError, _render_trigger_member
from tests.core_support._identity_support import OWNER
from tests.test_procedures.test_procedure_run_surface import _slotless_procedure
from tests.test_procedures.test_trigger_artifacts import _line_world
from tests.test_server.test_playbill_line_run_refusals import _served_line

NAME = CONTRACT.identity.name
PATH = capture_contract_path(NAME)
SELECTOR = CaptureEventSelector(
    capture_contract_identity=CONTRACT.identity,
    capture_contract_digest=capture_contract_digest(CONTRACT).tagged,
)


def tree():
    # "triage" has no source/call node, so its Line closes with no slot
    # binding and no acquisition override (B2: `_served_line` carries none).
    line = _served_line("triage", accepted=_slotless_procedure("triage"))
    return {
        PATH: render_capture_contract(CONTRACT),
        line_spec_path("triage"): render_line_spec(line),
    }


def payload(**updates):
    return TriggerAuthoringPayload(
        **{
            "name": "on-landing",
            "line_name": "triage",
            "schedule": CaptureLandingByNameSchedule(capture_contract=NAME),
            **updates,
        }
    )


@pytest.mark.parametrize("window_close", [False, True])
@pytest.mark.parametrize("failure", ["missing", "retired", "wrong_kind"])
def test_unavailable_named_contract_is_a_typed_authoring_refusal(failure, window_close):
    staged = tree()
    if failure == "missing":
        staged.pop(PATH)
    elif failure == "retired":
        staged[PATH] = render_capture_contract(
            CONTRACT.model_copy(update={"lifecycle": ArtifactLifecycle(state="retired")})
        )
    else:
        staged[PATH] = staged[line_spec_path("triage")]
    schedule = (
        WindowCloseAuthoringSchedule(
            window=CaptureEventByNameWindow(
                capture_contract=NAME,
                duration_seconds=60,
            )
        )
        if window_close
        else CaptureLandingByNameSchedule(capture_contract=NAME)
    )
    with pytest.raises(AuthoringLoweringError) as caught:
        _render_trigger_member(payload(schedule=schedule), tree=staged)
    assert caught.value.code == "cruxible.authoring.trigger_capture_missing"
    assert caught.value.offending_element == "schedule"
    assert PATH in caught.value.message
    assert "Declare or install" in caught.value.repairs[0].description
    assert "existing live CaptureContract" in caught.value.repairs[0].description


@pytest.mark.parametrize(
    "extra",
    [{"event": SELECTOR.model_dump()}, {"expected_digest": SELECTOR.capture_contract_digest}],
)
def test_name_schedule_refuses_exact_selector_or_expected_digest(extra):
    with pytest.raises(ValidationError):
        TriggerAuthoringPayload.model_validate(
            {
                "name": "on-feed",
                "line_name": "triage",
                "schedule": {
                    "kind": "capture_landing",
                    "capture_contract": NAME,
                    **extra,
                },
            }
        )


def test_named_resolution_reads_one_contract_path_without_enumerating_the_tree():
    class DirectReads(Mapping[str, bytes]):
        def __init__(self):
            self.values = tree()
            self.reads: list[str] = []

        def __getitem__(self, key: str) -> bytes:
            self.reads.append(key)
            return self.values[key]

        def __iter__(self) -> Iterator[str]:
            pytest.fail("named Trigger resolution must not enumerate the instance")

        def __len__(self) -> int:
            pytest.fail("named Trigger resolution must not count the instance")

    staged = DirectReads()
    _render_trigger_member(payload(), tree=staged)
    assert staged.reads.count(PATH) == 1
    assert set(staged.reads) <= {PATH, line_spec_path("triage"), "triggers/on-landing.json"}


def test_payload_prepare_resolves_against_candidate_overlay_and_explains_the_pin(tmp_path: Path):
    instance, _owner, line, _mandate = _line_world(tmp_path)
    contract = foreign_source_capture_contract("security.advisory").model_copy(
        update={
            "identity": ArtifactIdentity(kind="CaptureContract", name="security.advisory"),
        }
    )
    declaration = CaptureContractAuthoringPayload(capture_contract=contract)
    trigger = payload(
        line_name=line.identity.name,
        schedule=CaptureLandingByNameSchedule(
            capture_contract=contract.identity.name,
        ),
    )
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    authored = coordinator.create(
        actor=OWNER,
        payload=ChangeSetAuthoringPayload(members=(declaration, trigger)),
        canonical_timestamp="2026-10-17T12:00:00.000000Z",
    ).intent
    computed, _ = coordinator._compute_and_bind_preflight(authored.intent_id, actor=OWNER)
    assert computed.result.verdict == "passed", computed.result.frontier
    assert computed.lowered is not None
    member = next(
        m
        for m in computed.lowered.resolved_authoring["members"]
        if m["identity"].startswith("Trigger:")
    )
    assert member["capture_contract"] == {
        "capture_contract_identity": contract.identity.model_dump(mode="json"),
    }
    stored = parse_trigger(
        computed.lowered.proposed_tree["triggers/on-landing.json"], path="triggers/on-landing.json"
    )
    assert stored.artifact_format == "cruxible-trigger-v2"
    assert stored.pins == ()
    assert stored.schedule.event.capture_contract_identity == contract.identity


@pytest.mark.parametrize("window_close", [False, True])
def test_exact_event_authoring_form_is_refused(window_close):
    from cruxible_client.contracts.authoring.inputs import TriggerInput

    schedule = (
        WindowCloseSchedule(window=CaptureEventWindow(event=SELECTOR, duration_seconds=60))
        if window_close
        else CaptureLandingSchedule(event=SELECTOR)
    )
    for model in (TriggerInput, TriggerAuthoringPayload):
        values = {"name": "on-feed", "line_name": "triage", "schedule": schedule.model_dump()}
        if model is TriggerInput:
            values["kind"] = "trigger"
        with pytest.raises(ValidationError):
            model.model_validate(values)


@pytest.mark.parametrize("unavailable", ["missing", "retired", "changed", "wrong_kind"])
@pytest.mark.parametrize("window_close", [False, True])
def test_retiring_trigger_reuses_predecessor_without_resolving_contract(
    unavailable,
    window_close,
    monkeypatch,
):
    from cruxible_core.authoring import lowering

    staged = tree()
    decision = (
        payload(
            schedule=WindowCloseAuthoringSchedule(
                window=CaptureEventByNameWindow(
                    capture_contract=NAME,
                    duration_seconds=60,
                )
            )
        )
        if window_close
        else payload()
    )
    path, content, digest = _render_trigger_member(decision, tree=staged)
    staged[path] = content
    previous = parse_trigger(content, path=path)
    if unavailable == "missing":
        staged.pop(PATH)
    elif unavailable == "retired":
        staged[PATH] = render_capture_contract(
            CONTRACT.model_copy(
                update={
                    "lifecycle": ArtifactLifecycle(state="retired"),
                }
            )
        )
    elif unavailable == "changed":
        staged[PATH] = render_capture_contract(
            CONTRACT.model_copy(
                update={
                    "evidence_kinds": ("changed",),
                }
            )
        )
    else:
        staged[PATH] = staged[line_spec_path("triage")]
    staged.pop(line_spec_path("triage"))
    monkeypatch.setattr(
        lowering,
        "_resolve_trigger_authoring_schedule",
        lambda *args, **kwargs: pytest.fail("retirement must not resolve"),
    )
    retired_path, retired_content, _ = _render_trigger_member(
        decision.model_copy(update={"retire": True}),
        tree=staged,
    )
    retired = parse_trigger(retired_content, path=retired_path)
    assert retired.lifecycle == ArtifactLifecycle(state="retired", predecessor_digest=digest)
    assert (retired.schedule, retired.pins, retired.target) == (
        previous.schedule,
        previous.pins,
        previous.target,
    )
    staged[path] = retired_content
    assert (
        _render_trigger_member(decision.model_copy(update={"retire": True}), tree=staged)[1]
        == retired_content
    )


def test_retiring_an_absent_trigger_refuses_without_resolving_its_schedule():
    with pytest.raises(AuthoringLoweringError) as caught:
        _render_trigger_member(payload(retire=True), tree={})
    assert caught.value.code == "cruxible.authoring.trigger_missing"
    assert caught.value.repairs


@pytest.mark.parametrize(
    "sources", [("b", "a"), ("a", "a"), ("",), ("https://example.com",), ("UPPER",)]
)
def test_selector_and_authored_sources_must_be_sorted_unique_logical_names(sources):
    for model, fields in (
        (CaptureEventSelector, SELECTOR.model_dump()),
        (CaptureLandingByNameSchedule, {"capture_contract": NAME}),
        (CaptureEventByNameWindow, {"capture_contract": NAME, "duration_seconds": 60}),
    ):
        with pytest.raises(ValidationError, match="logical_sources"):
            model.model_validate({**fields, "logical_sources": sources})


def test_unrestricted_selector_keeps_its_historical_serialization():
    assert SELECTOR.model_dump(mode="json") == {
        "capture_contract_identity": CONTRACT.identity.model_dump(mode="json"),
        "capture_contract_digest": capture_contract_digest(CONTRACT).tagged,
    }
    assert (
        CaptureEventSelector.model_validate({**SELECTOR.model_dump(), "logical_sources": []})
        == SELECTOR
    )


@pytest.mark.parametrize(
    "required, offered, allowed",
    [
        ((), ("sarif.log",), True),
        (("sarif.log",), (), False),
        (("sarif.log",), ("sarif.log",), True),
        (("github.repo.snapshot", "sarif.log"), ("sarif.log",), True),
        (("sarif.log",), ("github.repo.snapshot", "sarif.log"), False),
        (("sarif.log",), ("github.repo.snapshot",), False),
    ],
)
def test_trigger_input_accepts_only_sources_covered_by_its_line(required, offered, allowed):
    from cruxible_client.contracts.triggers import CaptureEventInput, schedule_satisfies_input

    selector = SELECTOR.model_copy(update={"logical_sources": offered})
    need = CaptureEventInput(event=SELECTOR.model_copy(update={"logical_sources": required}))
    assert schedule_satisfies_input(CaptureLandingSchedule(event=selector), need) is allowed
    assert (
        schedule_satisfies_input(
            WindowCloseSchedule(window=CaptureEventWindow(event=selector, duration_seconds=60)),
            need,
        )
        is allowed
    )


def test_named_sourced_trigger_can_target_an_unrestricted_source_input():
    from cruxible_client.contracts.procedures.line_specs import parse_line_spec

    staged = tree()
    path = line_spec_path("triage")
    line = parse_line_spec(staged[path], path=path)
    staged[path] = render_line_spec(
        line.model_copy(
            update={
                "trigger_input": "feed",
                "trigger_event": SELECTOR,
                "pins": tuple(
                    sorted(
                        (
                            *line.pins,
                            *trigger_schedule_pins(CaptureLandingSchedule(event=SELECTOR)),
                        ),
                        key=lambda p: (p.role, p.target.qualified, p.artifact_digest),
                    )
                ),
            }
        )
    )
    decision = payload(
        schedule=CaptureLandingByNameSchedule(capture_contract=NAME, logical_sources=("sarif.log",))
    )
    path, content, _ = _render_trigger_member(decision, tree=staged)
    assert parse_trigger(content, path=path).schedule.event.logical_sources == ("sarif.log",)
