"""Model-constructed decision-only examples for the point-of-use CLI surface."""

from __future__ import annotations

from typing import Callable, Final, Literal

from cruxible_core.playbill.artifacts import ArtifactAuthority
from cruxible_core.playbill.authoring.inputs import (
    AuthoringInputV1,
    BriefInput,
    ClaimInput,
    LiteralObjectInput,
    ProcedureInput,
    SelfSourceInput,
    WorkingSelectionInput,
)

AuthoringExampleName = Literal[
    "claim-flow-a",
    "claim-self-source",
    "procedure",
    "brief",
]


def claim_flow_a_example() -> ClaimInput:
    return ClaimInput(
        kind="claim",
        subject="project.work_item/replace-me",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="replace-me"),
        role="observation",
        rationale="Replace with why this source supports the statement.",
        source=WorkingSelectionInput(
            kind="working_selection",
            source_id="repo.replace-me",
        ),
        citation_role="evidence",
    )


def claim_self_source_example() -> ClaimInput:
    return ClaimInput(
        kind="claim",
        subject="project.work_item/replace-me",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="replace-me"),
        role="observation",
        rationale="Replace with why this new statement should be governed.",
        source=SelfSourceInput(kind="self_source", body="status: replace-me\n"),
    )


def procedure_example() -> ProcedureInput:
    return ProcedureInput(
        kind="procedure",
        definition={
            "graph_format": 3,
            "name": "replace-me",
            "contract_in": {"kind": "slot", "slot_name": "input-contract"},
            "contract_out": {"kind": "slot", "slot_name": "output-contract"},
            "nodes": [],
            "returns": "replace_me",
            "pin_slots": [],
        },
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
        activation_policy="snapshot",
    )


def brief_example() -> BriefInput:
    return BriefInput(
        kind="brief",
        subject="project.work_item/replace-me",
        purpose="Replace with the question this Brief answers.",
        brief_kind="brief",
        prose="Replace with concise guidance and add governed references.",
        rationale="Replace with why this Brief should be governed.",
    )


AUTHORING_EXAMPLE_FACTORIES: Final[dict[AuthoringExampleName, Callable[[], AuthoringInputV1]]] = {
    "claim-flow-a": claim_flow_a_example,
    "claim-self-source": claim_self_source_example,
    "procedure": procedure_example,
    "brief": brief_example,
}


def authoring_example(name: AuthoringExampleName) -> AuthoringInputV1:
    return AUTHORING_EXAMPLE_FACTORIES[name]()


__all__ = [
    "AUTHORING_EXAMPLE_FACTORIES",
    "AuthoringExampleName",
    "authoring_example",
    "brief_example",
    "claim_flow_a_example",
    "claim_self_source_example",
    "procedure_example",
]
