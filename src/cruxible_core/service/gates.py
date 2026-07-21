"""Atomic evaluation and durable receipts for declared outbound gates."""

from __future__ import annotations

from typing import Any

from cruxible_core.config.property_validation import entity_with_identity_properties
from cruxible_core.config.schema import CoreConfig, GateSchema
from cruxible_core.errors import CoreError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.query.entity_state import entity_matches_query_state
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service.types import (
    GateCandidateOutcome,
    GateEvaluationResult,
    GateEvaluationVerdict,
)

_READ_REVISION_STATE_KEY = "read_revision"


def _failure_reason(prefix: str, exc: Exception) -> str:
    """Keep domain detail while preventing unexpected internals from crossing HTTP."""
    if isinstance(exc, CoreError):
        return f"{prefix}: {exc.__class__.__name__}: {exc}"
    return f"{prefix}: {exc.__class__.__name__}"


def _gate_resolution_error(config: CoreConfig, gate_name: str) -> str | None:
    if not config.gates:
        return (
            "the active instance config declares no gates element "
            "(or the server predates gates support); a gate that silently "
            "passes when unconfigured is forbidden. Declare gates: in the "
            "instance config, then reload."
        )
    if gate_name not in config.gates:
        declared = ", ".join(sorted(config.gates))
        return f"no gate named '{gate_name}' is declared. Declared gates: {declared}"
    return None


def _satisfying_entity_ids(
    config: CoreConfig,
    graph: EntityGraph,
    gate: GateSchema,
    candidate: str,
) -> list[str]:
    """Return every live entity that pins one candidate, in stable ID order."""
    matches: list[str] = []
    expected = {gate.match_property: candidate, **gate.condition}
    for entity in graph.list_entities(gate.entity_type):
        if not entity_matches_query_state(entity.metadata, "live"):
            continue
        properties = entity_with_identity_properties(config, entity).properties
        if all(properties.get(property_name) == value for property_name, value in expected.items()):
            matches.append(entity.entity_id)
    return sorted(matches)


def _receipt_parameters(
    *,
    instance_id: str,
    read_revision: int,
    gate_name: str,
    kind: str | None,
    candidates: list[str],
    outcomes: list[GateCandidateOutcome],
    verdict: GateEvaluationVerdict,
    reason: str | None,
) -> dict[str, Any]:
    # The durable read coordinate is deliberately explicit. When
    # oq-revision-epoch-lineage lands in 0.3, this upgrades from
    # (instance_id, revision) to (instance_id, epoch, revision); do not infer
    # or invent an epoch before then.
    return {
        "instance_id": instance_id,
        "read_revision": read_revision,
        "gate_name": gate_name,
        "kind": kind,
        "candidates": list(candidates),
        "candidate_outcomes": [
            {
                "candidate": outcome.candidate,
                "satisfied": outcome.satisfied,
                "satisfying_entity_ids": list(outcome.satisfying_entity_ids),
            }
            for outcome in outcomes
        ],
        "verdict": verdict,
        "reason": reason,
    }


