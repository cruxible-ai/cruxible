"""Model-constructed authoring examples for the point-of-use CLI surface."""

from __future__ import annotations

import base64
import hashlib
from typing import Callable, Final, Literal

from cruxible_core.playbill.artifacts import ArtifactAuthority
from cruxible_core.playbill.authoring.models import (
    AuthoringClaimStatementV1,
    AuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ProcedureAuthoringPayloadV1,
    SelfSourceBodyV1,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_core.playbill.claims import LiteralClaimObject
from cruxible_core.playbill.knowledge_briefs import (
    KNOWLEDGE_BRIEF_PREDICATE,
    KnowledgeBriefValueV1,
)
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.subjects import subject_path

AuthoringExampleName = Literal[
    "claim-flow-a",
    "claim-self-source",
    "procedure",
    "brief",
]


def _subject() -> SemanticAddress:
    return SemanticAddress.whole_artifact(subject_path("project.work_item", "replace-me"))


def _claim_statement(*, predicate: str = "project.work_item.status") -> AuthoringClaimStatementV1:
    return AuthoringClaimStatementV1(
        subject=_subject(),
        predicate=predicate,
        object=LiteralClaimObject(value="replace-me"),
        role="observation",
    )


def claim_flow_a_example() -> ClaimAuthoringPayloadV1:
    selected = b"status: replace-me"
    digest = "sha256:" + hashlib.sha256(selected).hexdigest()
    return ClaimAuthoringPayloadV1(
        statement=_claim_statement(),
        rationale="Replace with why this source supports the statement.",
        source=WorkingSelectionObservationV1(
            source_id="repo.replace-me",
            coordinate=WorkingDigestCoordinateV1(
                source_content_digest=digest,
                source_byte_length=len(selected),
            ),
            selected_content_base64=base64.b64encode(selected).decode("ascii"),
            selected_bytes_digest=digest,
            selector=WorkingAnchorWindowV1(
                anchor="status: replace-me",
                start_byte=0,
                end_byte=len(selected),
                observed_occurrence_count=1,
            ),
        ),
        citation_role="evidence",
    )


def claim_self_source_example() -> ClaimAuthoringPayloadV1:
    return ClaimAuthoringPayloadV1(
        statement=_claim_statement(),
        rationale="Replace with why this new statement should be governed.",
        source=SelfSourceBodyV1(
            content_base64=base64.b64encode(b"status: replace-me\n").decode("ascii")
        ),
    )


def procedure_example() -> ProcedureAuthoringPayloadV1:
    return ProcedureAuthoringPayloadV1(
        definition={
            "name": "replace-me",
            "graph_format": 3,
            "nodes": [],
            "entry_node": "replace-me",
        },
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
        activation_policy="snapshot",
    )


def brief_example() -> ClaimAuthoringPayloadV1:
    value = KnowledgeBriefValueV1(
        purpose="Replace with the question this Brief answers.",
        kind="brief",
        prose="Replace with concise guidance and add governed references.",
    )
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=_subject(),
            predicate=KNOWLEDGE_BRIEF_PREDICATE,
            object=LiteralClaimObject(value=value.model_dump(mode="json")),
            role="normative",
        ),
        rationale="Replace with why this Brief should be governed.",
        source=SelfSourceBodyV1(
            content_base64=base64.b64encode(value.prose.encode("utf-8")).decode("ascii")
        ),
    )


AUTHORING_EXAMPLE_FACTORIES: Final[dict[AuthoringExampleName, Callable[[], AuthoringPayloadV1]]] = {
    "claim-flow-a": claim_flow_a_example,
    "claim-self-source": claim_self_source_example,
    "procedure": procedure_example,
    "brief": brief_example,
}


def authoring_example(name: AuthoringExampleName) -> AuthoringPayloadV1:
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
