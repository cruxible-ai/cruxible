"""Drive curation detection in-process, as the curation.detect Trigger does in a daemon."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from cruxible_core.consumers.next.curation import CurationDetection, record_detection
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.service.discovery.curation import (
    PlaybillCurationListRequestV1,
    PlaybillCurationListResultV1,
    PlaybillCurationObserveRequestV1,
    run_playbill_curation_detection,
    service_list_playbill_curation,
    service_observe_playbill_curation_blocks,
)


def detect(instance: Any, *, evaluation_time: datetime) -> CurationDetection:
    """Run detection once at ``evaluation_time`` and keep it as the consumer would."""

    actor = GovernedActorContext(
        actor_type="system",
        actor_id="curation-detector",
        org_id=instance.descriptor.instance_id,
        operation_id="curation.detect:test",
        timestamp=evaluation_time,
    )
    generation, coverage = run_playbill_curation_detection(
        instance, evaluation_time=evaluation_time, actor_context=actor
    )
    detection = CurationDetection(
        generation=generation, detected_at=evaluation_time, coverage=coverage
    )
    record_detection(instance, sequence=0, detection=detection)
    return detection


def detect_and_list(
    instance: Any,
    *,
    request: PlaybillCurationListRequestV1 | dict[str, Any],
    evaluation_time: datetime,
    actor_context: GovernedActorContext | None = None,
    workspace_observation: Any = None,
) -> PlaybillCurationListResultV1:
    """Record a workspace scan (when given), run detection, then list: the old one-call road."""

    if workspace_observation is not None:
        assert actor_context is not None
        service_observe_playbill_curation_blocks(
            instance,
            request=PlaybillCurationObserveRequestV1(workspace_observation=workspace_observation),
            actor_context=actor_context,
        )
    detect(instance, evaluation_time=evaluation_time)
    parsed = (
        request
        if isinstance(request, PlaybillCurationListRequestV1)
        else PlaybillCurationListRequestV1.model_validate(request)
    )
    return service_list_playbill_curation(instance, request=parsed)


__all__ = ["detect", "detect_and_list"]
