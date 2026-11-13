"""Record reviewer judgment through the existing resolution law and incremental fold."""

from __future__ import annotations

from datetime import datetime

from cruxible_client.contracts.errors import JournalConflictError
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.dispositions import (
    ProcedureMeasurementReview,
    ProcedureResolutionDispositionRefusalCode,
    ProcedureResolutionDispositionRequest,
    ProcedureResolutionDispositionResult,
    ResolutionDispositionSummary,
)
from cruxible_client.contracts.repairs import served_repair_for_refusal
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.procedures.resolution import (
    ProcedureResolutionDispositionV1,
    ResolutionBookView,
    ResolutionContractActivationV1,
    append_resolution_disposition,
    build_resolution_disposition,
    resolution_book_at_head,
    resolution_contract_partition_id,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.procedures.measurements import (
    _accepted_procedure,
    _FencedWriter,
    _journal,
    _resolve_observation,
    measurement_activation_basis,
)
from cruxible_core.service.procedures.procedure_runs import ProcedureSurfaceError


class ResolutionDispositionRefused(ProcedureSurfaceError):
    def __init__(self, code: ProcedureResolutionDispositionRefusalCode, message: str) -> None:
        self.error_code = code
        self.http_status = 404 if code == "cruxible.resolution.unknown" else 409
        self.repair = served_repair_for_refusal(code)
        super().__init__(f"{code}: {message}")


def disposition_summary(value: ProcedureResolutionDispositionV1) -> ResolutionDispositionSummary:
    return ResolutionDispositionSummary(
        disposition_id=value.disposition_id,
        resolution_id=value.resolution_id,
        sequence=value.sequence,
        verdict=value.verdict,
        reviewer_id=value.reviewer_actor_context.actor_id,
        recorded_at=value.recorded_at,
        reason=value.note,
    )


def procedure_measurement_reviews(
    instance: PlaybillInstance,
    *,
    accepted: AcceptedProcedure,
    observation: AcceptedProjectionCoordinate,
    history_limit: int = 0,
) -> tuple[ProcedureMeasurementReview, ...]:
    """One lookup per declaration, plus at most history_limit judgments per declaration.

    The fold extends from journal suffixes; review history uses maintained
    sequence keys, never a scan of resolutions or earlier dispositions.
    """
    if not accepted.procedure.definition.measurements:
        return ()
    basis = measurement_activation_basis(instance, accepted=accepted, observation=observation)
    journal, stream = _journal(instance)
    rows = []
    for activation in basis.activations:
        book, _ = resolution_book_at_head(journal, stream, activation, bodies=instance.body_store())
        rows.append(measurement_review(book, activation, history_limit=history_limit))
    return tuple(rows)


def measurement_review(
    book: ResolutionBookView,
    activation: ResolutionContractActivationV1,
    *,
    history_limit: int = 0,
) -> ProcedureMeasurementReview:
    latest = book.latest_resolution(activation.contract_id)
    standing = book.latest_non_overturned(activation.contract_id)
    last = None if latest is None else book.last_disposition(latest.resolution_id)
    total = book.review_count(activation.contract_id)
    return ProcedureMeasurementReview(
        measurement_name=activation.measurement_name,
        contract_id=activation.contract_id,
        latest_resolution_id=None if latest is None else latest.resolution_id,
        standing_resolution_id=None if standing is None else standing.resolution_id,
        unavailable=bool(book.unavailable),
        last_disposition=None if last is None else disposition_summary(last),
        history=tuple(
            disposition_summary(item)
            for item in book.review_history(activation.contract_id, limit=history_limit)
        ),
        history_total=total,
        history_truncated=total > history_limit,
    )


def service_dispose_procedure_resolution(
    instance: PlaybillInstance,
    *,
    name: str,
    request: ProcedureResolutionDispositionRequest,
    actor_context: GovernedActorContext,
    recorded_at: datetime,
) -> ProcedureResolutionDispositionResult:
    instance.require_writable()
    observation = _resolve_observation(instance, request.at)
    # Review is an operator act against a live contract. Historical get remains
    # readable, but cannot add judgment to a retired/replaced Procedure revision.
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        current = projection.typed.dependency_state(f"Procedure:{name}")
    if current is None or current.lifecycle.state != "live":
        raise ResolutionDispositionRefused(
            "cruxible.resolution.contract_closed", "Procedure is not live."
        )
    accepted = _accepted_procedure(instance, name=name, coordinate=observation)
    if current.artifact_digest != accepted.artifact_digest:
        raise ResolutionDispositionRefused(
            "cruxible.resolution.contract_closed", "Procedure revision has been replaced."
        )
    basis = measurement_activation_basis(instance, accepted=accepted, observation=observation)
    journal, stream = _journal(instance)
    unavailable = False
    # This population is the selected Procedure's declarations, not the instance.
    for activation in basis.activations:
        book, _ = resolution_book_at_head(journal, stream, activation, bodies=instance.body_store())
        if book.unavailable:
            unavailable = True
            continue
        resolution = book.resolution_by_id(request.resolution_id)
        if resolution is None:
            continue
        fenced = _FencedWriter(instance, journal)
        try:
            fenced.acquire(stream, resolution_contract_partition_id(activation))
            # Lock order matches acceptance/Line admission: accepted state and
            # ledger activation, then the append's material/journal locks. The
            # writer fence above holds no journal lock across this section.
            with instance.holding_accepted_head() as main:
                live_coordinate = instance.accepted_coordinate()
                if live_coordinate.git_oid != main:
                    raise ResolutionDispositionRefused(
                        "cruxible.resolution.conflict",
                        "Accepted state advanced; refresh before reviewing again.",
                    )
                with instance.bind_accepted_projection(live_coordinate) as projection:
                    live = projection.typed.dependency_state(f"Procedure:{name}")
                if live is None or live.lifecycle.state != "live":
                    raise ResolutionDispositionRefused(
                        "cruxible.resolution.contract_closed", "Procedure is not live."
                    )
                if live.artifact_digest != accepted.artifact_digest:
                    raise ResolutionDispositionRefused(
                        "cruxible.resolution.contract_closed",
                        "Procedure revision has been replaced.",
                    )
                # Read after acquiring the fence, then CAS this exact decision head.
                book, head = resolution_book_at_head(
                    journal, stream, activation, bodies=instance.body_store()
                )
                if book.unavailable:
                    raise ResolutionDispositionRefused(
                        "cruxible.resolution.unavailable",
                        "Restore the unavailable resolution exhaust before reviewing it.",
                    )
                latest = book.latest_resolution(activation.contract_id)
                if latest is None or latest.resolution_id != request.resolution_id:
                    raise ResolutionDispositionRefused(
                        "cruxible.resolution.not_latest", "Review the contract's latest answer."
                    )
                previous = book.last_disposition(request.resolution_id)
                if previous is not None and previous.verdict == request.verdict:
                    reviewer = previous.reviewer_actor_context
                    if previous.note != request.reason or any(
                        getattr(reviewer, field) != getattr(actor_context, field)
                        for field in ("actor_type", "actor_id", "org_id")
                    ):
                        raise ResolutionDispositionRefused(
                            "cruxible.resolution.already_disposed",
                            "The last judgment has this verdict with a different reason "
                            "or reviewer.",
                        )
                    return ProcedureResolutionDispositionResult(
                        disposition=disposition_summary(previous),
                        standing_resolution_id=None
                        if request.verdict == "overturned"
                        else resolution.resolution_id,
                        written_now=False,
                    )
                disposition = build_resolution_disposition(
                    resolution,
                    sequence=book.disposition_count(request.resolution_id) + 1,
                    verdict=request.verdict,
                    reviewer_actor_context=actor_context,
                    recorded_at=recorded_at,
                    note=request.reason,
                )
                append_resolution_disposition(
                    fenced.writer,
                    activation=activation,
                    resolution=resolution,
                    disposition=disposition,
                    stream=stream,
                    expected_head=head,
                )
                return ProcedureResolutionDispositionResult(
                    disposition=disposition_summary(disposition),
                    standing_resolution_id=None
                    if request.verdict == "overturned"
                    else resolution.resolution_id,
                    written_now=True,
                )
        except JournalConflictError as exc:
            raise ResolutionDispositionRefused(
                "cruxible.resolution.conflict",
                "The answer changed during review; read it again before retrying.",
            ) from exc
        finally:
            fenced.release()
    if unavailable:
        raise ResolutionDispositionRefused(
            "cruxible.resolution.unavailable",
            "A measurement book is unavailable; restore it before reviewing an unresolved ID.",
        )
    raise ResolutionDispositionRefused(
        "cruxible.resolution.unknown",
        "Resolution ID is not in this Procedure revision; inspect its readings.",
    )
