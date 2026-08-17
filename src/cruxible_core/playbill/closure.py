"""Deterministic multi-artifact dependency closure and exact refusal evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.acquisition_policies import (
    SourceAcquisitionPolicyError,
    acquisition_policy_digest,
    parse_acquisition_policy,
)
from cruxible_core.playbill.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_core.playbill.candidates import DependencyProofReferenceV1
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    Sha256Value,
    canonical_bytes,
    normalize_manifest_paths,
    typed_digest,
)
from cruxible_core.playbill.captures import (
    CaptureFormatError,
    capture_contract_digest,
    parse_capture_contract,
)
from cruxible_core.playbill.claim_types import (
    ClaimTypeFormatError,
    claim_type_digest,
    parse_claim_type,
)
from cruxible_core.playbill.claims import (
    ClaimFormatError,
    claim_artifact_digest,
    parse_claim,
)
from cruxible_core.playbill.documents import (
    DocumentArtifactAdapter,
    document_digest,
    parse_document,
)
from cruxible_core.playbill.errors import DocumentFormatError, SubjectFormatError
from cruxible_core.playbill.procedures.artifacts import (
    ProcedureFormatError,
    parse_procedure,
    procedure_artifact_digest,
)
from cruxible_core.playbill.procedures.line_specs import (
    LineSpecFormatError,
    line_spec_digest,
    parse_line_spec,
)
from cruxible_core.playbill.providers import ProviderFormatError, parse_provider, provider_digest
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.standing_mandates import (
    StandingMandateError,
    parse_standing_mandate,
    standing_mandate_digest,
)
from cruxible_core.playbill.subjects import (
    parse_subject,
    subject_digest,
)


class _StrictClosureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactDependencyStateV1(_StrictClosureModel):
    path: str
    artifact_kind: Literal[
        "document",
        "subject",
        "claim-type",
        "capture-contract",
        "provider",
        "source-acquisition-policy",
        "standing-mandate",
        "claim",
        "procedure",
        "line",
    ]
    artifact_tag: str
    identity: ArtifactIdentity
    artifact_digest: str
    pins: tuple[ArtifactPin, ...]
    lifecycle: ArtifactLifecycle

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if tuple(normalize_manifest_paths((value,))) != (value,):
            raise ValueError("artifact dependency path must be canonical")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @property
    def address(self) -> SemanticAddress:
        return SemanticAddress.whole_artifact(self.path)


def parse_dependency_artifact(path: str, content: bytes) -> ArtifactDependencyStateV1 | None:
    """Parse only artifact kinds participating in PC-A2 dependency closure."""

    try:
        if path.startswith("documents/"):
            document = parse_document(content, path=path)
            adapter = DocumentArtifactAdapter(document)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="document",
                artifact_tag=document.tag,
                identity=adapter.identity,
                artifact_digest=document_digest(document).tagged,
                pins=adapter.pins,
                lifecycle=adapter.lifecycle,
            )
        if path.startswith("subjects/"):
            subject = parse_subject(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="subject",
                artifact_tag=subject.artifact_format,
                identity=subject.identity,
                artifact_digest=subject_digest(subject).tagged,
                pins=subject.pins,
                lifecycle=subject.lifecycle,
            )
        if path.startswith("claim-types/"):
            claim_type = parse_claim_type(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="claim-type",
                artifact_tag=claim_type.artifact_format,
                identity=claim_type.identity,
                artifact_digest=claim_type_digest(claim_type).tagged,
                pins=claim_type.pins,
                lifecycle=claim_type.lifecycle,
            )
        if path.startswith("capture-contracts/"):
            contract = parse_capture_contract(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="capture-contract",
                artifact_tag=contract.artifact_format,
                identity=contract.identity,
                artifact_digest=capture_contract_digest(contract).tagged,
                pins=contract.pins,
                lifecycle=contract.lifecycle,
            )
        if path.startswith("providers/"):
            provider = parse_provider(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="provider",
                artifact_tag=provider.artifact_format,
                identity=provider.identity,
                artifact_digest=provider_digest(provider).tagged,
                pins=provider.pins,
                lifecycle=provider.lifecycle,
            )
        if path.startswith("source-acquisition-policies/"):
            policy = parse_acquisition_policy(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="source-acquisition-policy",
                artifact_tag=policy.artifact_format,
                identity=policy.identity,
                artifact_digest=acquisition_policy_digest(policy).tagged,
                pins=policy.pins,
                lifecycle=policy.lifecycle,
            )
        if path.startswith("standing-mandates/"):
            mandate = parse_standing_mandate(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="standing-mandate",
                artifact_tag=mandate.artifact_format,
                identity=mandate.identity,
                artifact_digest=standing_mandate_digest(mandate).tagged,
                pins=mandate.pins,
                lifecycle=mandate.lifecycle,
            )
        if path.startswith("claims/"):
            claim = parse_claim(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="claim",
                artifact_tag=claim.artifact_format,
                identity=claim.identity,
                artifact_digest=claim_artifact_digest(claim).tagged,
                pins=claim.pins,
                lifecycle=claim.lifecycle,
            )
        if path.startswith("procedures/"):
            procedure = parse_procedure(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="procedure",
                artifact_tag=procedure.artifact_format,
                identity=procedure.identity,
                artifact_digest=procedure_artifact_digest(procedure).tagged,
                pins=procedure.pins,
                lifecycle=procedure.lifecycle,
            )
        if path.startswith("lines/"):
            line = parse_line_spec(content, path=path)
            return ArtifactDependencyStateV1(
                path=path,
                artifact_kind="line",
                artifact_tag=line.artifact_format,
                identity=line.identity,
                artifact_digest=line_spec_digest(line).tagged,
                pins=line.pins,
                lifecycle=line.lifecycle,
            )
    except (
        CaptureFormatError,
        ClaimFormatError,
        DocumentFormatError,
        ProviderFormatError,
        SourceAcquisitionPolicyError,
        StandingMandateError,
        SubjectFormatError,
        ClaimTypeFormatError,
        ProcedureFormatError,
        LineSpecFormatError,
    ):
        raise
    return None


def dependency_artifacts(
    tree: Mapping[str, bytes],
) -> tuple[ArtifactDependencyStateV1, ...]:
    items = tuple(
        parsed
        for path in sorted(tree, key=lambda item: item.encode("utf-8"))
        if (parsed := parse_dependency_artifact(path, tree[path])) is not None
    )
    identities = tuple(item.identity.qualified for item in items)
    if len(identities) != len(set(identities)):
        raise ValueError("candidate tree contains a duplicate semantic artifact identity")
    return items


class UnresolvedArtifactPinV1(_StrictClosureModel):
    source_path: str
    source_artifact_digest: str
    target_identity: ArtifactIdentity
    expected_target_digest: str
    pin_role: str
    reason: Literal["missing_or_digest_mismatch", "live_source_targets_retired"]


class IncompleteClosureItemV1(_StrictClosureModel):
    path: str
    address: SemanticAddress
    current_artifact_digest: str
    triggering_dependency_path: str
    triggering_dependency_digest: str
    dependency_edge_role: str
    permitted_dispositions: tuple[Literal["successor", "retire", "invalidation"], ...]

    @field_validator("current_artifact_digest", "triggering_dependency_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class MemberDependencyProofsV1(_StrictClosureModel):
    path: str
    proof_refs: tuple[DependencyProofReferenceV1, ...]


class ClosureEvaluationV2(_StrictClosureModel):
    tag: Literal["playbill-closure-evaluation-v2"] = "playbill-closure-evaluation-v2"
    verdict: Literal["complete", "refused"]
    paths: tuple[str, ...]
    dependency_graph_digest: str
    member_dependency_proofs: tuple[MemberDependencyProofsV1, ...]
    missing_dependents: tuple[IncompleteClosureItemV1, ...] = ()
    unresolved_pins: tuple[UnresolvedArtifactPinV1, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "ClosureEvaluationV2":
        refused = bool(self.missing_dependents or self.unresolved_pins)
        if (self.verdict == "refused") != refused:
            raise ValueError("closure verdict must agree with exact refusal evidence")
        if tuple(item.path for item in self.member_dependency_proofs) != self.paths:
            raise ValueError("closure member proof paths differ from complete scope")
        return self

    def proofs_for(self, path: str) -> tuple[DependencyProofReferenceV1, ...]:
        for item in self.member_dependency_proofs:
            if item.path == path:
                return item.proof_refs
        raise KeyError(path)


def _edges(
    artifacts: tuple[ArtifactDependencyStateV1, ...],
) -> tuple[DependencyProofReferenceV1, ...]:
    by_identity = {item.identity.qualified: item for item in artifacts}
    edges: list[DependencyProofReferenceV1] = []
    for source in artifacts:
        for pin in source.pins:
            target = by_identity.get(pin.target.qualified)
            if target is None or target.artifact_digest != pin.artifact_digest:
                continue
            edges.append(
                DependencyProofReferenceV1(
                    source_path=source.path,
                    source_artifact_digest=source.artifact_digest,
                    target_path=target.path,
                    target_artifact_digest=target.artifact_digest,
                    pin_role=pin.role,
                )
            )
    return tuple(sorted(edges, key=lambda item: canonical_bytes(item.model_dump(mode="json"))))


def _unresolved_pins(
    artifacts: tuple[ArtifactDependencyStateV1, ...],
    *,
    source_paths: set[str],
) -> tuple[UnresolvedArtifactPinV1, ...]:
    by_identity = {item.identity.qualified: item for item in artifacts}
    missing: list[UnresolvedArtifactPinV1] = []
    for source in artifacts:
        if source.path not in source_paths:
            continue
        for pin in source.pins:
            # These component families are exact compiler/policy-registry pins
            # until PC-F gives QueryDefinition and Contract ledger artifacts.
            # Their family law verifies the role-named digest; this exception
            # is never a name lookup and never permits a missing Playbill
            # artifact kind such as Procedure, ClaimType, or Provider.
            if pin.target.kind in {
                "Contract",
                "EffectPolicy",
                "EnvironmentManifest",
                "LandingFilter",
                "Policy",
                "QueryDefinition",
                "Reducer",
            }:
                continue
            target = by_identity.get(pin.target.qualified)
            reason: Literal["missing_or_digest_mismatch", "live_source_targets_retired"] | None = (
                None
            )
            if target is None or target.artifact_digest != pin.artifact_digest:
                reason = "missing_or_digest_mismatch"
            elif source.lifecycle.state == "live" and target.lifecycle.state == "retired":
                reason = "live_source_targets_retired"
            if reason is not None:
                missing.append(
                    UnresolvedArtifactPinV1(
                        source_path=source.path,
                        source_artifact_digest=source.artifact_digest,
                        target_identity=pin.target,
                        expected_target_digest=pin.artifact_digest,
                        pin_role=pin.role,
                        reason=reason,
                    )
                )
    return tuple(sorted(missing, key=lambda item: canonical_bytes(item.model_dump(mode="json"))))


def evaluate_dependency_closure(
    *,
    parent_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    scope: tuple[str, ...],
) -> ClosureEvaluationV2:
    """Compute exact reverse dependencies and refuse every omitted live dependent."""

    if tuple(normalize_manifest_paths(scope)) != scope or not scope:
        raise ValueError("closure scope must be nonempty, sorted, and unique")
    parent = dependency_artifacts(parent_tree)
    candidate = dependency_artifacts(candidate_tree)
    parent_by_path = {item.path: item for item in parent}
    parent_edges = _edges(parent)
    candidate_edges = _edges(candidate)
    scope_set = set(scope)
    missing: list[IncompleteClosureItemV1] = []
    for changed_path in scope:
        changed = parent_by_path.get(changed_path)
        if changed is None:
            continue
        for edge in parent_edges:
            if edge.target_path != changed_path or edge.source_path in scope_set:
                continue
            dependent = parent_by_path[edge.source_path]
            if dependent.lifecycle.state != "live":
                continue
            missing.append(
                IncompleteClosureItemV1(
                    path=dependent.path,
                    address=dependent.address,
                    current_artifact_digest=dependent.artifact_digest,
                    triggering_dependency_path=changed.path,
                    triggering_dependency_digest=changed.artifact_digest,
                    dependency_edge_role=edge.pin_role,
                    permitted_dispositions=("invalidation", "retire", "successor"),
                )
            )
    missing_tuple = tuple(
        sorted(missing, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
    )
    unresolved = _unresolved_pins(candidate, source_paths=scope_set)
    graph_digest = typed_digest(
        Sha256Value,
        "playbill-dependency-graph-v2",
        {
            "parent_edges": [item.model_dump(mode="json") for item in parent_edges],
            "candidate_edges": [item.model_dump(mode="json") for item in candidate_edges],
        },
    ).tagged
    proofs: list[MemberDependencyProofsV1] = []
    all_edges = tuple(
        sorted(
            {*parent_edges, *candidate_edges},
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )
    for path in scope:
        related = tuple(
            edge for edge in all_edges if edge.source_path == path or edge.target_path == path
        )
        proofs.append(MemberDependencyProofsV1(path=path, proof_refs=related))
    return ClosureEvaluationV2(
        verdict="refused" if missing_tuple or unresolved else "complete",
        paths=scope,
        dependency_graph_digest=graph_digest,
        member_dependency_proofs=tuple(proofs),
        missing_dependents=missing_tuple,
        unresolved_pins=unresolved,
    )


__all__ = [
    "ArtifactDependencyStateV1",
    "ClosureEvaluationV2",
    "IncompleteClosureItemV1",
    "MemberDependencyProofsV1",
    "UnresolvedArtifactPinV1",
    "dependency_artifacts",
    "evaluate_dependency_closure",
    "parse_dependency_artifact",
]
