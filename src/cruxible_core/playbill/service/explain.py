"""Coordinate-bound structured explanation over accepted Playbill projections."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.documents import parse_document
from cruxible_core.playbill.errors import DocumentNotFoundError, ProjectionIntegrityError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillDocumentView,
    service_get_playbill_document,
)

PlaybillExplainDetail = Literal["summary", "evidence", "proof"]
PLAYBILL_EXPLAIN_SUPPORTED_DETAILS: tuple[Literal["summary", "evidence"], ...] = (
    "summary",
    "evidence",
)


class _StrictExplainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillExplainResult(_StrictExplainModel):
    tag: Literal["playbill-explain-v1"] = "playbill-explain-v1"
    subject: SemanticAddress
    coordinate: PlaybillAcceptedCoordinate
    detail: Literal["summary", "evidence"]
    governance: dict[str, object]
    provenance: dict[str, object]
    attestation_coverage: dict[str, object]
    history: dict[str, object]
    source_mapping: dict[str, object] | None
    proof_references: tuple[dict[str, object], ...]
    redactions: tuple[str, ...]
    supported_details: tuple[Literal["summary", "evidence"], ...] = (
        "summary",
        "evidence",
    )


class PlaybillExplainUnsupportedDetail(_StrictExplainModel):
    tag: Literal["playbill-explain-unsupported-detail-v1"] = (
        "playbill-explain-unsupported-detail-v1"
    )
    subject: SemanticAddress
    coordinate: PlaybillAcceptedCoordinate
    requested_detail: Literal["proof"] = "proof"
    code: Literal["playbill.explain.detail_unsupported"] = "playbill.explain.detail_unsupported"
    message: str = "Complete proof-bundle retrieval is deferred beyond PB-E."
    supported_details: tuple[Literal["summary", "evidence"], ...] = (
        "summary",
        "evidence",
    )


PlaybillExplainResponse = PlaybillExplainResult | PlaybillExplainUnsupportedDetail


def _facts(document: PlaybillDocumentView) -> dict[str, object]:
    result: dict[str, object] = {}
    for fact in document.facts:
        schema_id = fact.get("schema_id")
        if not isinstance(schema_id, str) or schema_id in result:
            raise ProjectionIntegrityError("explanation facts are missing or ambiguous by schema")
        result[schema_id] = fact.get("value")
    return result


def _required_object(facts: dict[str, object], schema_id: str) -> dict[str, object]:
    value = facts.get(schema_id)
    if not isinstance(value, dict):
        raise ProjectionIntegrityError(f"accepted explanation is missing {schema_id}")
    return value


def _summary_governance(value: dict[str, object]) -> dict[str, object]:
    return {
        key: value[key]
        for key in (
            "activation_policy",
            "approval_requirements",
            "law_digest",
            "law_identifier",
            "required_tier",
        )
        if key in value
    }


def _summary_provenance(value: dict[str, object]) -> dict[str, object]:
    return {
        key: value[key]
        for key in (
            "actor_id",
            "artifact_digest",
            "input_digest",
            "source_compilation_digest",
        )
        if key in value
    }


def _summary_coverage(value: dict[str, object]) -> dict[str, object]:
    attestations = value.get("attestations")
    summarized: list[dict[str, object]] = []
    if isinstance(attestations, list):
        for item in attestations:
            if isinstance(item, dict):
                summarized.append(
                    {key: item[key] for key in ("attestation_digest", "signer_id") if key in item}
                )
    return {
        "attestations": summarized,
        "basis_kinds": _basis_kinds(value.get("basis")),
        "coverage_binding": value.get("coverage_binding"),
    }


def _summary_history(value: dict[str, object]) -> dict[str, object]:
    return {
        key: value[key] for key in ("history", "predecessor_digest", "proof_ref") if key in value
    }


def _basis_kinds(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            kind
            for item in value
            if isinstance(item, dict) and isinstance((kind := item.get("kind")), str)
        }
    )


def _proof_references(*values: object) -> tuple[dict[str, object], ...]:
    found: dict[tuple[str, str, str], dict[str, object]] = {}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if {
                "accepted_coordinate",
                "candidate_digest",
                "change_set_path",
                "changeset_digest",
            }.issubset(value):
                path = value.get("change_set_path")
                change = value.get("changeset_digest")
                candidate = value.get("candidate_digest")
                if (
                    isinstance(path, dict)
                    and isinstance(change, dict)
                    and isinstance(candidate, dict)
                    and isinstance(path.get("$path"), str)
                    and isinstance(change.get("$digest"), str)
                    and isinstance(candidate.get("$digest"), str)
                ):
                    key = (
                        path["$path"],
                        change["$digest"],
                        candidate["$digest"],
                    )
                    found[key] = value
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for item in values:
        visit(item)
    return tuple(found[key] for key in sorted(found))


def service_explain_playbill_subject(
    instance: PlaybillInstance,
    *,
    subject: SemanticAddress,
    at: PlaybillAcceptedCoordinate,
    detail: PlaybillExplainDetail,
    access: BodyAccessContext,
) -> PlaybillExplainResponse:
    """Explain one accepted subject only after exact coordinate correspondence."""

    coordinate = instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )
    public_coordinate = PlaybillAcceptedCoordinate.from_internal(coordinate)
    if detail == "proof":
        return PlaybillExplainUnsupportedDetail(
            subject=subject,
            coordinate=public_coordinate,
        )

    tree = instance.tree_at(coordinate.git_oid)
    content = tree.get(subject.artifact_path)
    if content is None:
        raise DocumentNotFoundError(subject.artifact_path)
    shell = parse_document(content, path=subject.artifact_path)
    document = service_get_playbill_document(
        instance,
        identity=shell.identity,
        access=access,
        at=public_coordinate,
    )
    facts = _facts(document)
    governance = _required_object(facts, "playbill.document.governance")
    provenance = _required_object(facts, "playbill.document.provenance")
    coverage = _required_object(facts, "playbill.document.attestation_coverage")
    history = _required_object(facts, "playbill.document.history")
    source = facts.get("playbill.document.source_mapping")
    if source is not None and not isinstance(source, dict):
        raise ProjectionIntegrityError("Document source mapping has an invalid shape")
    proof_references = _proof_references(governance, provenance, coverage, history)
    if not proof_references:
        raise ProjectionIntegrityError("accepted explanation has no exact proof reference")

    return PlaybillExplainResult(
        subject=subject,
        coordinate=public_coordinate,
        detail=detail,
        governance=(governance if detail == "evidence" else _summary_governance(governance)),
        provenance=(provenance if detail == "evidence" else _summary_provenance(provenance)),
        attestation_coverage=(coverage if detail == "evidence" else _summary_coverage(coverage)),
        history=history if detail == "evidence" else _summary_history(history),
        source_mapping=source,
        proof_references=proof_references,
        redactions=() if access.can_read_body else ("body", "source_mapping"),
    )


__all__ = [
    "PLAYBILL_EXPLAIN_SUPPORTED_DETAILS",
    "PlaybillExplainDetail",
    "PlaybillExplainResponse",
    "PlaybillExplainResult",
    "PlaybillExplainUnsupportedDetail",
    "service_explain_playbill_subject",
]
