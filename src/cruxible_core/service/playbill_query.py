"""Accepted-state query execution over governed QueryDefinitions.

This module supplies the one adapter the F2/F3 evaluator was missing: accepted
ledger state projected into the exact ``ClaimQueryFactsV1`` the engine reads.
The evidence assembly mirrors ``service_evaluate_playbill_claim_verdict`` so a
queried Claim and an explained Claim can never disagree about their verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.claim_attestations import VerifiedClaimAttestationV1
from cruxible_client.contracts.claim_types import (
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimLawEvidenceAny,
    claim_artifact_digest,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.errors import ClaimNotFoundError, ProposalIntegrityError
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_client.contracts.subjects import AcceptedSubject, parse_subject, subject_digest
from cruxible_core.errors import DataValidationError
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.exhaust.records import (
    QUERY_RECEIPT_EVENT_KIND,
    QUERY_RECEIPT_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    StoredProcedureJournalRecordV1,
)
from cruxible_core.playbill.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.query.backends import ClaimFactRowV1, ClaimQueryFactsV1
from cruxible_core.playbill.query.engine import (
    ClaimQueryResultV1,
    QueryExecutionReceiptV1,
    evaluate_claim_query,
    query_execution_receipt,
)
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.service.query_definitions import accepted_query_definition
from cruxible_core.playbill.source_readers import ExternalSourceReaderProtocol
from cruxible_core.service.playbill_evidence import (
    ClaimReadHistoryIndex,
    ClaimReadSourceProtocol,
    _claim_read_history_index,
    _current_replay_available,
    _referent_digests,
    _reproduced_claim_adjudication_rule,
    accepted_claim_providers,
)

CLAIM_PATH_PREFIX = "claims/"
SUBJECT_PATH_PREFIX = "subjects/"
DEFAULT_RECEIPT_STREAM_ID = "query-receipts"
DEFAULT_RECEIPT_PARTITION_ID = "default"


class _StrictQueryRunModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillQueryRunV1(_StrictQueryRunModel):
    """One executed query: its replayable result and its execution receipt."""

    tag: Literal["playbill-query-run-v1"] = "playbill-query-run-v1"
    coordinate: PlaybillAcceptedCoordinate
    name: str
    definition_path: str
    definition_digest: str
    result: ClaimQueryResultV1
    receipt: QueryExecutionReceiptV1
    journal_record_digest: str | None = None


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: PlaybillAcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def _accepted_subjects(tree: Mapping[str, bytes]) -> tuple[AcceptedSubject, ...]:
    return tuple(
        AcceptedSubject(
            path=path,
            shell=shell,
            artifact_digest=subject_digest(shell).tagged,
        )
        for path in sorted(tree, key=lambda item: item.encode("utf-8"))
        if path.startswith(SUBJECT_PATH_PREFIX)
        for shell in (parse_subject(tree[path], path=path),)
    )


def _fact_row(
    instance: ClaimReadSourceProtocol,
    *,
    path: str,
    tree: Mapping[str, bytes],
    coordinate: AcceptedProjectionCoordinate,
    readers: Mapping[str, ExternalSourceReaderProtocol],
    evidence: ClaimLawEvidenceAny,
    history: ClaimReadHistoryIndex,
) -> ClaimFactRowV1:
    """Assemble one Claim's verdict inputs exactly as the verdict service does."""

    claim = parse_claim(tree[path], path=path)
    accepted = AcceptedClaim(
        path=path,
        claim=claim,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        artifact_digest=claim_artifact_digest(claim).tagged,
    )
    # The caller supplies the accepted-history evidence from one shared index;
    # rebuilding it per row would make current-state folds quadratic in Claims.
    type_path = claim_type_path(claim.statement.predicate)
    type_content = tree.get(type_path)
    if type_content is None:
        raise ClaimNotFoundError(type_path)
    claim_type = parse_claim_type(type_content, path=type_path)
    rule = _reproduced_claim_adjudication_rule(
        claim_type=claim_type,
        evidence_digest=evidence.adjudication_rule_digest,
        history=history,
    )
    captures = tuple(
        item.model_copy(
            update={
                "current_replay_available": _current_replay_available(
                    instance,
                    item.capture_digest,
                    readers=readers,
                )
            }
        )
        for item in evidence.verdict_captures
    )
    subject_content_digest, object_content_digest = _referent_digests(tree, claim)
    referent_current = (
        claim.backing.referent_context.subject_content_digest == subject_content_digest
        and claim.backing.referent_context.object_content_digest == object_content_digest
    )
    attestations: tuple[VerifiedClaimAttestationV1, ...] = tuple(
        item.model_copy(
            update={
                "coverage": (
                    "exact_subject"
                    if item.statement.subject_content_digest == subject_content_digest
                    and item.statement.object_content_digest == object_content_digest
                    else "shell_stale"
                ),
                "current": item.statement.subject_content_digest == subject_content_digest
                and item.statement.object_content_digest == object_content_digest,
            }
        )
        for item in evidence.verified_attestations
    )
    return ClaimFactRowV1(
        accepted=accepted,
        rule=rule,
        captures=captures,
        attestations=attestations,
        referent_current=referent_current,
        # Authority is resolved from live mandate/resolution state by PC-E1's
        # resolver; acceptance-time verdict output is never carried forward.
        resolved_authority_basis=(),
    )


