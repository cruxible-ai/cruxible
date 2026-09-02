"""Service orchestration for direct Procedure runs and exact exhaust promotion."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_client.contracts.errors import PlaybillExecutionError, PlaybillJournalError
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.contracts import OwnedProcedureContractValidator
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_core.playbill.cas import ContentAddressedBodyStore
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
from cruxible_core.playbill.procedures.egress import EffectiveRungV1
from cruxible_core.playbill.procedures.execution import (
    ContractValidatorProtocol,
    PreparedProcedureRunV1,
    ProcedureActivationAuthorityProtocol,
    ProcedureClockProtocol,
    ProcedureExecutor,
    ProcedureRunResultV1,
    ProviderExecutorProtocol,
    ProviderRuntimeInvokerProtocol,
    StateTapReaderProtocol,
    StateTapReadResultV1,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.engine import ClaimQueryResultV1
from cruxible_core.playbill.service.query_definitions import accepted_query_definition
from cruxible_core.playbill.workspace_file import WorkspaceFileReader
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
    ) -> StateTapReadResultV1:
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
        return StateTapReadResultV1(
            value=state_tap_value(run.result),
            effective_budgets=run.result.budgets,
        )


def state_tap_value(result: ClaimQueryResultV1) -> dict[str, object]:
    """Render one query result as the governed value a state tap consumes.

    A tap consumes governed data, not advisories. `verdict_visibility` is
    accounting of the rows the policy declined to show, and it is kept out of
    the query's own result digest for exactly that reason; letting it into this
    value would put it straight back into the state-result digest that admits
    and replays the run, so which rows were hidden could change a run's
    identity.
    """

    value = result.model_dump(mode="json")
    value.pop("verdict_visibility", None)
    return value


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
    provider_runtime_invoker: ProviderRuntimeInvokerProtocol | None = None,
    provider_runtime_invoker_factory: Callable[[], ProviderRuntimeInvokerProtocol] | None = None,
    workspace_file_reader: WorkspaceFileReader | None = None,
    slot_pins: Mapping[str, ArtifactPin] | None = None,
    effective_rung: EffectiveRungV1 | None = None,
    clock: ProcedureClockProtocol | None = None,
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
            provider_runtime_invoker=provider_runtime_invoker,
            provider_runtime_invoker_factory=provider_runtime_invoker_factory,
            workspace_file_reader=workspace_file_reader,
            slot_pins=slot_pins,
            effective_rung=effective_rung,
            clock=clock,
        ).execute(prepared, accepted)
    finally:
        index.close()


__all__ = [
    "ExhaustReducerRegistry",
    "LocalExhaustPromotionVerifier",
    "PlaybillProcedureStateTapReader",
    "service_execute_direct_procedure",
]
