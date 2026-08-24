"""Read-only consumers for the canonical knowledge.brief health evaluator."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    LiteralClaimObject,
    claim_statement_digest,
)
from cruxible_client.contracts.knowledge_briefs import (
    KNOWLEDGE_BRIEF_PREDICATE,
    parse_knowledge_brief_value,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.brief_health import (
    KnowledgeBriefHealthEvaluationV1,
    KnowledgeBriefHealthEvaluator,
    KnowledgeBriefHealthRequestV1,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    service_list_playbill_claims,
)


class _StrictBriefServiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillBriefHealthEntryV1(_StrictBriefServiceModel):
    tag: Literal["playbill-brief-health-entry-v1"] = "playbill-brief-health-entry-v1"
    identity: str
    statement_digest: str
    subject: SemanticAddress
    purpose: str
    health: KnowledgeBriefHealthEvaluationV1


class PlaybillBriefReauthorQueueV1(_StrictBriefServiceModel):
    tag: Literal["playbill-brief-reauthor-queue-v1"] = "playbill-brief-reauthor-queue-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    entries: tuple[PlaybillBriefHealthEntryV1, ...]


def service_list_playbill_brief_reauthor_queue(
    instance: PlaybillInstance,
    *,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfileV1,
    at: PlaybillAcceptedCoordinate | None = None,
) -> PlaybillBriefReauthorQueueV1:
    """Derive the unhealthy Brief queue without storing workflow state on a Brief."""

    listed = service_list_playbill_claims(
        instance,
        at=at,
        predicate=KNOWLEDGE_BRIEF_PREDICATE,
    )
    evaluator = KnowledgeBriefHealthEvaluator(instance)
    entries: list[PlaybillBriefHealthEntryV1] = []
    for view in listed.claims:
        claim = _claim_from_view(view)
        if (
            not isinstance(claim, ClaimArtifactV2)
            or claim.lifecycle.state != "live"
            or not isinstance(claim.statement.object, LiteralClaimObject)
        ):
            continue
        value = parse_knowledge_brief_value(claim.statement.object.value)
        statement_digest = claim_statement_digest(claim.statement).tagged
        health = evaluator.evaluate(
            KnowledgeBriefHealthRequestV1(
                brief_statement_digest=statement_digest,
                accepted_coordinate=AcceptedCoordinate.model_validate(
                    listed.coordinate.model_dump(mode="json")
                ),
                evaluation_time=evaluation_time,
                access_profile=access_profile,
            )
        )
        if not health.result.healthy:
            entries.append(
                PlaybillBriefHealthEntryV1(
                    identity=claim.identity.name,
                    statement_digest=statement_digest,
                    subject=claim.statement.subject,
                    purpose=value.purpose,
                    health=health,
                )
            )
    return PlaybillBriefReauthorQueueV1(
        coordinate=listed.coordinate,
        evaluation_time=evaluation_time,
        entries=tuple(sorted(entries, key=lambda item: item.identity.encode("utf-8"))),
    )


__all__ = [
    "PlaybillBriefHealthEntryV1",
    "PlaybillBriefReauthorQueueV1",
    "service_list_playbill_brief_reauthor_queue",
]
