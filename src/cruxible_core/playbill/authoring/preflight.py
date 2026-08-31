"""Complete-vector, coordinate-binding preflight for AuthoringIntents."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Literal

from cruxible_client.contracts.authoring.models import (
    AUTHORING_CANDIDATE_TREE_DIGEST_DOMAIN,
    AUTHORING_INSTANCE_DESCRIPTOR_DIGEST_DOMAIN,
    AUTHORING_RESOLVED_DIGEST_DOMAIN,
    MAX_BLOCKED_CHECKS,
    MAX_DIAGNOSTICS,
    AuthoringDiagnosticV1,
    AuthoringIntentV1,
    AuthoringIntentV2,
    AuthoringReferenceExpectationV1,
    AuthoringReferenceSuccessorV1,
    BlockedCheckV1,
    CandidateStatusV1,
    ClaimAuthoringPayloadV1,
    DiagnosticFrontierLimitsV1,
    DiagnosticFrontierV1,
    PreflightResultV1,
    RepairAlternativeV1,
    SelfSourceBodyV1,
    build_preflight_certificate,
)
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    parse_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillError, ProposalAdmissionError
from cruxible_client.contracts.procedures.artifacts import (
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.query.definitions import (
    parse_query_definition,
    query_definition_digest,
    query_definition_path,
)
from cruxible_client.contracts.subjects import parse_subject, subject_digest, subject_path
from cruxible_core.playbill.authoring.lowering import (
    AuthoringLoweringError,
    LoweredAuthoring,
    lower_authoring,
)
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


_PAYLOAD_PATH_PART_RE = re.compile(r"([^.\[\]]+)|\[([0-9]+)\]")


def _reference_artifact_path(expectation: AuthoringReferenceExpectationV1) -> str:
    if expectation.artifact_kind == "Subject":
        kind, separator, identifier = expectation.address.partition("/")
        if not separator:
            raise ValueError("Subject ref must use <kind>/<id>")
        return subject_path(kind, identifier)
    if expectation.artifact_kind == "ClaimType":
        return claim_type_path(expectation.address)
    if expectation.artifact_kind == "Claim":
        return claim_path(expectation.address)
    if expectation.artifact_kind == "Procedure":
        return procedure_path(expectation.address)
    if expectation.artifact_kind == "QueryDefinition":
        return query_definition_path(expectation.address)
    # A Source ref carries the accepted ledger artifact path, not a workstation path.
    if expectation.address.startswith("/") or ".." in expectation.address.split("/"):
        raise ValueError("Source ref must use a canonical ledger artifact path")
    return expectation.address


def _reference_artifact_digest(
    expectation: AuthoringReferenceExpectationV1,
    *,
    path: str,
    content: bytes,
) -> str:
    if expectation.artifact_kind == "Subject":
        return subject_digest(parse_subject(content, path=path)).tagged
    if expectation.artifact_kind == "ClaimType":
        return claim_type_digest(parse_claim_type(content, path=path)).tagged
    if expectation.artifact_kind == "Claim":
        return claim_artifact_digest(parse_claim(content, path=path)).tagged
    if expectation.artifact_kind == "Procedure":
        return procedure_artifact_digest(parse_procedure(content, path=path)).tagged
    if expectation.artifact_kind == "QueryDefinition":
        return query_definition_digest(parse_query_definition(content, path=path)).tagged
    return typed_digest(
        Sha256Value,
        "playbill-authoring-source-reference-v1",
        {"content": content.hex(), "path": path},
    ).tagged


def _payload_path_value(payload: object, path: str) -> object:
    current = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    offset = 0
    for match in _PAYLOAD_PATH_PART_RE.finditer(path):
        if match.start() != offset and path[offset : match.start()] != ".":
            raise KeyError(path)
        key, index = match.groups()
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                raise KeyError(path)
            current = current[key]
        else:
            if not isinstance(current, list):
                raise KeyError(path)
            current = current[int(index)]
        offset = match.end()
    if offset != len(path):
        raise KeyError(path)
    return current


def _payload_value_matches_reference(
    value: object,
    *,
    expectation: AuthoringReferenceExpectationV1,
    artifact_path: str,
) -> bool:
    if isinstance(value, str):
        return value in {
            expectation.address,
            artifact_path,
            f"{expectation.artifact_kind}:{expectation.address}",
        }
    if isinstance(value, dict):
        return any(
            value.get(key) in {expectation.address, artifact_path}
            for key in ("address", "artifact_path", "name")
        )
    return False


def _existing_capture_contract_matches_reference(
    instance: PlaybillInstance,
    *,
    value: object,
    artifact_path: str,
    artifact_content: bytes,
) -> bool:
    """Bind an existing-Capture payload ref to its exact accepted contract bytes."""

    if not (
        isinstance(value, dict)
        and value.get("tag") == "playbill-existing-capture-citation-source-v1"
        and isinstance(value.get("capture_digest"), str)
    ):
        return False
    try:
        envelope = parse_capture_envelope(
            instance.body_store().read(
                value["capture_digest"],
                access=BodyAccessContext(
                    principal_id="playbill-authoring",
                    can_read_body=True,
                ),
            )
        )
        contract = parse_capture_contract(artifact_content, path=artifact_path)
    except (PlaybillError, ValueError):
        return False
    return capture_contract_digest(contract).tagged == envelope.capture_contract_digest


def _reference_diagnostics(
    instance: PlaybillInstance,
    *,
    intent: AuthoringIntentV1,
    base_tree: dict[str, bytes],
) -> tuple[AuthoringDiagnosticV1, ...]:
    if not isinstance(intent, AuthoringIntentV2):
        return ()
    diagnostics: list[AuthoringDiagnosticV1] = []
    for expectation in intent.reference_expectations:
        try:
            path = _reference_artifact_path(expectation)
            value = _payload_path_value(intent.payload, expectation.payload_path)
        except (KeyError, TypeError, ValueError):
            diagnostics.append(
                _diagnostic(
                    code="playbill.authoring.reference_payload_mismatch",
                    stage="reference_assertion",
                    offending_element=expectation.payload_path,
                    message="The reference assertion does not describe its emitted payload path.",
                    repairs=(
                        _repair(
                            "replace_reference",
                            "Replace the ref at the named builder expression.",
                            {
                                "address": expectation.address,
                                "artifact_kind": expectation.artifact_kind,
                            },
                        ),
                    ),
                )
            )
            continue
        existing_capture_contract_ref = (
            expectation.artifact_kind == "Source"
            and isinstance(value, dict)
            and value.get("tag") == "playbill-existing-capture-citation-source-v1"
        )
        if not existing_capture_contract_ref and not _payload_value_matches_reference(
            value,
            expectation=expectation,
            artifact_path=path,
        ):
            diagnostics.append(
                _diagnostic(
                    code="playbill.authoring.reference_payload_mismatch",
                    stage="reference_assertion",
                    offending_element=expectation.payload_path,
                    message="The emitted value differs from the asserted typed reference.",
                    repairs=(
                        _repair(
                            "replace_reference",
                            "Replace the ref at the named builder expression.",
                            {
                                "address": expectation.address,
                                "artifact_kind": expectation.artifact_kind,
                            },
                        ),
                    ),
                )
            )
            continue
        try:
            minted = instance.resolve_accepted_coordinate(
                git_oid=expectation.minted_coordinate.git_oid,
                semantic_root=expectation.minted_coordinate.semantic_root,
                generation_root=expectation.minted_coordinate.generation_root,
                compiler_digest=expectation.minted_coordinate.compiler_digest,
            )
            minted_tree = instance.tree_at(minted.git_oid)
        except (OSError, PlaybillError, ValueError):
            diagnostics.append(
                _diagnostic(
                    code="playbill.authoring.reference_coordinate_unavailable",
                    stage="reference_assertion",
                    offending_element=expectation.payload_path,
                    message=(
                        "The accepted coordinate that minted this ref cannot be "
                        "verified. Re-mint the reference against the current "
                        "coordinate: run playbill authoring rebase on this intent, "
                        "then preflight again."
                    ),
                    owner="daemon",
                    disposition="terminal",
                    repairs=(),
                )
            )
            continue
        minted_content = minted_tree.get(path)
        if minted_content is None:
            diagnostics.append(
                _diagnostic(
                    code="playbill.authoring.reference_absent_at_minted_coordinate",
                    stage="reference_assertion",
                    offending_element=expectation.payload_path,
                    message=(
                        "The named artifact was absent where this ref claims it was "
                        "minted. Re-resolve the target with playbill discover, then "
                        "re-create the intent with the address it returns."
                    ),
                    owner="daemon",
                    disposition="terminal",
                    repairs=(),
                )
            )
            continue
        if existing_capture_contract_ref and not _existing_capture_contract_matches_reference(
            instance,
            value=value,
            artifact_path=path,
            artifact_content=minted_content,
        ):
            diagnostics.append(
                _diagnostic(
                    code="playbill.authoring.reference_payload_mismatch",
                    stage="reference_assertion",
                    offending_element=expectation.payload_path,
                    message="The existing Capture names another exact CaptureContract.",
                    repairs=(
                        _repair(
                            "replace_reference",
                            "Use the contract reference minted with this Capture.",
                            {
                                "address": expectation.address,
                                "artifact_kind": expectation.artifact_kind,
                            },
                        ),
                    ),
                )
            )
            continue
        current_content = base_tree.get(path)
        if current_content == minted_content:
            continue
        if current_content is not None:
            successor = AuthoringReferenceSuccessorV1(
                payload_path=expectation.payload_path,
                artifact_kind=expectation.artifact_kind,
                address=expectation.address,
                coordinate=intent.base_coordinate,
            )
            diagnostics.append(
                _diagnostic(
                    code="playbill.authoring.reference_stale",
                    stage="reference_assertion",
                    offending_element=expectation.payload_path,
                    message="The typed reference has a newer accepted successor.",
                    disposition="superseded",
                    repairs=(
                        _repair(
                            "replace_reference",
                            "Replace the stale ref with the named successor generation.",
                            successor.model_dump(mode="json"),
                        ),
                    ),
                )
            )
            continue
        # A moved successor is identified by the exact predecessor artifact digest.
        try:
            predecessor_digest = _reference_artifact_digest(
                expectation,
                path=path,
                content=minted_content,
            )
        except ValueError:
            predecessor_digest = ""
        successors: list[str] = []
        if predecessor_digest:
            for candidate_path, candidate_content in base_tree.items():
                try:
                    candidate = json.loads(candidate_content)
                except (UnicodeDecodeError, ValueError):
                    continue
                if not isinstance(candidate, dict):
                    continue
                lifecycle = candidate.get("lifecycle")
                if (
                    isinstance(lifecycle, dict)
                    and lifecycle.get("predecessor_digest") == predecessor_digest
                ):
                    successors.append(candidate_path)
        if len(successors) > 1:
            code = "playbill.authoring.reference_successor_ambiguous"
            message = (
                "More than one accepted artifact claims to succeed this reference. "
                "Name the intended successor explicitly: read the candidates with "
                "playbill list, then re-create the intent against one of them."
            )
        else:
            code = "playbill.authoring.reference_retired"
            message = (
                "The typed reference has no live successor at the intent base. "
                "Choose a live target with playbill discover, then re-create the "
                "intent against it."
            )
        diagnostics.append(
            _diagnostic(
                code=code,
                stage="reference_assertion",
                offending_element=expectation.payload_path,
                message=message,
                owner="daemon",
                disposition="terminal",
                repairs=(),
            )
        )
    return tuple(diagnostics)


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
            if not isinstance(payload.source, SelfSourceBodyV1):
                diagnostics.append(
                    _diagnostic(
                        code="playbill.authoring.insertion_target_requires_self_source",
                        stage="source_binding",
                        offending_element="insertion_target",
                        message="Publication insertion is available only for a Flow-B body.",
                        repairs=(
                            _repair(
                                "replace_source",
                                "Use a retained self-source body for publication insertion.",
                                {"required_source_tag": "playbill-self-source-body-v1"},
                            ),
                            _repair(
                                "omit_insertion_target",
                                "Omit insertion_target and keep the existing Flow-A binding.",
                                None,
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
    diagnostics.extend(_reference_diagnostics(instance, intent=intent, base_tree=base_tree))
    service = instance.proposal_service()
    proposal_ref = f"refs/proposals/{actor.actor_id}/intent-{intent.intent_id[4:]}"
    proposal_ref_oid = service.transport.read_proposal_ref(proposal_ref)
    lowered: LoweredAuthoring | None = None
    evaluation: CandidateEvaluation | None = None
    evaluated_tree = current_tree
    resolved_payload: object

    try:
        lowered = lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
        if lowered.idempotent:
            evaluated_tree = current_tree
            resolved_payload = lowered.resolved_authoring
        else:
            proposed_tree = lowered.proposed_tree
            try:
                proposed_tree = validate_proposal_tree(
                    proposed_tree,
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
                                "Reduce the authored member count or byte size named by this "
                                "refusal.",
                            ),
                        ),
                    )
                )
                blocked.append(
                    BlockedCheckV1(
                        check="proposal_evaluation",
                        blocked_by=("playbill.authoring.proposal_receive_refused",),
                        reason=(
                            "The candidate tree must pass bounded receive before semantic laws run."
                        ),
                    )
                )
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
                    query_facts_provider=service.query_facts_provider,
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
            message=(
                "The bounded diagnostic frontier was exhausted. Repair the diagnostics "
                "already listed and preflight again; the next pass reports what this "
                "one had no room for."
            ),
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