def service_evaluate_gate(
    instance: InstanceProtocol,
    *,
    instance_id: str,
    gate_name: str,
    candidates: list[str],
    error_reason: str | None = None,
) -> GateEvaluationResult:
    """Evaluate one gate invocation and persist exactly one composite receipt.

    Candidate sourcing belongs to callers. Evaluation, read-revision capture,
    and receipt persistence share one instance transaction. SQLite's
    ``BEGIN IMMEDIATE`` therefore holds a single state image across every
    candidate and commits the audit-only receipt without advancing the revision.

    ``error_reason`` records a caller-side refusal (for example malformed
    pre-push input) without pretending an evaluation took place.
    """
    normalized_candidates = list(candidates)
    gate: GateSchema | None = None
    preflight_error: str | None = error_reason
    if error_reason is not None and not error_reason.strip():
        preflight_error = "gate evaluation refusal reason must be non-empty"
    try:
        config = instance.load_config()
    except Exception as exc:
        config = None
        if preflight_error is None:
            preflight_error = _failure_reason("failed to load gate declarations", exc)
    else:
        assert config is not None
        resolution_error = _gate_resolution_error(config, gate_name)
        if resolution_error is not None and preflight_error is None:
            preflight_error = resolution_error
        gate = config.gates.get(gate_name)

    kind = gate.kind if gate is not None else None
    if preflight_error is None and any(
        not isinstance(candidate, str) or not candidate.strip()
        for candidate in normalized_candidates
    ):
        preflight_error = "candidate values must be non-empty strings"
    if preflight_error is None and gate is not None and gate.kind == "generic":
        if not normalized_candidates:
            preflight_error = (
                "generic gate received no candidate values; pass --candidate VALUE "
                "(repeatable) or pipe one candidate per line"
            )

    with instance.write_transaction() as uow:
        revision_value = uow.snapshots.get_instance_state(_READ_REVISION_STATE_KEY)
        read_revision = int(revision_value) if isinstance(revision_value, int) else 0
        outcomes: list[GateCandidateOutcome] = []
        reason = preflight_error

        if reason is None:
            assert config is not None
            assert gate is not None
            try:
                graph = uow.graph.load_graph()
                for candidate in normalized_candidates:
                    satisfying_ids = _satisfying_entity_ids(
                        config,
                        graph,
                        gate,
                        candidate,
                    )
                    outcomes.append(
                        GateCandidateOutcome(
                            candidate=candidate,
                            satisfied=bool(satisfying_ids),
                            satisfying_entity_ids=satisfying_ids,
                        )
                    )
            except Exception as exc:
                reason = _failure_reason("gate evaluation failed", exc)

        if reason is not None:
            verdict: GateEvaluationVerdict = "error"
        elif all(outcome.satisfied for outcome in outcomes):
            verdict = "satisfied"
        else:
            verdict = "unsatisfied"

        parameters = _receipt_parameters(
            instance_id=instance_id,
            read_revision=read_revision,
            gate_name=gate_name,
            kind=kind,
            candidates=normalized_candidates,
            outcomes=outcomes,
            verdict=verdict,
            reason=reason,
        )
        builder = ReceiptBuilder(
            query_name=gate_name,
            parameters=parameters,
            operation_type="gate_evaluation",
            head_snapshot_id=instance.get_head_snapshot_id(),
            root_detail={"verdict": verdict, "reason": reason},
        )
        parent_ids: list[str] = []
        if verdict == "error":
            parent_ids.append(
                builder.record_validation(
                    passed=False,
                    detail={"verdict": verdict, "reason": reason},
                )
            )
        else:
            assert gate is not None
            for outcome in outcomes:
                outcome_payload = {
                    "candidate": outcome.candidate,
                    "satisfied": outcome.satisfied,
                    "satisfying_entity_ids": list(outcome.satisfying_entity_ids),
                }
                validation_id = builder.record_validation(
                    passed=outcome.satisfied,
                    detail=outcome_payload,
                )
                parent_ids.append(validation_id)
                for entity_id in outcome.satisfying_entity_ids:
                    builder.record_entity_lookup(
                        gate.entity_type,
                        entity_id,
                        parent_id=validation_id,
                    )
        result_payloads = parameters["candidate_outcomes"]
        builder.record_results(result_payloads, parent_ids=parent_ids or None)
        builder.mark_committed()
        receipt = builder.build(results=result_payloads)
        uow.receipts.save_receipt(receipt)

    return GateEvaluationResult(
        gate_name=gate_name,
        kind=kind,
        candidates=normalized_candidates,
        candidate_outcomes=outcomes,
        verdict=verdict,
        reason=reason,
        instance_id=instance_id,
        read_revision=read_revision,
        receipt_id=receipt.receipt_id,
        receipt=receipt,
    )
