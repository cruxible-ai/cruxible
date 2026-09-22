"""Accepted-state query execution over governed QueryDefinitions.

This module supplies the one adapter the F2/F3 evaluator was missing: accepted
ledger state projected into the exact ``ClaimQueryFactsV1`` the engine reads.
The evidence assembly mirrors ``service_evaluate_playbill_claim_verdict`` so a
queried Claim and an explained Claim can never disagree about their verdict.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.accepted_attestations import parse_accepted_attestation
from cruxible_client.contracts.claim_attestations import ClaimAttestationV2
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifactAny,
    ClaimLawEvidenceAny,
    claim_artifact_digest,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.errors import (
    ClaimNotFoundError,
    ProjectionIntegrityError,
    ProposalIntegrityError,
)
from cruxible_client.contracts.query.definitions import AcceptedQueryDefinitionV1
from cruxible_client.contracts.query.grammar import QueryArtifactsEntryV2, QueryBudgetsV1
from cruxible_client.contracts.query.results import (
    ClaimQueryResultV1,
    QueryArtifactDefinitionV2,
    QueryExecutionReceiptV1,
)
from cruxible_client.contracts.subjects import AcceptedSubject, parse_subject, subject_digest
from cruxible_core.errors import DataValidationError
from cruxible_core.evidence.source_readers import ExternalSourceReaderProtocol
from cruxible_core.exhaust.records import (
    QUERY_RECEIPT_EVENT_KIND,
    QUERY_RECEIPT_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    StoredProcedureJournalRecordV1,
)
from cruxible_core.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.query.backends import ClaimFactRowV1, ClaimQueryFactsV1
from cruxible_core.query.engine import (
    evaluate_artifact_query,
    evaluate_claim_query,
    query_execution_receipt,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.discovery.query_definitions import accepted_query_definition
from cruxible_core.service.evidence.evidence import (
    ClaimReadHistoryIndex,
    ClaimReadSourceProtocol,
    ClaimVerdictReadContext,
    _claim_read_history_index,
    _current_replay_available,
    _referent_digests,
    _reproduced_claim_adjudication_rule,
    accepted_claim_attestations,
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


def _accepted_subjects(
    tree: Mapping[str, bytes], *, paths: tuple[str, ...] | None = None
) -> tuple[AcceptedSubject, ...]:
    return tuple(
        AcceptedSubject(
            path=path,
            shell=shell,
            artifact_digest=subject_digest(shell).tagged,
        )
        for path in sorted(tree if paths is None else paths, key=lambda item: item.encode("utf-8"))
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
    claim: ClaimArtifactAny,
    claim_types: dict[str, ClaimType],
    attestation_envelopes: tuple[ClaimAttestationV2, ...],
) -> ClaimFactRowV1:
    """Assemble one Claim's verdict inputs exactly as the verdict service does."""

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
    claim_type = claim_types.get(type_path)
    if claim_type is None:
        claim_type = parse_claim_type(type_content, path=type_path)
        claim_types[type_path] = claim_type
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
    attestations = accepted_claim_attestations(
        instance,
        coordinate=coordinate,
        tree=tree,
        claim=claim,
        historical=evidence.verified_attestations,
        envelopes=attestation_envelopes,
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


class _AcceptedQueryFactsRead:
    """Private facts shared by folds within one request at one coordinate.

    The tree and reader mapping are fixed for this read. Capture availability
    is sampled when a row is first assembled; no row survives into a later
    request. Consumers must not mutate the shared facts.
    """

    def __init__(
        self,
        instance: ClaimReadSourceProtocol,
        *,
        coordinate: AcceptedProjectionCoordinate,
        external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
        source_tree: Mapping[str, bytes] | None = None,
        predicates: tuple[str, ...] | None = None,
    ) -> None:
        self._instance = instance
        self._coordinate = coordinate
        self._readers = dict(external_readers or {})
        self._tree: Mapping[str, bytes] | None = None
        self._source_tree = source_tree
        self._predicates = predicates
        self._claim_paths: tuple[str, ...] = ()
        self._subject_paths: tuple[str, ...] = ()
        self._history: ClaimReadHistoryIndex | None = None
        self._attestations: dict[tuple[str, str], list[ClaimAttestationV2]] = {}
        self._claims: dict[str, ClaimArtifactAny] = {}
        self._claim_types: dict[str, ClaimType] = {}
        self._rows: dict[str, ClaimFactRowV1] = {}
        self._results: dict[bool, ClaimQueryFactsV1] = {}

    def live_claims(self) -> tuple[ClaimArtifactAny, ...]:
        """Return the source Claims already read by this request's live fact fold."""
        self.build()
        return tuple(
            self._claims[path]
            for path in sorted(self._claims)
            if self._claims[path].lifecycle.state == "live"
        )

    def build(self, *, include_retired: bool = False) -> ClaimQueryFactsV1:
        previous = self._results.get(include_retired)
        if previous is not None:
            return previous
        if self._tree is None:
            if isinstance(self._instance, PlaybillInstance):
                context = ClaimVerdictReadContext(self._instance, self._coordinate)
                self._tree = self._source_tree if self._source_tree is not None else context.tree
                with self._instance.bind_accepted_projection(self._coordinate) as projection:
                    if self._predicates is None:
                        self._claim_paths = tuple(
                            row.path for row in projection.typed.envelopes(kind="claim")
                        )
                    else:
                        self._claim_paths = tuple(
                            row[0]
                            for row in projection.typed.connection.execute(
                                "SELECT path FROM claims WHERE predicate IN ("
                                + ",".join("?" for _ in self._predicates)
                                + ") ORDER BY path",
                                self._predicates,
                            )
                        )
                    self._subject_paths = tuple(
                        row.path for row in projection.typed.envelopes(kind="subject")
                    )
                    for value in projection.typed.claim_attestations(
                        current_claims_only=True, claim_predicates=self._predicates
                    ):
                        self._attestations.setdefault(
                            (
                                value.statement.claim_identity.qualified,
                                value.statement.claim_artifact_digest,
                            ),
                            [],
                        ).append(value)
                    type_paths = tuple(
                        row[0]
                        for row in projection.typed.connection.execute(
                            "SELECT DISTINCT t.path FROM claims c "
                            "JOIN claim_types t ON t.identity=c.claim_type_identity"
                        )
                    )
                if self._source_tree is None:
                    context.prefetch(self._claim_paths + self._subject_paths + type_paths)
            else:
                # Cold candidate compilation has source bytes, without a served index.
                self._tree = self._instance.tree_at(self._coordinate.git_oid)
                self._claim_paths = tuple(p for p in self._tree if p.startswith(CLAIM_PATH_PREFIX))
                self._subject_paths = tuple(
                    p for p in self._tree if p.startswith(SUBJECT_PATH_PREFIX)
                )
                for path, content in self._tree.items():
                    if path.startswith("attestations/"):
                        value = parse_accepted_attestation(content, path=path)
                        self._attestations.setdefault(
                            (
                                value.statement.claim_identity.qualified,
                                value.statement.claim_artifact_digest,
                            ),
                            [],
                        ).append(value)
        tree = self._tree
        if self._history is None:
            self._history = _claim_read_history_index(self._instance, coordinate=self._coordinate)
        history = self._history

        def evidence_for(path: str) -> ClaimLawEvidenceAny:
            evidence = history.law_evidence.get(path)
            if evidence is None:
                raise ProposalIntegrityError(
                    "accepted Claim has no reproducible Claim law evidence"
                )
            return evidence

        rows: list[ClaimFactRowV1] = []
        for path in sorted(self._claim_paths, key=lambda item: item.encode("utf-8")):
            # The full-history path historically looked up evidence before
            # parsing the Claim; live-only reads parsed lifecycle first and
            # never demanded evidence for retired heads. Keep both orders.
            evidence = evidence_for(path) if include_retired else None
            claim = self._claims.get(path)
            if claim is None:
                claim = parse_claim(tree[path], path=path)
                self._claims[path] = claim
            if self._predicates is not None and claim.statement.predicate not in self._predicates:
                continue
            if not include_retired and claim.lifecycle.state != "live":
                continue
            row = self._rows.get(path)
            if row is None:
                row = _fact_row(
                    self._instance,
                    path=path,
                    tree=tree,
                    coordinate=self._coordinate,
                    readers=self._readers,
                    evidence=evidence if evidence is not None else evidence_for(path),
                    history=history,
                    claim=claim,
                    claim_types=self._claim_types,
                    attestation_envelopes=tuple(
                        self._attestations.get(
                            (claim.identity.qualified, claim_artifact_digest(claim).tagged), ()
                        )
                    ),
                )
                self._rows[path] = row
            rows.append(row)
        assembled = next(iter(self._results.values()), None)
        if assembled is None:
            providers = accepted_claim_providers(self._instance, coordinate=self._coordinate)
            subjects = _accepted_subjects(tree, paths=self._subject_paths)
            ordered_providers = tuple(
                providers[key] for key in sorted(providers, key=lambda item: item.encode("utf-8"))
            )
        else:
            subjects = assembled.subjects
            ordered_providers = assembled.providers
        result = ClaimQueryFactsV1(
            coordinate=self._coordinate,
            subjects=subjects,
            claims=tuple(rows),
            providers=ordered_providers,
        )
        self._results[include_retired] = result
        return result


def build_accepted_query_facts(
    instance: ClaimReadSourceProtocol,
    *,
    coordinate: AcceptedProjectionCoordinate,
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
    include_retired: bool = False,
    predicates: tuple[str, ...] | None = None,
) -> ClaimQueryFactsV1:
    """Project accepted ledger state into the facts one evaluation may read.

    Normal query evaluation admits only live Claims. Read-side lineage folds may
    opt into retired heads explicitly; the shared visibility path still judges
    verdicts rather than lifecycle, and callers remain responsible for limiting
    dependents to live rows. A served QueryDefinition supplies its complete
    referenced-predicate inventory so unrelated Claim and attestation bodies
    never enter the snapshot. General discovery callers leave it unrestricted.
    """

    return _AcceptedQueryFactsRead(
        instance, coordinate=coordinate, external_readers=external_readers, predicates=predicates
    ).build(include_retired=include_retired)


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


def evaluate_accepted_query(
    instance: PlaybillInstance,
    definition: AcceptedQueryDefinitionV1,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    parameters: Mapping[str, object] | None = None,
    budgets: QueryBudgetsV1 | None = None,
    facts: Callable[[], ClaimQueryFactsV1] | None = None,
    external_readers: Mapping[str, ExternalSourceReaderProtocol] | None = None,
) -> ClaimQueryResultV1:
    """One dispatch for public reads and projection currency, with lazy Claim facts."""
    if isinstance(definition.query.entry, QueryArtifactsEntryV2):

        def read(
            entry: QueryArtifactsEntryV2, limit: int
        ) -> tuple[int, tuple[QueryArtifactDefinitionV2, ...]]:
            with instance.bind_accepted_projection(coordinate) as projection:
                count, rows = projection.typed.query_artifact_definitions(
                    kind=entry.artifact_kind,
                    namespaces=entry.namespaces,
                    name_prefixes=entry.name_prefixes,
                    limit=limit,
                )
                definitions = []
                for row in rows:
                    source = projection.typed.source(row.identity)
                    if source is None:
                        raise ProjectionIntegrityError("selected definition source is absent")
                    definitions.append(
                        QueryArtifactDefinitionV2(
                            identity=row.identity,
                            path=row.path,
                            artifact_digest=row.artifact_digest,
                            definition=source,
                        )
                    )
                return count, tuple(definitions)

        return evaluate_artifact_query(
            definition,
            read=read,
            coordinate=coordinate,
            evaluation_time=evaluation_time,
            parameters=parameters,
            budgets=budgets,
        )
    return evaluate_claim_query(
        definition,
        facts=facts()
        if facts
        else build_accepted_query_facts(
            instance,
            coordinate=coordinate,
            external_readers=external_readers,
            predicates=definition.query.referenced_predicates,
        ),
        coordinate=coordinate,
        evaluation_time=evaluation_time,
        parameters=parameters,
        budgets=budgets,
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
    result = evaluate_accepted_query(
        instance,
        definition,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
        parameters=parameters,
        budgets=budgets,
        external_readers=external_readers,
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
