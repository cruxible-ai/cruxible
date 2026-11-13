"""The Runbook definition kind (D-R36): a flat ordered list of Trigger targets.

Authoring lowers ``{line}`` / ``{action}`` steps to the Trigger's own
``LineTarget`` / ``ActionTarget``; a missing or retired Line, a Line that needs
an event, and an unregistered action refuse at authoring, and the acceptance law
judges the same again in the final candidate. A Trigger may aim at a Runbook on
a time schedule or generation acceptance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
)
from cruxible_client.contracts.kits import kit_artifact_path_allowed
from cruxible_client.contracts.runbooks import (
    AcceptedRunbook,
    Runbook,
    RunbookLineState,
    evaluate_runbook_law,
    parse_runbook,
    render_runbook,
    runbook_digest,
    runbook_path,
)
from cruxible_client.contracts.triggers import (
    ActionTarget,
    CadenceSchedule,
    LineTarget,
    RunbookTarget,
    Trigger,
    evaluate_trigger_law,
    parse_trigger,
    trigger_path,
)
from cruxible_core.authoring.lowering import AuthoringLoweringError
from tests.support.runbooks import (
    PROPOSE,
    SETTLE,
    author_runbook,
    author_runbook_trigger,
    line_steps,
    runbook_world,
)


def _line(name: str) -> LineTarget:
    return LineTarget(line=ArtifactNameRef(target=ArtifactIdentity(kind="Line", name=name)))


def _runbook(*steps: Any, predecessor: str | None = None, state: str = "live") -> Runbook:
    return Runbook(
        identity=ArtifactIdentity(kind="Runbook", name="test.rb"),
        steps=steps,
        lifecycle=ArtifactLifecycle(state=state, predecessor_digest=predecessor),  # type: ignore[arg-type]
    )


def _codes(result: Any) -> list[str]:
    return [item.code for item in result.diagnostics]


# -- the law, as a pure function ---------------------------------------------------


def test_the_law_accepts_live_event_free_lines_and_registered_actions() -> None:
    runbook = _runbook(_line("a"), ActionTarget(action="evidence.sweep"), _line("a"))
    result = evaluate_runbook_law(
        runbook,
        path=runbook_path("test.rb"),
        predecessor=None,
        lines={"Line:a": RunbookLineState(live=True)},
    )
    assert result.verdict == "accepted"
    assert result.artifact_digest == runbook_digest(runbook).tagged
    assert result.required_tier == "governed_write"


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (None, "cruxible.runbook.line_unavailable"),
        (RunbookLineState(live=False), "cruxible.runbook.line_unavailable"),
        (RunbookLineState(live=True, takes_event=True), "cruxible.runbook.line_takes_event"),
    ],
)
def test_the_law_refuses_a_line_it_cannot_run(state: Any, code: str) -> None:
    lines = {} if state is None else {"Line:a": state}
    result = evaluate_runbook_law(
        _runbook(_line("a")), path=runbook_path("test.rb"), predecessor=None, lines=lines
    )
    assert _codes(result) == [code]


def test_the_law_refuses_an_unregistered_action() -> None:
    result = evaluate_runbook_law(
        _runbook(ActionTarget(action="nothing.here")),
        path=runbook_path("test.rb"),
        predecessor=None,
        lines={},
    )
    assert _codes(result) == ["cruxible.runbook.action_unknown"]


def test_the_law_judges_succession() -> None:
    first = _runbook(_line("a"))
    accepted = AcceptedRunbook(
        path=runbook_path("test.rb"), runbook=first, artifact_digest=runbook_digest(first).tagged
    )
    lines = {"Line:a": RunbookLineState(live=True)}
    path = runbook_path("test.rb")
    assert _codes(
        evaluate_runbook_law(
            _runbook(_line("a"), predecessor=accepted.artifact_digest),
            path=path,
            predecessor=None,
            lines=lines,
        )
    ) == ["cruxible.runbook.invalid_genesis"]
    assert _codes(
        evaluate_runbook_law(_runbook(_line("a")), path=path, predecessor=accepted, lines=lines)
    ) == ["cruxible.runbook.predecessor_mismatch"]
    retired = _runbook(_line("a"), predecessor=accepted.artifact_digest, state="retired")
    # A retiring Runbook is judged only for its succession: its Lines no longer matter.
    assert (
        evaluate_runbook_law(retired, path=path, predecessor=accepted, lines={}).verdict
        == "accepted"
    )
    gone = AcceptedRunbook(
        path=path, runbook=retired, artifact_digest=runbook_digest(retired).tagged
    )
    assert _codes(
        evaluate_runbook_law(
            _runbook(_line("a"), predecessor=gone.artifact_digest),
            path=path,
            predecessor=gone,
            lines=lines,
        )
    ) == ["cruxible.runbook.revival_refused"]


def test_a_runbook_is_flat_and_bounded() -> None:
    with pytest.raises(ValueError):
        _runbook()  # at least one step
    with pytest.raises(ValueError):
        _runbook(*(_line("a") for _ in range(65)))
    with pytest.raises(ValueError):
        # A Runbook step is a Line or an action: never another Runbook.
        Runbook.model_validate(
            {
                "identity": {"kind": "Runbook", "name": "test.rb"},
                "steps": [
                    {"kind": "runbook", "runbook": {"target": {"kind": "Runbook", "name": "x"}}}
                ],
            }
        )


def test_runbooks_are_not_kit_content_while_lines_are_local() -> None:
    """A Runbook names Lines, which are local binding (never kit content), and its
    law refuses a missing Line: a kit-shipped Runbook could not be accepted on
    install, so the family stays out of KIT_ARTIFACT_PREFIXES."""

    assert not kit_artifact_path_allowed(runbook_path("security.catch-up"))


# -- authoring and acceptance --------------------------------------------------------


def test_authoring_lowers_steps_to_trigger_targets_and_accepts(tmp_path: Path) -> None:
    instance, _root = runbook_world(tmp_path)
    changed = author_runbook(
        instance,
        "test.rb",
        [{"line": SETTLE}, {"action": "evidence.sweep"}, {"line": PROPOSE}],
        description="Catch up the fixture sources.",
    )
    path = runbook_path("test.rb")
    assert list(changed) == [path]
    accepted = instance.tree_at(instance.accepted_coordinate().git_oid)[path]
    runbook = parse_runbook(accepted, path=path)
    assert runbook.steps == (_line(SETTLE), ActionTarget(action="evidence.sweep"), _line(PROPOSE))
    assert runbook.description == "Catch up the fixture sources."
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        assert projection.typed.source("Runbook:test.rb") == runbook
    # The same decision again changes nothing.
    assert (
        author_runbook(
            instance,
            "test.rb",
            [{"line": SETTLE}, {"action": "evidence.sweep"}, {"line": PROPOSE}],
            description="Catch up the fixture sources.",
        )
        == {}
    )
    # A revision names its exact predecessor.
    author_runbook(instance, "test.rb", line_steps(PROPOSE))
    revised = parse_runbook(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    assert revised.lifecycle.predecessor_digest == runbook_digest(runbook).tagged
    assert revised.steps == (_line(PROPOSE),)


@pytest.mark.parametrize(
    ("step", "code"),
    [
        ({"line": "test.no-such-line"}, "cruxible.authoring.runbook_line_missing"),
        ({"action": "nothing.here"}, "cruxible.authoring.runbook_action_unknown"),
    ],
)
def test_a_target_it_cannot_run_refuses_at_authoring(
    tmp_path: Path, step: dict[str, str], code: str
) -> None:
    instance, _root = runbook_world(tmp_path)
    base = instance.accepted_coordinate()
    with pytest.raises(AuthoringLoweringError) as refused:
        author_runbook(instance, "test.rb", [{"line": SETTLE}, step])
    assert refused.value.code == code
    assert refused.value.offending_element.startswith("steps[1]")
    assert instance.accepted_coordinate() == base


def test_a_line_that_takes_an_event_refuses_at_authoring(tmp_path: Path) -> None:
    from cruxible_client.contracts.authoring.models import RunbookAuthoringPayload
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, parse_line_spec
    from cruxible_client.contracts.triggers import CaptureEventNameSelector
    from cruxible_core.authoring.runbook_lowering import render_runbook_member

    instance, _root = runbook_world(tmp_path)
    tree = dict(instance.tree_at(instance.accepted_coordinate().git_oid))
    line = parse_line_spec(tree[line_spec_path(SETTLE)], path=line_spec_path(SETTLE))
    evented = line.model_copy(
        update={
            "trigger_input": "doc",
            "trigger_event": CaptureEventNameSelector(
                capture_contract_identity=ArtifactIdentity(
                    kind="CaptureContract", name="test.orders-v1"
                )
            ),
        }
    )
    from cruxible_client.contracts.procedures.line_specs import render_line_spec

    tree[line_spec_path(SETTLE)] = render_line_spec(evented)
    with pytest.raises(AuthoringLoweringError) as refused:
        render_runbook_member(
            RunbookAuthoringPayload(name="test.rb", steps=({"line": SETTLE},)), tree=tree
        )
    assert refused.value.code == "cruxible.authoring.runbook_line_takes_event"


def test_the_acceptance_law_refuses_a_runbook_naming_a_missing_line(tmp_path: Path) -> None:
    """Lowering is not the only gate: a candidate written by hand meets the law."""

    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    instance, _root = runbook_world(tmp_path)
    base = instance.accepted_coordinate()
    tree = dict(instance.tree_at(base.git_oid))
    tree[runbook_path("test.rb")] = render_runbook(_runbook(_line("test.no-such-line")))
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/bad-runbook", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-10-09T11:59:00.000000Z",
    )
    assert proposed.candidate is None
    codes = [item.code for item in proposed.evaluation.diagnostics]
    assert "cruxible.runbook.line_unavailable" in codes, codes


def test_retiring_a_runbook(tmp_path: Path) -> None:
    from cruxible_client.contracts.authoring.inputs import RunbookInput
    from tests.support.runbooks import author

    instance, _root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", line_steps(SETTLE))
    author(
        instance,
        RunbookInput(kind="runbook", name="test.rb", retire=True),
        name="retire-runbook",
    )
    path = runbook_path("test.rb")
    retired = parse_runbook(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    assert retired.lifecycle.state == "retired"
    with pytest.raises(AuthoringLoweringError) as refused:
        author_runbook(instance, "test.rb", line_steps(SETTLE))
    assert refused.value.code == "cruxible.runbook.revival_refused"


# -- a Runbook as a Trigger target ---------------------------------------------------


def test_a_trigger_may_aim_at_a_runbook_on_a_time_schedule(tmp_path: Path) -> None:
    instance, _root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", line_steps(SETTLE))
    changed = author_runbook_trigger(
        instance,
        "test.rb-hourly",
        runbook="test.rb",
        schedule={"kind": "cadence", "interval_seconds": 3600},
    )
    path = trigger_path("test.rb-hourly")
    trigger = parse_trigger(changed[path], path=path)
    assert trigger.target == RunbookTarget(
        runbook=ArtifactNameRef(target=ArtifactIdentity(kind="Runbook", name="test.rb"))
    )
    assert trigger.runbook == ArtifactIdentity(kind="Runbook", name="test.rb")
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        row = projection.typed.connection.execute(
            "SELECT target_kind,target FROM triggers WHERE identity=?", ("Trigger:test.rb-hourly",)
        ).fetchone()
    assert tuple(row) == ("runbook", "Runbook:test.rb")


def test_a_runbook_trigger_refuses_a_capture_schedule_and_a_missing_runbook(
    tmp_path: Path,
) -> None:
    instance, _root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", line_steps(SETTLE))
    with pytest.raises(AuthoringLoweringError) as capture:
        author_runbook_trigger(
            instance,
            "test.rb-landing",
            runbook="test.rb",
            schedule={"kind": "capture_landing", "capture_contract": "test.orders-v1"},
        )
    assert capture.value.code == "cruxible.trigger.schedule_unsupported_for_runbook"
    with pytest.raises(AuthoringLoweringError) as missing:
        author_runbook_trigger(
            instance,
            "test.rb-missing",
            runbook="test.none",
            schedule={"kind": "generation_accepted"},
        )
    assert missing.value.code == "cruxible.authoring.trigger_runbook_missing"


def test_the_trigger_law_judges_a_runbook_target() -> None:
    trigger = Trigger(
        identity=ArtifactIdentity(kind="Trigger", name="t"),
        schedule=CadenceSchedule(interval_seconds=60),
        target=RunbookTarget(
            runbook=ArtifactNameRef(target=ArtifactIdentity(kind="Runbook", name="test.rb"))
        ),
    )
    path = trigger_path("t")
    assert (
        evaluate_trigger_law(trigger, path=path, predecessor=None, target_runbook_live=True).verdict
        == "accepted"
    )
    refused = evaluate_trigger_law(trigger, path=path, predecessor=None, target_runbook_live=False)
    assert [item.code for item in refused.diagnostics] == [
        "cruxible.trigger.target_runbook_unavailable"
    ]


# -- one change set: Lines, the Runbook over them, the Trigger aimed at it ----------


def test_a_runbook_may_name_lines_staged_in_the_same_change(tmp_path: Path) -> None:
    """D-R48: an adopted automation template stages its Lines, the start Runbook
    naming them and a Trigger aimed at that Runbook in ONE change set. Lowering
    stages Runbooks after Lines and Mandates and before Triggers, so each reads
    the change's own candidate tree; the acceptance law judges the same tree."""

    from cruxible_client.contracts.authoring.inputs import ChangeSetInput
    from cruxible_client.contracts.procedures.line_specs import line_spec_path
    from tests.support.runbooks import author

    instance, _root = runbook_world(tmp_path)
    changed = author(
        instance,
        ChangeSetInput.model_validate(
            {
                "kind": "change_set",
                "members": [
                    {
                        "kind": "line",
                        "name": "test.same-change-line",
                        "procedure_name": "test.override",
                        "parameters": {"path": "data/doc.json"},
                        "max_authority": "observe",
                    },
                    {
                        "kind": "runbook",
                        "name": "test.start",
                        "steps": [{"line": "test.same-change-line"}, {"line": SETTLE}],
                    },
                    {
                        "kind": "trigger",
                        "name": "test.start-daily",
                        "runbook_name": "test.start",
                        "schedule": {"kind": "cron", "expression": "0 6 * * *"},
                    },
                ],
            }
        ),
        name="same-change",
    )
    assert set(changed) == {
        line_spec_path("test.same-change-line"),
        runbook_path("test.start"),
        trigger_path("test.start-daily"),
    }
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        runbook = projection.typed.source("Runbook:test.start")
        trigger = projection.typed.source("Trigger:test.start-daily")
    assert runbook.steps == (_line("test.same-change-line"), _line(SETTLE))
    assert trigger.runbook == ArtifactIdentity(kind="Runbook", name="test.start")


def test_a_runbook_naming_a_line_the_change_does_not_stage_still_refuses(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.authoring.inputs import ChangeSetInput
    from tests.support.runbooks import author

    instance, _root = runbook_world(tmp_path)
    base = instance.accepted_coordinate()
    with pytest.raises(AuthoringLoweringError) as refused:
        author(
            instance,
            ChangeSetInput.model_validate(
                {
                    "kind": "change_set",
                    "members": [
                        {
                            "kind": "line",
                            "name": "test.same-change-line",
                            "procedure_name": "test.override",
                            "parameters": {"path": "data/doc.json"},
                            "max_authority": "observe",
                        },
                        {
                            "kind": "runbook",
                            "name": "test.start",
                            "steps": [{"line": "test.not-staged"}],
                        },
                    ],
                }
            ),
            name="same-change-missing",
        )
    assert refused.value.code == "cruxible.authoring.runbook_line_missing"
    assert instance.accepted_coordinate() == base
