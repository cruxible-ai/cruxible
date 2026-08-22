"""Service orchestration for direct Procedure runs and exact exhaust promotion."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from cruxible_core.playbill.artifacts import ArtifactPin
from cruxible_core.playbill.canonical import CanonicalValue
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.errors import PlaybillExecutionError, PlaybillJournalError
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    ExhaustPromotionLawResultV1,
    ExhaustPromotionV1,
    ExhaustReducerProtocol,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    evaluate_exhaust_promotion_law,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.procedures.artifacts import AcceptedProcedureV1
from cruxible_core.playbill.procedures.contracts import OwnedProcedureContractValidator
from cruxible_core.playbill.procedures.execution import (
    ContractValidatorProtocol,
    PreparedProcedureRunV1,
    ProcedureActivationAuthorityProtocol,
    ProcedureExecutor,
    ProcedureRunResultV1,
    ProviderExecutorProtocol,
    StateTapReaderProtocol,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.grammar import QueryBudgetsV1
from cruxible_core.playbill.service.query_definitions import accepted_query_definition
from cruxible_core.service.playbill_query import service_run_playbill_query


class ExhaustReducerRegistry:
    """Closed digest-keyed runtime registry; callers cannot select by mutable label."""

    def __init__(self, reducers: Mapping[str, ExhaustReducerProtocol] | None = None) -> None:
        self._reducers = dict(reducers or {})
        for digest, reducer in self._reducers.items():
            if digest != reducer.reducer_digest:
                raise ValueError("ExhaustReducer registry key differs from reducer digest")

    def require(self, digest: str) -> ExhaustReducerProtocol:
        try:
            return self._reducers[digest]
        except KeyError as exc:
            raise PlaybillJournalError(f"pinned ExhaustReducer is unavailable: {digest}") from exc


class LocalExhaustPromotionVerifier:
    """Replay a promotion against one local portable journal and exact reducer bytes."""

    def __init__(
        self,
        *,
        instance_id: str,
        journal: LocalJournalBackend,
        bodies: ContentAddressedBodyStore,
        reducers: ExhaustReducerRegistry,
    ) -> None:
        self.instance_id = instance_id
        self.journal = journal
        self.bodies = bodies
        self.reducers = reducers

    def verify_promotion(
        self,
        promotion: ExhaustPromotionV1,
    ) -> ExhaustPromotionLawResultV1:
        try:
            stream = JournalStreamIdentityV1(
                instance_id=self.instance_id,
                journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
                stream_id=promotion.stream_id,
            )
            journal_range = self.journal.range_from_sequences(
                stream,
                promotion.partition_id,
                first_sequence=promotion.first_sequence,
                last_sequence=promotion.last_sequence,
            )
            records = self.journal.read_exact_range(journal_range)
            reducer = self.reducers.require(promotion.reducer_digest)
        except (PlaybillJournalError, ValueError) as exc:
            return ExhaustPromotionLawResultV1(
                verdict="refused",
                refusal_code="promotion.operational_basis_unavailable",
                message=str(exc),
            )
        return evaluate_exhaust_promotion_law(
            promotion,
            records=records,
            bodies=self.bodies,
            reducer=reducer,
        )


class PlaybillProcedureStateTapReader(StateTapReaderProtocol):
    """Execute one exact QueryDefinition pin at the admitted coordinate."""

    def __init__(
        self,
        *,
        instance: PlaybillInstance,
        evaluation_time: datetime,
        budgets: QueryBudgetsV1 | None = None,
    ) -> None:
        self.instance = instance
        self.evaluation_time = evaluation_time
        self.budgets = budgets

    def read_accepted_state(
        self,
        *,
        query: ArtifactPin,
        parameters: CanonicalValue,
        coordinate: AcceptedCoordinate,
    ) -> object:
        if query.target.kind != "QueryDefinition":
            raise PlaybillExecutionError("state tap pin must target QueryDefinition")
        if not isinstance(parameters, Mapping):
            raise PlaybillExecutionError("state tap query parameters must be an object")
        internal = self.instance.resolve_accepted_coordinate(
            git_oid=coordinate.git_oid,
            semantic_root=coordinate.semantic_root,
            generation_root=coordinate.generation_root,
            compiler_digest=coordinate.compiler_digest,
        )
        accepted = accepted_query_definition(
            self.instance,
            name=query.target.name,
            coordinate=internal,
        )
        if accepted.artifact_digest != query.artifact_digest:
            raise PlaybillExecutionError(
                "state tap QueryDefinition pin does not match the admitted coordinate"
            )
        run = service_run_playbill_query(
            self.instance,
            name=query.target.name,
            evaluation_time=self.evaluation_time,
            parameters=dict(parameters),
            at=coordinate,
            budgets=self.budgets,
        )
        if run.result.verdict != "completed":
            code = None if run.result.refusal is None else run.result.refusal.code
            raise PlaybillExecutionError(f"state tap query refused: {code or 'unknown'}")
        return run.result.model_dump(mode="json")


def service_execute_direct_procedure(
    prepared: PreparedProcedureRunV1,
    accepted: AcceptedProcedureV1,
    *,
    journal: LocalJournalBackend,
    bodies: ContentAddressedBodyStore,
    run_index_path: Path,
    fencing_token: str,
    activation_authority: ProcedureActivationAuthorityProtocol,
    contract_validator: ContractValidatorProtocol | None = None,
    provider_executor: ProviderExecutorProtocol | None = None,
) -> ProcedureRunResultV1:
    """Execute through the shared runtime; no transport duplicates orchestration."""

    validator = contract_validator or OwnedProcedureContractValidator(accepted)
    index = ProcedureRunIndex(run_index_path)
    try:
        return ProcedureExecutor(
            journal=journal,
            bodies=bodies,
            run_index=index,
            fencing_token=fencing_token,
            activation_authority=activation_authority,
            contract_validator=validator,
            provider_executor=provider_executor,
        ).execute(prepared, accepted)
    finally:
        index.close()


__all__ = [
    "ExhaustReducerRegistry",
    "LocalExhaustPromotionVerifier",
    "PlaybillProcedureStateTapReader",
    "service_execute_direct_procedure",
]
