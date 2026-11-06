"""The Procedure template is closed at authoring, for every Procedure (D-R37 h1).

A Procedure is read, call (fetch, then derive), extract and claims, with
filter/dedupe narrowing and inbox egress. Every other step kind (guard, halt,
select, repeat, invoke, emit, project, and the shape/join/aggregate/union/derive
transforms) refuses ``cruxible.authoring.procedure_step_closed`` at the step,
pointing to a provider derive: offline when the input is lowered, in the SDK
preview, and on the service authoring path below the input. The executor keeps
its handlers (h3); this module is deliberately not ``executor_only``.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.authoring.procedures import Sequence
from cruxible_client.contracts.authoring.inputs import (
    AuthoringInputError,
    ProcedureInput,
    lower_authoring_input,
)
from cruxible_client.contracts.authoring.models import ProcedureAuthoringPayload
from cruxible_client.contracts.procedures.authored import (
    CLOSED_STEP_KINDS,
    EmitStep,
    GuardStep,
    HaltStep,
    InvokeStep,
    ProcedureSpec,
    ProcedureSpecError,
    ProjectStep,
    RepeatStep,
    SelectStep,
    TransformStep,
    closed_step_kind,
    refuse_closed_steps,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.proposals.proposals import AuthenticatedActor
from tests.core_support._support import initialize_local
from tests.support.procedures import procedure_spec

CODE = "cruxible.authoring.procedure_step_closed"
CLOSED_STEPS: dict[str, Any] = {
    "guard": GuardStep,
    "halt": HaltStep,
    "select": SelectStep,
    "repeat": RepeatStep,
    "invoke": InvokeStep,
    "emit": EmitStep,
    "project": ProjectStep,
}


def _closed(kind: str) -> Any:
    """One step of ``kind``, built without its own validation (only its kind matters)."""

    if kind.startswith("transform."):
        return TransformStep.model_construct(transform="t", kind=kind.split(".", 1)[1])
    return CLOSED_STEPS[kind].model_construct()


@pytest.mark.parametrize("kind", sorted(CLOSED_STEP_KINDS))
def test_every_closed_kind_refuses_at_its_step_pointing_to_derive(kind: str) -> None:
    open_step = TransformStep.model_construct(transform="narrow", kind="filter")
    spec = ProcedureSpec.model_construct(procedure="p", steps=(open_step, _closed(kind)))

    assert closed_step_kind(spec.steps[1]) == kind
    with pytest.raises(ProcedureSpecError) as caught:
        refuse_closed_steps(spec)
    assert caught.value.code == CODE
    assert caught.value.field_path == "$.steps[1]"
    assert "derive" in caught.value.message and "derive" in caught.value.repair


def test_the_template_kinds_and_narrowing_transforms_stay_open() -> None:
    from cruxible_client.contracts.procedures.authored import (
        CallStep,
        ClaimsStep,
        ExtractStep,
        InboxStep,
        ReadStep,
    )

    steps = (
        ReadStep.model_construct(),
        CallStep.model_construct(),
        ExtractStep.model_construct(),
        TransformStep.model_construct(transform="f", kind="filter"),
        TransformStep.model_construct(transform="d", kind="dedupe"),
        ClaimsStep.model_construct(),
        InboxStep.model_construct(),
    )
    assert [closed_step_kind(step) for step in steps] == [None] * len(steps)
    refuse_closed_steps(ProcedureSpec.model_construct(procedure="p", steps=steps))


def test_the_authoring_input_refuses_a_closed_step_offline() -> None:
    with pytest.raises(AuthoringInputError) as caught:
        lower_authoring_input(ProcedureInput(kind="procedure", spec=procedure_spec()))
    assert caught.value.code == CODE
    assert caught.value.field_path == "$.steps[0]"


def test_the_sdk_preview_names_the_closed_step() -> None:
    from tests.support.procedures import project

    procedure = Sequence("demo", input={}, output={"n": "int"})
    project(procedure, "summary", fields={"n": 1})
    preview = procedure.preview()
    assert not preview.ready_for_prepare
    assert [error.code for error in preview.errors] == ["procedure_step_closed"]


def test_the_sdk_has_no_builder_for_a_closed_step_kind() -> None:
    """R-ON4: the closed step kinds' builder methods are cut before 0.5.0."""

    import cruxible_client.authoring.procedures as builder

    for method in ("project", "guard", "halt", "invoke", "select", "repeat", "emit"):
        assert not hasattr(Sequence, method), method
    for helper in ("abort", "goto", "halt", "ShapeSpec", "JoinSpec", "DeriveSpec"):
        assert helper not in builder.__all__, helper
    with pytest.raises(ValueError):
        Sequence("demo").transform("shaped", kind="shape", spec={"fields": {}})  # type: ignore[arg-type]


def test_the_service_authoring_path_refuses_below_the_input(tmp_path) -> None:
    """A payload that skips the input check is refused by the core lowering, typed."""

    instance, _owner = initialize_local(tmp_path)
    before = instance.accepted_coordinate()
    spec = procedure_spec(procedure="assessment")
    result = AuthoringIntentCoordinator.for_instance(instance).compile(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=ProcedureAuthoringPayload(
            activation_policy="snapshot",
            definition={
                "name": spec.procedure,
                "procedure_spec": spec.model_dump(mode="json", by_alias=True),
            },
        ),
        canonical_timestamp="2026-08-21T12:00:00.000000Z",
    )
    assert result.verdict == "refused"
    (refusal,) = result.frontier.diagnostics
    assert refusal.code == CODE
    assert refusal.offending_element == "$.steps[0]"
    assert instance.accepted_coordinate() == before
