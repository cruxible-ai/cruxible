"""Every trigger has one governed home: a Trigger aimed at a Line or an internal action."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.windows import (
    CaptureEventSelector,
    CaptureEventWindow,
    FixedWindow,
)
from cruxible_client.contracts.triggers import (
    INTERNAL_ACTIONS,
    AcceptedTrigger,
    CadenceSchedule,
    CaptureEventInput,
    CaptureLandingSchedule,
    CronSchedule,
    GenerationAcceptedSchedule,
    InternalActionSpec,
    NoTriggerInput,
    Trigger,
    TriggerFormatError,
    WindowCloseSchedule,
    evaluate_trigger_law,
    parse_trigger,
    render_trigger,
    schedule_satisfies_input,
    trigger_digest,
    trigger_path,
)
from cruxible_core.compiler.compiler import (
    AUTHORITY_VERBS_COMPILER,
    GOVERNED_TRIGGERS_COMPILER,
    artifact_kinds_for_compiler,
    projection_registry_for_compiler,
)
from cruxible_core.compiler.projection_artifacts import parse_projection_tree
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from tests.core_support._support import initialize_local
from tests.support.lines import (
    action_trigger,
    graph_v4,
    line_trigger,
    successor,
    trigger_members,
)
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_procedure_run_surface import _slotless_procedure
from tests.test_server.test_playbill_line_run_refusals import (
    _acquisition_policy,
    _line_mandate,
    _served_line,
)

SELECTOR = CaptureEventSelector(
    capture_contract_identity=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity,
    capture_contract_digest=capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged,
)


def _accepted(trigger: Trigger) -> AcceptedTrigger:
    return AcceptedTrigger(
        path=trigger_path(trigger.identity.name),
        trigger=trigger,
        artifact_digest=trigger_digest(trigger).tagged,
    )


def _law(trigger: Trigger, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("predecessor", None)
    return evaluate_trigger_law(trigger, path=trigger_path(trigger.identity.name), **kwargs)


def _code(result) -> str:  # type: ignore[no-untyped-def]
    assert result.verdict == "refused", result
    return result.diagnostics[0].code


def test_a_trigger_round_trips_and_pins_exactly_the_contract_its_schedule_names() -> None:
    trigger = line_trigger(
        "on-landing", line="triage", schedule=CaptureLandingSchedule(event=SELECTOR)
    )
    content = render_trigger(trigger)
    assert parse_trigger(content, path=trigger_path("on-landing")) == trigger
    assert [pin.role for pin in trigger.pins] == ["trigger-capture-contract"]
    assert trigger.pins[0].artifact_digest == SELECTOR.capture_contract_digest
    with pytest.raises(TriggerFormatError, match="identity/path"):
        parse_trigger(content, path=trigger_path("elsewhere"))
    with pytest.raises(ValidationError, match="exactly the CaptureContract"):
        Trigger.model_validate({**json.loads(content), "pins": []})
    ticking = line_trigger("hourly", line="triage", schedule=CadenceSchedule(interval_seconds=60))
    assert ticking.pins == ()
    # The Line is named by identity, never by a version.
    assert json.loads(render_trigger(ticking))["target"] == {
        "kind": "line",
        "line": {"role": "line", "target": {"kind": "Line", "name": "triage"}},
    }


def test_the_trigger_law_judges_targets_actions_and_accepted_events() -> None:
    sweep = action_trigger("sweep", action="evidence.sweep", interval_seconds=86400)
    assert _law(sweep).verdict == "accepted"
    nightly = action_trigger(
        "sweep", action="evidence.sweep", schedule=CronSchedule(expression="0 0 * * *")
    )
    assert _law(nightly).verdict == "accepted"
    # In v1 an internal action takes a time schedule only, whatever its input.
    for schedule in (
        WindowCloseSchedule(
            window=FixedWindow(starts_at="2026-09-30T00:00:00Z", duration_seconds=60)
        ),
        WindowCloseSchedule(window=CaptureEventWindow(event=SELECTOR, duration_seconds=60)),
        CaptureLandingSchedule(event=SELECTOR),
    ):
        refused = _law(action_trigger("sweep", action="evidence.sweep", schedule=schedule))
        assert _code(refused) == "cruxible.trigger.schedule_unsupported_for_action"
        assert "not supported yet" in refused.diagnostics[0].message

    landing = line_trigger(
        "on-landing", line="triage", schedule=CaptureLandingSchedule(event=SELECTOR)
    )
    assert (
        _code(_law(landing, target_line_live=False)) == "cruxible.trigger.target_line_unavailable"
    )
    assert _law(landing, target_line_live=True).verdict == "accepted"
    # A Line that binds its triggering Capture accepts exactly its declared event.
    exact = CaptureEventInput(event=SELECTOR)
    assert _law(landing, target_line_live=True, target_line_input=exact).verdict == "accepted"
    other = SELECTOR.model_copy(update={"capture_contract_digest": "sha256:" + "b" * 64})
    assert (
        _code(
            _law(
                landing,
                target_line_live=True,
                target_line_input=CaptureEventInput(event=other),
            )
        )
        == "cruxible.trigger.event_not_accepted"
    )
    ticking = line_trigger("hourly", line="triage", schedule=CadenceSchedule(interval_seconds=60))
    assert (
        _code(_law(ticking, target_line_live=True, target_line_input=exact))
        == "cruxible.trigger.event_not_accepted"
    )
    event_window = line_trigger(
        "window",
        line="triage",
        schedule=WindowCloseSchedule(
            window=CaptureEventWindow(event=SELECTOR, duration_seconds=60)
        ),
    )
    assert _law(event_window, target_line_live=True, target_line_input=exact).verdict == "accepted"


def test_an_action_is_held_to_its_declared_input_by_the_line_event_rule() -> None:
    # A test-only action that needs a Capture event, judged by the same rule a
    # Line's accepted event is. No v1 schedule an action admits supplies one, so
    # the input rule refuses a time schedule and the v1 limit any event schedule.
    needs_capture = InternalActionSpec(
        name="test.on_capture",
        input=CaptureEventInput(),
        effect="findings",
        consumer="next",
        part="evidence",
    )
    actions = {**INTERNAL_ACTIONS, needs_capture.name: needs_capture}
    ticking = action_trigger("probe", action="test.on_capture", interval_seconds=60)
    refused = _law(ticking, actions=actions)
    assert _code(refused) == "cruxible.trigger.event_not_accepted"
    assert "needs a Capture event" in refused.diagnostics[0].message
    fires_on_event = action_trigger(
        "probe", action="test.on_capture", schedule=CaptureLandingSchedule(event=SELECTOR)
    )
    assert (
        _code(_law(fires_on_event, actions=actions))
        == "cruxible.trigger.schedule_unsupported_for_action"
    )
    assert schedule_satisfies_input(fires_on_event.schedule, needs_capture.input)
    # Without the entry, the name is simply unknown.
    assert _code(_law(ticking)) == "cruxible.trigger.action_unknown"
    # Needing no input, an action is satisfied by every schedule kind.
    assert all(
        schedule_satisfies_input(item, NoTriggerInput())
        for item in (ticking.schedule, CaptureLandingSchedule(event=SELECTOR))
    )


def test_the_trigger_law_refuses_a_cron_schedule_the_grammar_does_not_admit() -> None:
    def nightly(expression: str) -> Trigger:
        return action_trigger(
            "nightly", action="evidence.sweep", schedule=CronSchedule(expression=expression)
        )

    assert _law(nightly("0 2 * * *")).verdict == "accepted"
    on_line = line_trigger(
        "weekdays", line="triage", schedule=CronSchedule(expression="0 9 * * 1-5")
    )
    assert _law(on_line, target_line_live=True).verdict == "accepted"
    for expression, reason in (
        ("0 2 * *", "five"),
        ("0 25 * * *", "outside 0-23"),
        ("0 2 * * MON", "number, range or"),
        ("0 0 30 2 *", "never fires"),
    ):
        refused = _law(nightly(expression))
        assert _code(refused) == "cruxible.trigger.cron_invalid"
        assert reason in refused.diagnostics[0].message
    # A cron tick carries no Capture, so a Line that binds one refuses it.
    assert (
        _code(
            _law(
                on_line,
                target_line_live=True,
                target_line_input=CaptureEventInput(event=SELECTOR),
            )
        )
        == "cruxible.trigger.event_not_accepted"
    )


def test_the_trigger_law_holds_succession_and_never_revives() -> None:
    first = line_trigger("hourly", line="triage", schedule=CadenceSchedule(interval_seconds=3600))
    assert (
        _code(
            _law(
                first.model_copy(
                    update={
                        "lifecycle": ArtifactLifecycle(
                            predecessor_digest=trigger_digest(first).tagged
                        )
                    }
                ),
                predecessor=None,
                target_line_live=True,
            )
        )
        == "cruxible.trigger.invalid_genesis"
    )
    changed = successor(first, schedule=CadenceSchedule(interval_seconds=60))
    assert _law(changed, predecessor=_accepted(first), target_line_live=True).verdict == "accepted"
    assert (
        _code(
            _law(
                changed.model_copy(
                    update={"lifecycle": ArtifactLifecycle(predecessor_digest="sha256:" + "a" * 64)}
                ),
                predecessor=_accepted(first),
                target_line_live=True,
            )
        )
        == "cruxible.trigger.predecessor_mismatch"
    )
    retired = successor(first, state="retired")
    # A retiring Trigger sets nothing off: the Line it named may already be gone.
    assert _law(retired, predecessor=_accepted(first), target_line_live=False).verdict == "accepted"
    revived = successor(retired)
    assert (
        _code(_law(revived, predecessor=_accepted(retired), target_line_live=True))
        == "cruxible.trigger.revival_refused"
    )


def test_only_compiler_revision_32_admits_triggers_and_v6_lines() -> None:
    trigger = action_trigger("sweep", action="evidence.sweep", interval_seconds=86400)
    tree = trigger_members(trigger)
    parsed = parse_projection_tree(
        tree,
        registry=projection_registry_for_compiler(GOVERNED_TRIGGERS_COMPILER),
        artifact_kinds=artifact_kinds_for_compiler(GOVERNED_TRIGGERS_COMPILER),
    )
    (envelope,) = parsed.envelopes
    assert (envelope.identity, envelope.kind) == ("Trigger:sweep", "trigger")
    with pytest.raises(ProjectionFormatError, match="no registered format"):
        parse_projection_tree(
            tree,
            registry=projection_registry_for_compiler(AUTHORITY_VERBS_COMPILER),
            artifact_kinds=artifact_kinds_for_compiler(AUTHORITY_VERBS_COMPILER),
        )


def _line_world(tmp_path):  # type: ignore[no-untyped-def]
    """An accepted Line v6 with no Trigger aimed at it yet."""

    instance, owner = initialize_local(tmp_path)
    accepted = graph_v4(_slotless_procedure("triggered-method"))
    policy = _acquisition_policy("triggered-policy")
    line = _served_line("triggered-line", accepted=accepted, policy=policy)
    mandate = _line_mandate(accepted)
    from cruxible_client.contracts.acquisition_policies import (
        acquisition_policy_path,
        render_acquisition_policy,
    )

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(
        {
            capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name): (
                render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
            ),
            accepted.path: render_procedure(accepted.procedure),
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
            line_spec_path(line.identity.name): render_line_spec(line),
        }
    )
    _accept_tree(
        instance, owner, tree, timestamp="2026-09-30T10:00:00.000000Z", proposal_name="line"
    )
    return instance, owner, line


def _submit(instance, members, name):  # type: ignore[no-untyped-def]
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree.update(members)
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-09-30T10:05:00.000000Z",
    )


def _refused(result) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    assert result.candidate is None, result
    return tuple(item.code for item in result.evaluation.diagnostics)


def test_proposals_hold_triggers_to_the_line_they_aim_at_and_lines_to_their_triggers(tmp_path):
    instance, owner, line = _line_world(tmp_path)
    stray = line_trigger("stray", line="absent-line", schedule=CadenceSchedule(interval_seconds=60))
    assert _refused(_submit(instance, trigger_members(stray), "stray")) == (
        "cruxible.trigger.target_line_unavailable",
    )
    unknown = action_trigger("mystery", action="mystery.action", interval_seconds=60)
    assert _refused(_submit(instance, trigger_members(unknown), "unknown")) == (
        "cruxible.trigger.action_unknown",
    )
    landing_sweep = action_trigger(
        "landing-sweep", action="evidence.sweep", schedule=CaptureLandingSchedule(event=SELECTOR)
    )
    assert _refused(_submit(instance, trigger_members(landing_sweep), "landing-sweep")) == (
        "cruxible.trigger.schedule_unsupported_for_action",
    )

    hourly = line_trigger(
        "hourly", line=line.identity.name, schedule=CadenceSchedule(interval_seconds=3600)
    )
    landing = line_trigger(
        "on-landing", line=line.identity.name, schedule=CaptureLandingSchedule(event=SELECTOR)
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(trigger_members(hourly, landing))
    # A Line can have several Triggers.
    _accept_tree(
        instance, owner, tree, timestamp="2026-09-30T10:01:00.000000Z", proposal_name="two"
    )

    retired_line = line.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=line_spec_digest(line).tagged
            )
        }
    )
    refused = _submit(
        instance, {line_spec_path(line.identity.name): render_line_spec(retired_line)}, "retire"
    )
    assert _refused(refused) == ("cruxible.line.triggers_not_settled",)
    message = refused.evaluation.diagnostics[0].message
    assert "Trigger:hourly" in message and "Trigger:on-landing" in message
    # Retiring one of the two is still not enough; retiring both in the set is.
    half = _submit(
        instance,
        {
            line_spec_path(line.identity.name): render_line_spec(retired_line),
            **trigger_members(successor(hourly, state="retired")),
        },
        "retire-half",
    )
    assert _refused(half) == ("cruxible.line.triggers_not_settled",)
    whole = _submit(
        instance,
        {
            line_spec_path(line.identity.name): render_line_spec(retired_line),
            **trigger_members(
                successor(hourly, state="retired"), successor(landing, state="retired")
            ),
        },
        "retire-whole",
    )
    assert whole.candidate is not None, whole.evaluation.diagnostics
    # A Trigger cannot be removed, only retired.
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree.pop(trigger_path("hourly"))
    removed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/remove", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-09-30T10:06:00.000000Z",
    )
    assert _refused(removed) == ("cruxible.trigger.removal_unsupported",)


def test_new_instances_start_with_the_default_internal_triggers(tmp_path):
    from cruxible_core.triggers.journal import internal_triggers

    instance, _owner = initialize_local(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    defaults = {
        path: parse_trigger(tree[path], path=path) for path in tree if path.startswith("triggers/")
    }
    assert {path: (item.action, item.schedule) for path, item in defaults.items()} == {
        "triggers/evidence-sweep.json": (
            "evidence.sweep",
            CadenceSchedule(interval_seconds=86400),
        ),
        "triggers/floor-refresh.json": ("floor.refresh", GenerationAcceptedSchedule()),
        "triggers/prediction-anchor-retry.json": (
            "prediction.anchor_retry",
            CadenceSchedule(interval_seconds=3600),
        ),
    }
    assert all(item.lifecycle.state == "live" for item in defaults.values())
    assert [(item.action, item.schedule) for item in internal_triggers(instance)] == [
        ("evidence.sweep", CadenceSchedule(interval_seconds=86400)),
        ("floor.refresh", GenerationAcceptedSchedule()),
        ("prediction.anchor_retry", CadenceSchedule(interval_seconds=3600)),
    ]


def test_accepted_trigger_changes_move_the_internal_timers_that_fire(tmp_path):
    from datetime import UTC, datetime, timedelta

    from cruxible_core.triggers.journal import evaluate_triggers

    instance, owner = initialize_local(tmp_path)
    watched = {"Trigger:evidence-hourly", "Trigger:evidence-sweep"}

    def fired(at):  # type: ignore[no-untyped-def]
        # No supplied Trigger set: the accepted head, and each version's own
        # acceptance, decide what fires. Each check listens from its own instant.
        events = evaluate_triggers(instance, now=at, listening_since=at)
        return sorted(event.trigger for event in events if event.trigger in watched)

    def accept(name, at, *triggers):  # type: ignore[no-untyped-def]
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        tree.update(trigger_members(*triggers))
        _accept_tree(
            instance,
            owner,
            tree,
            timestamp=at.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
            proposal_name=name,
        )

    # Accepting a Trigger starts its timer at its acceptance: first fire one
    # interval later, never on sight.
    added_at = datetime(2026, 9, 30, 10, 1, tzinfo=UTC)
    hourly = action_trigger("evidence-hourly", action="evidence.sweep", interval_seconds=3600)
    accept("add", added_at, hourly)
    assert fired(added_at + timedelta(minutes=30)) == []
    assert fired(added_at + timedelta(hours=1)) == ["Trigger:evidence-hourly"]

    # Changing one restarts it from the successor's acceptance.
    changed_at = datetime(2026, 9, 30, 11, 2, tzinfo=UTC)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    daily = parse_trigger(tree["triggers/evidence-sweep.json"], path="triggers/evidence-sweep.json")
    accept("change", changed_at, successor(daily, schedule=CadenceSchedule(interval_seconds=60)))
    assert fired(changed_at + timedelta(seconds=30)) == []
    assert fired(changed_at + timedelta(minutes=1)) == ["Trigger:evidence-sweep"]

    # Retiring one removes it: at its next instant it no longer fires.
    accept("retire", datetime(2026, 9, 30, 11, 5, tzinfo=UTC), successor(hourly, state="retired"))
    assert fired(added_at + timedelta(hours=2)) == ["Trigger:evidence-sweep"]


def test_triggers_are_authored_lowered_and_read_like_other_definitions(tmp_path):
    from datetime import UTC, datetime

    from cruxible_client.authoring.examples import (
        AUTHORING_EXAMPLE_FACTORIES,
        AUTHORING_EXAMPLE_NAMES,
    )
    from cruxible_client.contracts.authoring.inputs import TriggerInput, lower_authoring_input
    from cruxible_client.contracts.authoring.models import TriggerAuthoringPayload
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.authoring.lowering import AuthoringLoweringError, _render_trigger_member
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.storage.cas import BodyAccessContext

    instance, owner, line = _line_world(tmp_path)
    assert "trigger" in AUTHORING_EXAMPLE_NAMES
    example = AUTHORING_EXAMPLE_FACTORIES["trigger"]()
    assert isinstance(example, TriggerInput) and example.line_name == "replace-me"
    payload = lower_authoring_input(
        TriggerInput(
            kind="trigger",
            name="hourly",
            schedule=CadenceSchedule(interval_seconds=3600),
            line_name=line.identity.name,
        )
    )
    assert isinstance(payload, TriggerAuthoringPayload)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path, content, digest = _render_trigger_member(payload, tree=tree)
    authored = parse_trigger(content, path=path)
    assert path == trigger_path("hourly") and digest == trigger_digest(authored).tagged
    assert authored.line == line.identity and authored.lifecycle.predecessor_digest is None
    with pytest.raises(AuthoringLoweringError) as missing:
        _render_trigger_member(payload.model_copy(update={"line_name": "absent"}), tree=tree)
    assert missing.value.code == "cruxible.authoring.trigger_line_missing"

    tree[path] = content
    _accept_tree(instance, owner, tree, timestamp="2026-09-30T10:02:00.000000Z", proposal_name="a")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    # Unchanged, it lowers to the accepted bytes; changed, to a successor of them.
    assert _render_trigger_member(payload, tree=tree)[1] == content
    _path, retired, _digest = _render_trigger_member(
        payload.model_copy(update={"retire": True}), tree=tree
    )
    successor_trigger = parse_trigger(retired, path=path)
    assert successor_trigger.lifecycle.state == "retired"
    assert successor_trigger.lifecycle.predecessor_digest == digest

    card = service_playbill_get(
        instance,
        request=GetRequest(ref="Trigger:hourly", evaluation_time=datetime(2026, 9, 30, tzinfo=UTC)),
        access=BodyAccessContext(principal_id="reader", can_read_body=True),
    )
    assert card.kind == "trigger"
    assert (card.card.trigger, card.card.target, card.card.lifecycle) == (
        "Trigger:hourly",
        line.identity.qualified,
        "live",
    )
    assert card.card.schedule == {"interval_seconds": 3600, "kind": "cadence"}


def test_an_unknown_schedule_kind_fails_loudly_everywhere_it_is_classified() -> None:
    from datetime import UTC, datetime
    from typing import Literal

    from pydantic import BaseModel

    from cruxible_client.contracts.triggers import schedule_capture_selector, schedule_is_timed
    from cruxible_core.triggers.cadence import timer_due

    class _QueryScheduleV1(BaseModel):
        kind: Literal["query"] = "query"

    future = _QueryScheduleV1()
    for classify in (schedule_is_timed, schedule_capture_selector):
        with pytest.raises(TriggerFormatError, match="unsupported Trigger schedule kind 'query'"):
            classify(future)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a timer"):
        timer_due(future, last=datetime(2026, 9, 30, tzinfo=UTC))  # type: ignore[arg-type]
    # The wire union is discriminated: a kind it does not name is refused at parse.
    wire = json.loads(
        render_trigger(action_trigger("probe", action="evidence.sweep", interval_seconds=60))
    )
    with pytest.raises(ValidationError, match="query"):
        Trigger.model_validate({**wire, "schedule": {"kind": "query"}})


def test_generation_schedule_uses_the_existing_target_input_law() -> None:
    from cruxible_client.contracts.triggers import GenerationAcceptedSchedule, schedule_is_timed
    from cruxible_core.service.discovery.operational import schedule_summary

    schedule = GenerationAcceptedSchedule()
    refresh = action_trigger("floor", action="floor.refresh", schedule=schedule)
    assert _law(refresh).verdict == "accepted"
    assert not schedule_is_timed(schedule)
    assert schedule_summary(schedule) == "when a new generation is accepted"
    capture_line = line_trigger("capture-line", line="triage", schedule=schedule)
    assert (
        _code(
            _law(
                capture_line,
                target_line_live=True,
                target_line_input=CaptureEventInput(event=SELECTOR),
            )
        )
        == "cruxible.trigger.event_not_accepted"
    )
    capture_action = action_trigger(
        "floor", action="floor.refresh", schedule=CaptureLandingSchedule(event=SELECTOR)
    )
    assert _code(_law(capture_action)) == "cruxible.trigger.schedule_unsupported_for_action"
