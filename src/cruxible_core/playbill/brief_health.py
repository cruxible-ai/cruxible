"""One canonical, receipted health evaluator for knowledge.brief consumers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator, model_validator

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.claim_slots import (
    classify_claim_slot,
    classify_claim_slot_member,
)
from cruxible_core.playbill.claims import (
    LiteralClaimObject,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.errors import PlaybillError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.knowledge_briefs import (
    KNOWLEDGE_BRIEF_PREDICATE,
    KnowledgeBriefClaimRefV1,
    KnowledgeBriefQueryRefV1,
    KnowledgeBriefValueV1,
    parse_knowledge_brief_value,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.definitions import (
    parse_query_definition,
    query_definition_digest,
    query_definition_path,
)
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_query import service_run_playbill_query
from cruxible_core.temporal import ensure_utc, format_datetime

BRIEF_HEALTH_REQUEST_DOMAIN = "playbill-knowledge-brief-health-request-v1"
BRIEF_HEALTH_RESULT_DOMAIN = "playbill-knowledge-brief-health-result-v1"
BRIEF_HEALTH_RECEIPT_DOMAIN = "playbill-knowledge-brief-health-receipt-v1"
BRIEF_HEALTH_EVALUATOR_NAME = "knowledge.brief.health.v1"

BriefClaimRefState = Literal[
    "accepted_current",
    "superseded_semantically",
    "overturned",
    "retired",
    "conflicted",
    "refused",
    "unavailable_to_reader",
]
BriefQueryRefState = Literal["current", "superseded", "conflicted", "refused"]


class _StrictHealthModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KnowledgeBriefHealthBudgetsV1(_StrictHealthModel):
    max_depth: Literal[4] = 4
    max_direct_refs: Literal[80] = 80
    max_transitive_refs: Literal[512] = 512
    max_result_bytes: Literal[1048576] = 1048576


class KnowledgeBriefHealthRequestV1(_StrictHealthModel):
    tag: Literal["playbill-knowledge-brief-health-request-v1"] = (
        "playbill-knowledge-brief-health-request-v1"
    )
    brief_statement_digest: str
    accepted_coordinate: AcceptedCoordinate
    evaluation_time: datetime
    access_profile: CoverageAccessProfileV1
    budgets: KnowledgeBriefHealthBudgetsV1 = KnowledgeBriefHealthBudgetsV1()

    @field_validator("brief_statement_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_time(self, value: datetime) -> str:
        rendered = format_datetime(value)
        assert rendered is not None
        return rendered

    @property
    def request_digest(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        return typed_digest(Sha256Value, BRIEF_HEALTH_REQUEST_DOMAIN, payload).tagged


class KnowledgeBriefClaimRefStateV1(_StrictHealthModel):
    ref: KnowledgeBriefClaimRefV1
    state: BriefClaimRefState


class KnowledgeBriefQueryRefStateV1(_StrictHealthModel):
    ref: KnowledgeBriefQueryRefV1
    state: BriefQueryRefState


class KnowledgeBriefCycleRefusalV1(_StrictHealthModel):
    path: tuple[str, ...]

    @field_validator("path")
    @classmethod
    def _path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) < 2 or value[0] != value[-1]:
            raise ValueError("Brief cycle path must be complete and closed")
        return value


class KnowledgeBriefHealthResultV1(_StrictHealthModel):
    tag: Literal["playbill-knowledge-brief-health-result-v1"] = (
        "playbill-knowledge-brief-health-result-v1"
    )
    evaluator_name: Literal["knowledge.brief.health.v1"] = "knowledge.brief.health.v1"
    verdict: Literal["completed", "refused"]
    healthy: bool
    ref_states: tuple[KnowledgeBriefClaimRefStateV1, ...] = ()
    query_states: tuple[KnowledgeBriefQueryRefStateV1, ...] = ()
    cycle_refusal: KnowledgeBriefCycleRefusalV1 | None = None
    truncated: bool
    budgets: KnowledgeBriefHealthBudgetsV1
    request_digest: str
    result_digest: str

    @model_validator(mode="after")
    def _shape(self) -> "KnowledgeBriefHealthResultV1":
        if (self.verdict == "refused") != (self.cycle_refusal is not None):
            raise ValueError("Brief health refuses exactly when it names a cycle")
        if self.verdict == "refused" and (self.ref_states or self.query_states):
            raise ValueError("Brief cycle refusal never returns partial traversal")
        expected_healthy = (
            self.verdict == "completed"
            and not self.truncated
            and all(item.state == "accepted_current" for item in self.ref_states)
            and all(item.state == "current" for item in self.query_states)
        )
        if self.healthy != expected_healthy:
            raise ValueError("Brief health boolean disagrees with its complete state inventory")
        if self.result_digest != knowledge_brief_health_result_digest(self):
            raise ValueError("Brief health result digest does not reproduce")
        return self


def knowledge_brief_health_result_digest(result: KnowledgeBriefHealthResultV1) -> str:
    payload = result.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("result_digest")
    return typed_digest(Sha256Value, BRIEF_HEALTH_RESULT_DOMAIN, payload).tagged


def _build_result(**values: object) -> KnowledgeBriefHealthResultV1:
    provisional = KnowledgeBriefHealthResultV1.model_construct(
        **cast(dict[str, Any], values),
        result_digest="sha256:" + "0" * 64,
    )
    return KnowledgeBriefHealthResultV1.model_validate(
        {**values, "result_digest": knowledge_brief_health_result_digest(provisional)}
    )


class KnowledgeBriefHealthReceiptV1(_StrictHealthModel):
    tag: Literal["playbill-knowledge-brief-health-receipt-v1"] = (
        "playbill-knowledge-brief-health-receipt-v1"
    )
    evaluator_name: Literal["knowledge.brief.health.v1"] = "knowledge.brief.health.v1"
    request_digest: str
    result_digest: str
    receipt_digest: str

    @model_validator(mode="after")
    def _digest(self) -> "KnowledgeBriefHealthReceiptV1":
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        payload.pop("receipt_digest")
        expected = typed_digest(Sha256Value, BRIEF_HEALTH_RECEIPT_DOMAIN, payload).tagged
        if self.receipt_digest != expected:
            raise ValueError("Brief health receipt digest does not reproduce")
        return self


class KnowledgeBriefHealthEvaluationV1(_StrictHealthModel):
    result: KnowledgeBriefHealthResultV1
    receipt: KnowledgeBriefHealthReceiptV1


def _receipt(result: KnowledgeBriefHealthResultV1) -> KnowledgeBriefHealthReceiptV1:
    values: dict[str, object] = {
        "evaluator_name": "knowledge.brief.health.v1",
        "request_digest": result.request_digest,
        "result_digest": result.result_digest,
    }
    digest = typed_digest(Sha256Value, BRIEF_HEALTH_RECEIPT_DOMAIN, values).tagged
    return KnowledgeBriefHealthReceiptV1(
        evaluator_name="knowledge.brief.health.v1",
        request_digest=result.request_digest,
        result_digest=result.result_digest,
        receipt_digest=digest,
    )


def _briefs_by_digest(tree: Mapping[str, bytes]) -> dict[str, tuple[str, KnowledgeBriefValueV1]]:
    found: dict[str, tuple[str, KnowledgeBriefValueV1]] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if (
            claim.lifecycle.state != "live"
            or claim.statement.predicate != KNOWLEDGE_BRIEF_PREDICATE
            or not isinstance(claim.statement.object, LiteralClaimObject)
        ):
            continue
        digest = claim_statement_digest(claim.statement).tagged
        found[digest] = (
            claim.identity.name,
            parse_knowledge_brief_value(claim.statement.object.value),
        )
    return found


def _historical_briefs(
    instance: PlaybillInstance,
    *,
    through_oid: str,
) -> dict[str, tuple[str, KnowledgeBriefValueV1]]:
    found: dict[str, tuple[str, KnowledgeBriefValueV1]] = {}
    for generation in instance.accepted_history():
        found.update(_briefs_by_digest(instance.tree_at(generation.oid)))
        if generation.oid == through_oid:
            break
    return found


def _claim_ref_state(
    *,
    tree: Mapping[str, bytes],
    ref: KnowledgeBriefClaimRefV1,
) -> BriefClaimRefState:
    path = claim_path(ref.claim_id)
    content = tree.get(path)
    if content is None:
        return "refused"
    claim = parse_claim(content, path=path)
    if claim.lifecycle.state == "retired":
        return "retired"
    if claim_statement_digest(claim.statement).tagged != ref.statement_digest:
        return "superseded_semantically"
    if ref.expect.subject is not None and claim.statement.subject != ref.expect.subject:
        return "refused"
    if ref.expect.claim_type is not None and claim.statement.predicate != ref.expect.claim_type:
        return "refused"
    same_slot = []
    for contender_path in sorted(tree, key=lambda value: value.encode("utf-8")):
        if not contender_path.startswith("claims/"):
            continue
        contender = parse_claim(tree[contender_path], path=contender_path)
        if (
            contender.statement.subject == claim.statement.subject
            and contender.statement.predicate == claim.statement.predicate
            and contender.statement.qualifier == claim.statement.qualifier
        ):
            same_slot.append(contender)
    resolution = classify_claim_slot(same_slot)
    member_state = classify_claim_slot_member(resolution, claim.identity.qualified)
    return "refused" if member_state == "absent" else member_state


def _query_ref_state(
    instance: PlaybillInstance,
    *,
    tree: Mapping[str, bytes],
    coordinate: AcceptedCoordinate,
    evaluation_time: datetime,
    ref: KnowledgeBriefQueryRefV1,
) -> tuple[BriefQueryRefState, bool]:
    try:
        path = query_definition_path(ref.query_id)
    except ValueError:
        return "refused", False
    content = tree.get(path)
    if content is None:
        return "refused", False
    definition = parse_query_definition(content, path=path)
    if query_definition_digest(definition).tagged != ref.definition_digest:
        return "superseded", False
    try:
        run = service_run_playbill_query(
            instance,
            name=ref.query_id,
            evaluation_time=evaluation_time,
            parameters=ref.parameters,
            at=PlaybillAcceptedCoordinate.model_validate(coordinate.model_dump(mode="json")),
        )
    except (PlaybillError, ValueError):
        return "refused", False
    if run.result.verdict == "refused":
        return "refused", run.result.truncation.truncated
    if run.result.conflicts:
        return "conflicted", run.result.truncation.truncated
    return "current", run.result.truncation.truncated


def evaluate_knowledge_brief_health(
    instance: PlaybillInstance,
    request: KnowledgeBriefHealthRequestV1,
) -> KnowledgeBriefHealthEvaluationV1:
    coordinate = instance.resolve_accepted_coordinate(
        git_oid=request.accepted_coordinate.git_oid,
        semantic_root=request.accepted_coordinate.semantic_root,
        generation_root=request.accepted_coordinate.generation_root,
        compiler_digest=request.accepted_coordinate.compiler_digest,
    )
    tree = instance.tree_at(coordinate.git_oid)
    briefs = _briefs_by_digest(tree)
    historical_briefs = _historical_briefs(instance, through_oid=coordinate.git_oid)
    root = briefs.get(request.brief_statement_digest)
    if root is None:
        result = _build_result(
            evaluator_name=BRIEF_HEALTH_EVALUATOR_NAME,
            verdict="completed",
            healthy=False,
            ref_states=(),
            query_states=(),
            cycle_refusal=None,
            truncated=True,
            budgets=request.budgets,
            request_digest=request.request_digest,
        )
        return KnowledgeBriefHealthEvaluationV1(result=result, receipt=_receipt(result))

    ref_states: list[KnowledgeBriefClaimRefStateV1] = []
    query_states: list[KnowledgeBriefQueryRefStateV1] = []
    transitive_count = 0
    truncated = False
    cycle: tuple[str, ...] | None = None

    def visit(value: KnowledgeBriefValueV1, stack: tuple[str, ...]) -> None:
        nonlocal transitive_count, truncated, cycle
        if cycle is not None or truncated:
            return
        if len(stack) - 1 > request.budgets.max_depth:
            truncated = True
            return
        for claim_ref in value.claim_refs:
            transitive_count += 1
            if transitive_count > request.budgets.max_transitive_refs:
                truncated = True
                return
            claim_state = _claim_ref_state(
                tree=tree,
                ref=claim_ref,
            )
            ref_states.append(KnowledgeBriefClaimRefStateV1(ref=claim_ref, state=claim_state))
            nested = historical_briefs.get(claim_ref.statement_digest)
            if nested is not None:
                nested_id = nested[0]
                if nested_id in stack:
                    start = stack.index(nested_id)
                    cycle = (*stack[start:], nested_id)
                    return
                visit(nested[1], (*stack, nested_id))
        for query_ref in value.query_refs:
            transitive_count += 1
            if transitive_count > request.budgets.max_transitive_refs:
                truncated = True
                return
            query_state, query_truncated = _query_ref_state(
                instance,
                tree=tree,
                coordinate=request.accepted_coordinate,
                evaluation_time=request.evaluation_time,
                ref=query_ref,
            )
            truncated = truncated or query_truncated
            query_states.append(KnowledgeBriefQueryRefStateV1(ref=query_ref, state=query_state))

    visit(root[1], (root[0],))
    if cycle is not None:
        result = _build_result(
            evaluator_name=BRIEF_HEALTH_EVALUATOR_NAME,
            verdict="refused",
            healthy=False,
            ref_states=(),
            query_states=(),
            cycle_refusal=KnowledgeBriefCycleRefusalV1(path=cycle),
            truncated=False,
            budgets=request.budgets,
            request_digest=request.request_digest,
        )
        return KnowledgeBriefHealthEvaluationV1(result=result, receipt=_receipt(result))
    healthy = (
        not truncated
        and all(item.state == "accepted_current" for item in ref_states)
        and all(item.state == "current" for item in query_states)
    )
    result = _build_result(
        evaluator_name=BRIEF_HEALTH_EVALUATOR_NAME,
        verdict="completed",
        healthy=healthy,
        ref_states=tuple(ref_states),
        query_states=tuple(query_states),
        cycle_refusal=None,
        truncated=truncated,
        budgets=request.budgets,
        request_digest=request.request_digest,
    )
    if len(canonical_bytes(result.model_dump(mode="json"))) > request.budgets.max_result_bytes:
        result = _build_result(
            evaluator_name=BRIEF_HEALTH_EVALUATOR_NAME,
            verdict="completed",
            healthy=False,
            ref_states=(),
            query_states=(),
            cycle_refusal=None,
            truncated=True,
            budgets=request.budgets,
            request_digest=request.request_digest,
        )
    return KnowledgeBriefHealthEvaluationV1(result=result, receipt=_receipt(result))


class KnowledgeBriefHealthEvaluator:
    """Coordinate-bound memoizing facade shared by batch consumers."""

    def __init__(self, instance: PlaybillInstance) -> None:
        self._instance = instance
        self._memo: dict[str, KnowledgeBriefHealthEvaluationV1] = {}

    def evaluate(
        self,
        request: KnowledgeBriefHealthRequestV1,
    ) -> KnowledgeBriefHealthEvaluationV1:
        request_digest = request.request_digest
        cached = self._memo.get(request_digest)
        if cached is None:
            cached = evaluate_knowledge_brief_health(self._instance, request)
            self._memo[request_digest] = cached
        return cached


__all__ = [
    "BRIEF_HEALTH_EVALUATOR_NAME",
    "BRIEF_HEALTH_RECEIPT_DOMAIN",
    "BRIEF_HEALTH_REQUEST_DOMAIN",
    "BRIEF_HEALTH_RESULT_DOMAIN",
    "KnowledgeBriefClaimRefStateV1",
    "KnowledgeBriefCycleRefusalV1",
    "KnowledgeBriefHealthBudgetsV1",
    "KnowledgeBriefHealthEvaluationV1",
    "KnowledgeBriefHealthEvaluator",
    "KnowledgeBriefHealthReceiptV1",
    "KnowledgeBriefHealthRequestV1",
    "KnowledgeBriefHealthResultV1",
    "KnowledgeBriefQueryRefStateV1",
    "evaluate_knowledge_brief_health",
    "knowledge_brief_health_result_digest",
]
