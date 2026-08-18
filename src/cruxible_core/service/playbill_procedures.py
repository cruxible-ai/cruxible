"""Service orchestration for direct Procedure runs and exact exhaust promotion."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.errors import PlaybillJournalError
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    ExhaustPromotionLawResultV1,
    ExhaustPromotionV1,
    ExhaustReducerProtocol,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    evaluate_exhaust_promotion_law,
)
from cruxible_core.playbill.procedures.artifacts import AcceptedProcedureV1
from cruxible_core.playbill.procedures.execution import (
    ContractValidatorProtocol,
    PreparedProcedureRunV1,
    ProcedureActivationAuthorityProtocol,
    ProcedureExecutor,
    ProcedureRunResultV1,
    ProviderExecutorProtocol,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex


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


def service_execute_direct_procedure(
    prepared: PreparedProcedureRunV1,
    accepted: AcceptedProcedureV1,
    *,
    journal: LocalJournalBackend,
    bodies: ContentAddressedBodyStore,
    run_index_path: Path,
    fencing_token: str,
    activation_authority: ProcedureActivationAuthorityProtocol,
    contract_validator: ContractValidatorProtocol,
    provider_executor: ProviderExecutorProtocol | None = None,
) -> ProcedureRunResultV1:
    """Execute through the shared runtime; no transport duplicates orchestration."""

    index = ProcedureRunIndex(run_index_path)
    try:
        return ProcedureExecutor(
            journal=journal,
            bodies=bodies,
            run_index=index,
            fencing_token=fencing_token,
            activation_authority=activation_authority,
            contract_validator=contract_validator,
            provider_executor=provider_executor,
        ).execute(prepared, accepted)
    finally:
        index.close()


__all__ = [
    "ExhaustReducerRegistry",
    "LocalExhaustPromotionVerifier",
    "service_execute_direct_procedure",
]
