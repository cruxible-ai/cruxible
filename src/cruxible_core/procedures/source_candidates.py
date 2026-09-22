"""Bind source-language candidates to the executor's exact evidence and state reads."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    ClaimDerivationBindingV1,
    ExistingCaptureCitationSourceV1,
    SelfSourceBodyV1,
)
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    normalize_canonical,
    pretty_canonical_bytes,
)
from cruxible_client.contracts.claims import claim_artifact_digest, parse_claim
from cruxible_client.contracts.procedures.proposal_items import ProcedureClaimProposalItemV2
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.procedures.terminal_dependencies import AliasProvenanceV1, DependencyToken


class SourceClaimCandidateV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-source-claim-candidate-v1"]
    subject_kind: str
    subject_id: str
    predicate: str
    value: object
    role: Literal["normative", "observation", "environment_binding", "derivation"]
    rationale: str
    source_kind: Literal["supported_by", "copied_from", "self_source"]
    source_value: object
    source_alias: str | None
    basis: tuple[dict[str, Any], ...] = ()
    qualifier: str | None = None
    revises: str | None = None

    _canonical = field_validator("value", "source_value", mode="before")(normalize_canonical)


def bind_source_candidate(
    value: object,
    *,
    procedure_identity: ArtifactIdentity,
    procedure_digest: str,
    outputs: Mapping[str, CanonicalValue],
    provenance: Mapping[str, AliasProvenanceV1],
    item_tokens: frozenset[DependencyToken],
) -> CanonicalValue:
    """Convert only the new source form; historical terminal items retain their wire."""
    if not isinstance(value, dict) or value.get("tag") != "playbill-source-claim-candidate-v1":
        return value  # type: ignore[return-value]
    candidate = SourceClaimCandidateV1.model_validate(value)
    citation_role: Literal["evidence", "copy"] | None
    if candidate.source_kind == "self_source":
        if not isinstance(candidate.source_value, str) or candidate.source_alias is not None:
            raise ValueError("self_source must carry text without an evidence alias")
        source: ExistingCaptureCitationSourceV1 | SelfSourceBodyV1 = SelfSourceBodyV1(
            content_base64=base64.b64encode(candidate.source_value.encode("utf-8")).decode("ascii")
        )
        citation_role = None
    else:
        alias = (candidate.source_alias or "").split(".", 1)[0]
        selected = provenance.get(alias)
        if selected is None or not selected.whole.issubset(item_tokens):
            raise ValueError("candidate evidence does not belong to this item's dataflow")
        captures = {p.digest for p in selected.whole if p.slot == "produced_capture"}
        if isinstance(candidate.source_value, dict) and set(candidate.source_value) == {
            "capture_digest"
        }:
            # A nested terminal returns an exact registered capture handle. Its
            # provenance can also include upstream observations it consumed.
            # Select the handle only when retained dataflow actually contains it.
            selected_capture = candidate.source_value["capture_digest"]
            captures = captures & {selected_capture} if isinstance(selected_capture, str) else set()
        if len(captures) != 1:
            raise ValueError("selected evidence must identify one verified produced Capture")
        source = ExistingCaptureCitationSourceV1(capture_digest=captures.pop())
        citation_role = "evidence" if candidate.source_kind == "supported_by" else "copy"
    bindings: list[ArtifactPin] = []
    for supplied in candidate.basis:
        # Match the complete admitted read, including its exact Claim and verdict.
        matching = [
            name
            for name, actual in outputs.items()
            if actual == supplied
            and name in provenance
            and provenance[name].whole.issubset(item_tokens)
            and any(p.slot == "accepted_state" for p in provenance[name].whole)
        ]
        if not matching:
            raise ValueError("candidate basis is not an exact admitted Claim read")
        claim = parse_claim(pretty_canonical_bytes(supplied["claim"]), path=str(supplied["path"]))
        digest = claim_artifact_digest(claim).tagged
        if supplied.get("artifact_digest") != digest:
            raise ValueError("candidate basis does not reproduce its Claim version")
        bindings.append(
            ArtifactPin(role="input-claim", target=claim.identity, artifact_digest=digest)
        )
    if bool(bindings) != (candidate.role == "derivation"):
        raise ValueError("only derivation Claims may carry nonempty basis")
    derivation = (
        None
        if not bindings
        else ClaimDerivationBindingV1(
            procedure=ArtifactPin(
                role="reducer", target=procedure_identity, artifact_digest=procedure_digest
            ),
            inputs=tuple(sorted(bindings, key=lambda p: p.target.qualified)),
        )
    )
    result = ProcedureClaimProposalItemV2(
        statement=AuthoringClaimStatementV1.model_validate(
            dict(
                subject=SemanticAddress.whole_artifact(
                    subject_path(candidate.subject_kind, candidate.subject_id)
                ),
                predicate=candidate.predicate,
                qualifier=candidate.qualifier,
                object=dict(kind="literal", value=candidate.value),
                role=candidate.role,
            )
        ),
        rationale=candidate.rationale,
        revises=candidate.revises,
        source=source,
        citation_role=citation_role,
        derivation=derivation,
    )
    return result.model_dump(mode="json")