def build_accepted_query_facts(
    instance: ClaimReadSourceProtocol,
    *,
    coordinate: AcceptedProjectionCoordinate,
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
    include_retired: bool = False,
) -> ClaimQueryFactsV1:
    """Project accepted ledger state into the facts one evaluation may read.

    Normal query evaluation admits only live Claims. Read-side lineage folds may
    opt into retired heads explicitly; the shared visibility path still judges
    verdicts rather than lifecycle, and callers remain responsible for limiting
    dependents to live rows.
    """

    tree = instance.tree_at(coordinate.git_oid)
    readers = external_readers or {}
    history = _claim_read_history_index(instance, coordinate=coordinate)

    def evidence_for(path: str) -> ClaimLawEvidenceAny:
        evidence = history.law_evidence.get(path)
        if evidence is None:
            raise ProposalIntegrityError("accepted Claim has no reproducible Claim law evidence")
        return evidence

    claims = tuple(
        _fact_row(
            instance,
            path=path,
            tree=tree,
            coordinate=coordinate,
            readers=readers,
            evidence=evidence_for(path),
            history=history,
        )
        for path in sorted(tree, key=lambda item: item.encode("utf-8"))
        if path.startswith(CLAIM_PATH_PREFIX)
        and (include_retired or parse_claim(tree[path], path=path).lifecycle.state == "live")
    )
    providers = accepted_claim_providers(tree)
    return ClaimQueryFactsV1(
        coordinate=coordinate,
        subjects=_accepted_subjects(tree),
        claims=claims,
        providers=tuple(
            providers[key] for key in sorted(providers, key=lambda item: item.encode("utf-8"))
        ),
    )


class PlaybillQueryReceiptJournal:
    """Append query execution receipts to the registered query-receipt family.

    The journal backend is caller-owned exactly as it is for Procedure exhaust;
    a Playbill instance never opens one implicitly.
    """

    def __init__(
        self,
        *,
        writer: ProcedureExhaustWriter,
        instance_id: str,
        actor_context: GovernedActorContext,
        stream_id: str = DEFAULT_RECEIPT_STREAM_ID,
        partition_id: str = DEFAULT_RECEIPT_PARTITION_ID,
    ) -> None:
        self.writer = writer
        self.actor_context = actor_context
        self.partition_id = partition_id
        self.stream = JournalStreamIdentityV1(
            instance_id=instance_id,
            journal_family=QUERY_RECEIPT_JOURNAL_FAMILY,
            stream_id=stream_id,
        )

    def record(
        self,
        receipt: QueryExecutionReceiptV1,
        *,
        accepted_coordinate: AcceptedCoordinate,
        recorded_at: datetime,
    ) -> StoredProcedureJournalRecordV1:
        return self.writer.append(
            stream=self.stream,
            partition_id=self.partition_id,
            event_kind=QUERY_RECEIPT_EVENT_KIND,
            accepted_coordinate=accepted_coordinate,
            definition_digest=receipt.definition_digest,
            actor_context=self.actor_context,
            recorded_at=recorded_at,
            payload=receipt.model_dump(mode="json"),
        )


def service_run_playbill_query(
    instance: PlaybillInstance,
    *,
    name: str,
    evaluation_time: datetime,
    parameters: Mapping[str, object] | None = None,
    at: PlaybillAcceptedCoordinate | None = None,
    budgets: QueryBudgetsV1 | None = None,
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
    receipt_journal: PlaybillQueryReceiptJournal | None = None,
) -> PlaybillQueryRunV1:
    """Execute one accepted QueryDefinition at one accepted coordinate.

    The result and its receipt are a pure function of the definition digest, the
    resolved parameters, the accepted coordinate, and the explicit evaluation
    time. Supplying ``receipt_journal`` also records the receipt in the
    registered query-receipt journal family.
    """

    if evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise DataValidationError("query evaluation_time must be timezone-aware")
    coordinate = _resolve_coordinate(instance, at)
    definition = accepted_query_definition(instance, name=name, coordinate=coordinate)
    facts = build_accepted_query_facts(
        instance,
        coordinate=coordinate,
        external_readers=external_readers,
    )
    result = evaluate_claim_query(
        definition,
        facts=facts,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
        parameters=parameters,
        budgets=budgets,
    )
    receipt = query_execution_receipt(result)
    accepted = PlaybillAcceptedCoordinate.from_internal(coordinate)
    journal_record_digest: str | None = None
    if receipt_journal is not None:
        stored = receipt_journal.record(
            receipt,
            accepted_coordinate=accepted,
            recorded_at=evaluation_time,
        )
        journal_record_digest = stored.record_digest
    return PlaybillQueryRunV1(
        coordinate=accepted,
        name=name,
        definition_path=definition.path,
        definition_digest=definition.artifact_digest,
        result=result,
        receipt=receipt,
        journal_record_digest=journal_record_digest,
    )


__all__ = [
    "PlaybillQueryReceiptJournal",
    "PlaybillQueryRunV1",
    "build_accepted_query_facts",
    "service_run_playbill_query",
]
