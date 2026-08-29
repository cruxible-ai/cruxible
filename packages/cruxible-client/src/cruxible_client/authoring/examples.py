"""Model-constructed decision-only examples for the point-of-use CLI surface."""

from __future__ import annotations

from typing import Callable, Final, Literal

from cruxible_client.authoring.inputs import (
    AuthoringInputV1,
    CarriedContractInput,
    ClaimInput,
    ExistingCaptureInput,
    LiteralObjectInput,
    ProcedureInput,
    SelfSourceInput,
    WorkingSelectionInput,
)
from cruxible_client.contracts.procedures.contract_schema import PropertySchema

AuthoringExampleName = Literal[
    "claim-existing-capture",
    "claim-flow-a",
    "claim-self-source",
    "procedure",
    "claim-adjudicate-contradicting-evidence",
    "claim-cite-supporting-evidence",
    "claim-adjudicate-unreviewed-evidence",
]


def claim_existing_capture_example() -> ClaimInput:
    return ClaimInput(
        kind="claim",
        subject="project.work_item/replace-me",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="replace-me"),
        role="observation",
        rationale="Replace with why the accepted Capture supports this statement.",
        source=ExistingCaptureInput(
            kind="existing_capture",
            capture_digest="sha256:" + "0" * 64,
        ),
        citation_role="evidence",
    )


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
            "description": "Read accepted state and return a bounded deterministic projection.",
            "contract_in": {
                "kind": "carried_contract",
                "name": "empty-input",
                "role": "contract-in",
            },
            "contract_out": {
                "kind": "carried_contract",
                "name": "query-result",
                "role": "contract-out",
            },
            "nodes": [
                {
                    "kind": "state_tap",
                    "node_id": "read",
                    "query": {
                        "kind": "accepted",
                        "role": "query",
                        "target": "QueryDefinition:replace-me",
                    },
                    "parameters": {},
                    "as": "query_rows",
                    "next": "normalize",
                },
                {
                    "kind": "transform",
                    "node_id": "normalize",
                    "transform_kind": "adapter",
                    "contract_in": {
                        "kind": "carried_contract",
                        "name": "query-result",
                        "role": "contract-in",
                    },
                    "contract_out": {
                        "kind": "carried_contract",
                        "name": "query-result",
                        "role": "contract-out",
                    },
                    "spec": "$steps.query_rows",
                    "as": "normalized",
                    "next": "project",
                },
                {
                    "kind": "project",
                    "node_id": "project",
                    "fields": {"rows": "$steps.normalized.rows"},
                    "contract_out": {
                        "kind": "carried_contract",
                        "name": "query-result",
                        "role": "contract-out",
                    },
                    "as": "result",
                },
            ],
            "returns": "result",
            "pin_slots": [],
            "budget": {
                "wall_clock": {"microseconds": 2_000_000},
                "max_provider_calls": 0,
                "max_capture_bytes": 0,
                "max_items": 100,
            },
            "hard_caps": {
                "max_wall_clock": {"microseconds": 4_000_000},
                "max_provider_calls": 0,
                "max_capture_bytes": 0,
                "max_items": 200,
                "max_repeat_attempts": 1,
            },
            "terminal_capability": 1,
        },
        activation_policy="snapshot",
        contracts=(
            CarriedContractInput(name="empty-input", fields={}),
            CarriedContractInput(
                name="query-result",
                fields={"rows": PropertySchema(type="json")},
            ),
        ),
    )


AUTHORING_EXAMPLE_FACTORIES: Final[dict[AuthoringExampleName, Callable[[], AuthoringInputV1]]] = {
    "claim-existing-capture": claim_existing_capture_example,
    "claim-flow-a": claim_flow_a_example,
    "claim-self-source": claim_self_source_example,
    "procedure": procedure_example,
}

_DOOR_EXAMPLES = {
    "claim-adjudicate-contradicting-evidence",
    "claim-cite-supporting-evidence",
    "claim-adjudicate-unreviewed-evidence",
}
AUTHORING_EXAMPLE_NAMES: Final[tuple[AuthoringExampleName, ...]] = (
    "claim-existing-capture",
    "claim-flow-a",
    "claim-self-source",
    "procedure",
    "claim-adjudicate-contradicting-evidence",
    "claim-cite-supporting-evidence",
    "claim-adjudicate-unreviewed-evidence",
)


def _door_example(
    name: AuthoringExampleName,
    *,
    claim_id: str,
    capture_digest: str,
) -> ClaimInput:
    rationale = {
        "claim-adjudicate-contradicting-evidence": (
            "Replace with the adjudication of this contradicting Capture."
        ),
        "claim-cite-supporting-evidence": (
            "Replace the statement fields, then cite this supporting Capture."
        ),
        "claim-adjudicate-unreviewed-evidence": (
            "Replace with the adjudication reached after reviewing this Capture."
        ),
    }[name]
    return ClaimInput(
        kind="claim",
        subject="project.work_item/replace-me",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="replace-me"),
        role="observation",
        rationale=rationale,
        source=ExistingCaptureInput(kind="existing_capture", capture_digest=capture_digest),
        citation_role="evidence",
        claim_id=claim_id,
    )


def authoring_example(
    name: AuthoringExampleName,
    *,
    claim_id: str | None = None,
    capture_digest: str | None = None,
) -> AuthoringInputV1:
    door = name in _DOOR_EXAMPLES
    if door:
        if claim_id is None or capture_digest is None:
            raise ValueError("attestation-door examples require claim_id and capture_digest")
        return _door_example(name, claim_id=claim_id, capture_digest=capture_digest)
    if claim_id is not None or capture_digest is not None:
        raise ValueError("claim_id/capture_digest apply only to attestation-door examples")
    return AUTHORING_EXAMPLE_FACTORIES[name]()


__all__ = [
    "AUTHORING_EXAMPLE_FACTORIES",
    "AUTHORING_EXAMPLE_NAMES",
    "AuthoringExampleName",
    "authoring_example",
    "claim_existing_capture_example",
    "claim_flow_a_example",
    "claim_self_source_example",
    "procedure_example",
]
