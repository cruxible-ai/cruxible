"""Complete-vector, coordinate-binding preflight for AuthoringIntents."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal

from cruxible_core.playbill.authoring.lowering import (
    AuthoringLoweringError,
    LoweredAuthoring,
    lower_authoring,
)
from cruxible_core.playbill.authoring.models import (
    AUTHORING_CANDIDATE_TREE_DIGEST_DOMAIN,
    AUTHORING_INSTANCE_DESCRIPTOR_DIGEST_DOMAIN,
    AUTHORING_RESOLVED_DIGEST_DOMAIN,
    MAX_BLOCKED_CHECKS,
    MAX_DIAGNOSTICS,
    AuthoringDiagnosticV1,
    AuthoringIntentV1,
    BlockedCheckV1,
    CandidateStatusV1,
    ClaimAuthoringPayloadV1,
    DiagnosticFrontierLimitsV1,
    DiagnosticFrontierV1,
    PreflightResultV1,
    RepairAlternativeV1,
    WorkingSelectionObservationV1,
    build_preflight_certificate,
)
from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
from cruxible_core.playbill.errors import ProposalAdmissionError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    CandidateEvaluation,
    evaluate_proposal_tree,
    validate_proposal_tree,
)


@dataclass(frozen=True)
class ComputedPreflight:
    result: PreflightResultV1
    status: CandidateStatusV1
    lowered: LoweredAuthoring | None
    evaluated_tree: dict[str, bytes]
    evaluation: CandidateEvaluation | None


def _repair(
    kind: str,
    description: str,
    replacement: object | None = None,
) -> RepairAlternativeV1:
    return RepairAlternativeV1(
        kind=kind,
        description=description,
        replacement=replacement,
    )


def _diagnostic(
    *,
    code: str,
    stage: str,
    offending_element: str,
    message: str,
    owner: str = "writer",
    disposition: str = "edit_and_retry",
    repairs: tuple[RepairAlternativeV1, ...],
) -> AuthoringDiagnosticV1:
    return AuthoringDiagnosticV1(
        code=code,
        stage=stage,
        offending_element=offending_element,
        message=message,
        owner=owner,  # type: ignore[arg-type]
        disposition=disposition,  # type: ignore[arg-type]
        repairs=tuple(
            sorted(
                repairs,
                key=lambda item: canonical_bytes(item.model_dump(mode="json")),
            )
        ),
    )


def _compiler_diagnostic(item: CompilerDiagnostic) -> AuthoringDiagnosticV1:
    offending = item.subject.artifact_path if item.subject is not None else "payload"
    return _diagnostic(
        code=item.code,
        stage="proposal_evaluation",
        offending_element=offending,
        message=item.message,
        repairs=(
            _repair(
                "edit_authoring",
                "Edit the named semantic element and preflight this intent again.",
                {"offending_element": offending},
            ),
        ),
    )


def _ordered_diagnostics(
    diagnostics: list[AuthoringDiagnosticV1],
) -> tuple[AuthoringDiagnosticV1, ...]:
    by_key = {(item.stage, item.code, item.offending_element): item for item in diagnostics}
    return tuple(
        by_key[key]
        for key in sorted(
            by_key,
            key=lambda item: (item[0].encode(), item[1].encode(), item[2].encode()),
        )
    )


def _encoded_changes(
    *,
    base_tree: dict[str, bytes],
    candidate_tree: dict[str, bytes],
) -> list[dict[str, object]]:
    paths = sorted(
        {
            path
            for path in base_tree.keys() | candidate_tree.keys()
            if base_tree.get(path) != candidate_tree.get(path)
        },
        key=lambda item: item.encode("utf-8"),
    )
    return [
        {
            "content_base64": (
                None
                if path not in candidate_tree
                else base64.b64encode(candidate_tree[path]).decode("ascii")
            ),
            "path": path,
        }
        for path in paths
    ]


def compute_preflight(
    instance: PlaybillInstance,
    *,
    intent: AuthoringIntentV1,
    actor: AuthenticatedActor,
) -> ComputedPreflight:
    """Compute every independently knowable refusal and one submit-binding certificate."""

    diagnostics: list[AuthoringDiagnosticV1] = []
    blocked: list[BlockedCheckV1] = []
    payload = intent.payload
    if isinstance(payload, ClaimAuthoringPayloadV1):
        if payload.insertion_target is not None:
            diagnostics.append(
                _diagnostic(
                    code="playbill.authoring.insertion_target_not_supported",
                    stage="lowering",
                    offending_element="insertion_target",
                    message="Publication insertion is not supported in PC-G1b.",
                    owner="daemon",
                    disposition="wait",
                    repairs=(
                        _repair(
                            "omit_insertion_target",
                            "Omit insertion_target and keep the retained self-source citation.",
                            None,
                        ),
                        _repair(
                            "wait_for_pc_g2",
                            "Keep this intent pending until PC-G2 insertion is available.",
                        ),
                    ),
                )
            )
        if isinstance(payload.source, WorkingSelectionObservationV1):
            count = payload.source.selector.observed_occurrence_count
            if count != 1:
                diagnostics.append(
                    _diagnostic(
                        code="playbill.authoring.working_selection_ambiguous",
                        stage="source_binding",
                        offending_element="source.selector.observed_occurrence_count",
                        message=(
                            "The working-source anchor must occur exactly once; "
                            f"the client observed {count}."
                        ),
                        repairs=(
                            _repair(
                                "replace_anchor",
                                "Choose an anchor/window that occurs exactly once.",
                                {"required_occurrence_count": 1},
                            ),
                        ),
                    )
                )

    current = instance.accepted_coordinate()
    current_public = AcceptedCoordinate.from_internal(current)
    current_tree = instance.tree_at(current.git_oid)
    base = instance.resolve_accepted_coordinate(
        git_oid=intent.base_coordinate.git_oid,
        semantic_root=intent.base_coordinate.semantic_root,
        generation_root=intent.base_coordinate.generation_root,
        compiler_digest=intent.base_coordinate.compiler_digest,
    )
    base_tree = instance.tree_at(base.git_oid)
    service = instance.proposal_service()
    proposal_ref = f"refs/proposals/{actor.actor_id}/intent-{intent.intent_id[4:]}"
    proposal_ref_oid = service.transport.read_proposal_ref(proposal_ref)
    lowered: LoweredAuthoring | None = None
    evaluation: CandidateEvaluation | None = None
    evaluated_tree = current_tree
    resolved_payload: object

    try:
        lowered = lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
        try:
            proposed_tree = validate_proposal_tree(
                lowered.proposed_tree,
                limits=service.receive_limits,
                base_tree=base_tree,
            )
        except ProposalAdmissionError as exc:
            diagnostics.append(
                _diagnostic(
                    code="playbill.authoring.proposal_receive_refused",
                    stage="proposal_receive",
                    offending_element="payload",
                    message=str(exc),
                    repairs=(
                        _repair(
                            "reduce_authoring",
                            "Reduce the authored member count or byte size named by this refusal.",
                        ),
                    ),
                )
            )
            blocked.append(
                BlockedCheckV1(
                    check="proposal_evaluation",
                    blocked_by=("playbill.authoring.proposal_receive_refused",),
                    reason="The candidate tree must pass bounded receive before semantic laws run.",
                )
            )
            proposed_tree = lowered.proposed_tree
        else:
            evaluation = evaluate_proposal_tree(
                base_tree=base_tree,
                current_tree=current_tree,
                proposed_tree=proposed_tree,
                current=current,
                bodies=instance.body_store(),
                timestamp=intent.canonical_timestamp,
                rebased=base.git_oid != current.git_oid,
                actor_id=actor.actor_id,
                promotion_verifier=service.promotion_verifier,
            )
            evaluated_tree = evaluation.tree
            diagnostics.extend(_compiler_diagnostic(item) for item in evaluation.diagnostics)
        resolved_payload = lowered.resolved_authoring
    except AuthoringLoweringError as exc:
        diagnostics.append(
            _diagnostic(
                code=exc.code,
                stage="lowering",
                offending_element=exc.offending_element,
                message=exc.message,
                repairs=exc.repairs,
            )
        )
        blocked.append(
            BlockedCheckV1(
                check="proposal_evaluation",
                blocked_by=(exc.code,),
                reason="Artifact lowering must succeed before semantic laws can evaluate it.",
            )
        )
        resolved_payload = {
            "lowering_refusal": {
                "code": exc.code,
                "offending_element": exc.offending_element,
            },
            "semantic_identity": intent.semantic_identity,
        }

    ordered_diagnostics = _ordered_diagnostics(diagnostics)
    ordered_blocked = tuple(sorted(blocked, key=lambda item: item.check.encode()))
    frontier_complete = True
    if len(ordered_diagnostics) > MAX_DIAGNOSTICS or len(ordered_blocked) > MAX_BLOCKED_CHECKS:
        frontier_complete = False
        ordered_diagnostics = ordered_diagnostics[: MAX_DIAGNOSTICS - 1]
        budget = _diagnostic(
            code="playbill.authoring.diagnostic_budget_exhausted",
            stage="frontier",
            offending_element="payload",
            message="The bounded diagnostic frontier was exhausted.",
            owner="daemon",
            disposition="terminal",
            repairs=(),
        )
        ordered_diagnostics = _ordered_diagnostics([*ordered_diagnostics, budget])
        ordered_blocked = ordered_blocked[:MAX_BLOCKED_CHECKS]
    frontier = DiagnosticFrontierV1(
        diagnostics=ordered_diagnostics,
        blocked_checks=ordered_blocked,
        frontier_complete=frontier_complete,
    )
    candidate_tree_digest = typed_digest(
        Sha256Value,
        AUTHORING_CANDIDATE_TREE_DIGEST_DOMAIN,
        {
            "accepted_coordinate": current_public.model_dump(mode="json"),
            "changed_members": _encoded_changes(
                base_tree=current_tree,
                candidate_tree=evaluated_tree,
            ),
        },
    ).tagged
    resolved_authoring_digest = typed_digest(
        Sha256Value,
        AUTHORING_RESOLVED_DIGEST_DOMAIN,
        {
            "canonical_timestamp": intent.canonical_timestamp,
            "resolved_authoring": resolved_payload,
            "semantic_identity": intent.semantic_identity,
        },
    ).tagged
    descriptor_preimage = instance.descriptor.model_dump(mode="json")
    descriptor_preimage.pop("tag", None)
    descriptor_digest = typed_digest(
        Sha256Value,
        AUTHORING_INSTANCE_DESCRIPTOR_DIGEST_DOMAIN,
        descriptor_preimage,
    ).tagged
    certificate = build_preflight_certificate(
        instance_id=intent.instance_id,
        intent_id=intent.intent_id,
        intent_revision=intent.intent_revision,
        actor=actor,
        payload_digest=intent.payload_digest,
        resolved_authoring_digest=resolved_authoring_digest,
        accepted_coordinate=current_public,
        compiler_coordinate=current.compiler,
        instance_descriptor_digest=descriptor_digest,
        receive_limits=service.receive_limits,
        canonical_timestamp=intent.canonical_timestamp,
        proposal_ref=proposal_ref,
        proposal_ref_oid=proposal_ref_oid,
        candidate_tree_digest=candidate_tree_digest,
        frontier_digest=frontier.digest,
        frontier_limits=DiagnosticFrontierLimitsV1(),
    )
    verdict: Literal["passed", "refused"] = (
        "passed" if not frontier.diagnostics and not frontier.blocked_checks else "refused"
    )
    result = PreflightResultV1(
        verdict=verdict,
        certificate=certificate,
        frontier=frontier,
    )
    candidate_digest = None
    if evaluation is not None and evaluation.candidate is not None:
        candidate_digest = evaluation.candidate.candidate_digest
    status = CandidateStatusV1(
        state="ready_to_submit" if verdict == "passed" else "preflight_refused",
        candidate_digest=candidate_digest,
        current_accepted_coordinate=current_public,
    )
    return ComputedPreflight(
        result=result,
        status=status,
        lowered=lowered,
        evaluated_tree=evaluated_tree,
        evaluation=evaluation,
    )


__all__ = ["ComputedPreflight", "compute_preflight"]
