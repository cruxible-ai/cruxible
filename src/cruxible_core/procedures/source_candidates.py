"""Bind source-language candidates to the executor's exact evidence and state reads."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatement,
    AuthoringExistingClaimDisposition,
    ClaimDerivationBinding,
    ExistingCaptureCitationSource,
    SelfSourceBody,
)
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    normalize_canonical,
    pretty_canonical_bytes,
)
from cruxible_client.contracts.claims import claim_artifact_digest, parse_claim
from cruxible_client.contracts.procedures.proposal_items import ProcedureClaimProposalItem
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.procedures.terminal_dependencies import AliasProvenanceV1, DependencyToken


class SourceClaimCandidateV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-source-claim-candidate-v1"]
    subject_kind: str
    subject_id: str
    predicate: str
    object_kind: Literal["literal", "subject", "exact_content"] = "literal"
    value: object
    role: Literal["normative", "observation", "environment_binding", "derivation"]
    rationale: str
    source_kind: Literal["supported_by", "copied_from", "self_source"]
    source_value: object
    source_alias: str | None
    basis: tuple[dict[str, Any], ...] = ()
    qualifier: str | None = None
    revises: str | None = None
    revision_claim: dict[str, Any] | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    dispositions: tuple[dict[str, Any], ...] = ()

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
        source: ExistingCaptureCitationSource | SelfSourceBody = SelfSourceBody(
            content_base64=base64.b64encode(candidate.source_value.encode("utf-8")).decode("ascii")
        )
        citation_role = None
    else:
        alias = (candidate.source_alias or "").split(".", 1)[0]
        selected = provenance.get(alias)
        if selected is None or not selected.whole.issubset(item_tokens):
            raise ValueError("candidate evidence does not belong to this item's dataflow")
        produced = {p.digest for p in selected.whole if p.slot == "produced_capture"}
        admitted = {p.digest for p in selected.whole if p.slot == "admitted_capture"}
        if isinstance(candidate.source_value, dict) and set(candidate.source_value) == {
            "capture_digest"
        }:
            # An explicit selection (a nested terminal's exact capture handle)
            # may name any Capture in the item's own dataflow, produced or
            # admitted; it is accepted only when the dataflow contains it.
            selected_capture = candidate.source_value["capture_digest"]
            captures = (
                (produced | admitted) & {selected_capture}
                if isinstance(selected_capture, str)
                else set()
            )
        else:
            # Implicit: the run's own observation, else the retained Capture it
            # was admitted (a Line's trigger input), as proposal delivery cites.
            captures = produced or admitted
        if len(captures) != 1:
            raise ValueError("selected evidence must identify one verified Capture")
        source = ExistingCaptureCitationSource(capture_digest=captures.pop())
        citation_role = "evidence" if candidate.source_kind == "supported_by" else "copy"

    def selected_claim(supplied: dict[str, Any]) -> ArtifactPin:
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
        return ArtifactPin(role="input-claim", target=claim.identity, artifact_digest=digest)

    bindings = [selected_claim(supplied) for supplied in candidate.basis]
    if bool(bindings) != (candidate.role == "derivation"):
        raise ValueError("only derivation Claims may carry nonempty basis")
    derivation = (
        None
        if not bindings
        else ClaimDerivationBinding(
            procedure=ArtifactPin(
                role="reducer", target=procedure_identity, artifact_digest=procedure_digest
            ),
            inputs=tuple(sorted(bindings, key=lambda p: p.target.qualified)),
        )
    )
    revises = candidate.revises
    if candidate.revision_claim is not None:
        if revises is not None:
            raise ValueError("revision must identify one selected Claim")
        revises = selected_claim(candidate.revision_claim).target.name
    dispositions = []
    for item in candidate.dispositions:
        if set(item) == {"claim", "disposition"}:
            claim_id = selected_claim(item["claim"]).target.name
        elif set(item) == {"claim_id", "disposition"}:
            claim_id = item["claim_id"]
        else:
            raise ValueError("invalid selected Claim disposition")
        dispositions.append(
            AuthoringExistingClaimDisposition(
                claim_id=claim_id,
                disposition=item["disposition"],
            )
        )
    if len({d.claim_id for d in dispositions}) != len(dispositions):
        raise ValueError("Claim dispositions must be unique")
    if candidate.object_kind == "subject":
        target = candidate.value
        if not isinstance(target, dict) or set(target) != {"subject_kind", "subject_id"}:
            raise ValueError("Subject value must identify an accepted Subject")
        obj = dict(
            kind="subject",
            address=SemanticAddress.whole_artifact(
                subject_path(target["subject_kind"], target["subject_id"])
            ),
        )
    elif candidate.object_kind == "exact_content":
        if not isinstance(candidate.value, str):
            raise ValueError("ExactContent requires UTF-8 text")
        obj = dict(
            kind="exact_content_body",
            content_base64=base64.b64encode(candidate.value.encode()).decode("ascii"),
        )
    else:
        obj = dict(kind="literal", value=candidate.value)
    result = ProcedureClaimProposalItem(
        statement=AuthoringClaimStatement.model_validate(
            dict(
                subject=SemanticAddress.whole_artifact(
                    subject_path(candidate.subject_kind, candidate.subject_id)
                ),
                predicate=candidate.predicate,
                qualifier=candidate.qualifier,
                object=obj,
                role=candidate.role,
                effective_from=candidate.effective_from,
                effective_until=candidate.effective_until,
            )
        ),
        rationale=candidate.rationale,
        revises=revises,
        existing_claim_dispositions=tuple(sorted(dispositions, key=lambda d: d.claim_id)),
        source=source,
        citation_role=citation_role,
        derivation=derivation,
    )
    return result.model_dump(mode="json")
