"""Shared fixtures for the PC-F2 coverage-delivery slice.

One handbook, one scratch file, one cited paragraph. Everything the coverage
tests need is a variation on where those bytes currently sit and what the
accepted ledger says about them.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    GenerationRoot,
    SemanticRoot,
    Sha256Value,
    typed_digest,
)
from cruxible_client.contracts.captures import CaptureEnvelopeV1, CaptureRunCoordinateV1
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    EvidenceCommitmentV1,
    ExternalSourceReferenceV1,
    LedgerSourceReferenceV1,
    SourceAccessClass,
    SourceHandleV1,
    SourceReferenceV1,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageRequestV1,
    CoverageSpanRequestV1,
    LogicalSourceIdentityV1,
)
from cruxible_core.playbill.coverage.indexes import (
    CaptureCitationInputV1,
    CaptureCitationInputV2,
    EvidenceCitationIndexV1,
    EvidenceCitationIndexV2,
    WorkingOccurrenceOverlayV2,
    WorkingSourceContent,
    build_evidence_citation_index,
    build_evidence_citation_index_v2,
    build_working_occurrence_overlay,
)
from cruxible_core.playbill.coverage.manifest import (
    CoverageManifestBodyV1,
    CoverageManifestBodyV2,
    coverage_manifest_body,
    coverage_manifest_body_v2,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

INSTANCE_ID = "inst_coverage"
NOW = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)

HANDBOOK = LogicalSourceIdentityV1(plane="ledger", identity="documents/handbook.md")
SCRATCH = LogicalSourceIdentityV1(plane="external", identity="workspace.scratch")
CATALOG = LogicalSourceIdentityV1(plane="external", identity="workspace.catalog")

CITED = b"The reviewer accepted the migration plan on the second reading.\n"
PREAMBLE = b"# Handbook\n\nIntroduction paragraph.\n\n"
EPILOGUE = b"\nUnrelated closing paragraph.\n"


def coordinate(*, generation: str = "22") -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="11" * 20,
        semantic_root=SemanticRoot("aa" * 32).tagged,
        generation_root=GenerationRoot(generation * 32).tagged,
        compiler_digest=Sha256Value("bb" * 32).tagged,
    )


def sha256(content: bytes) -> str:
    return Sha256Value(hashlib.sha256(content).hexdigest()).tagged


def _digest(domain: str, value: str) -> str:
    return typed_digest(Sha256Value, domain, {"value": value}).tagged


def source_reference(source: LogicalSourceIdentityV1) -> SourceReferenceV1:
    """Build the accepted reference that names one logical source."""

    if source.plane == "ledger":
        return LedgerSourceReferenceV1(
            address=SemanticAddress.whole_artifact(source.identity),
            coordinate=coordinate(),
        )
    return ExternalSourceReferenceV1(
        source_identity=source.identity,
        producer_binding_digest=_digest("coverage-binding", source.identity),
        coordinate_type="workspace-file-v1",
        coordinate={"scope": source.identity},
        selector_type="whole-file-v1",
        selector={"whole": True},
        replayability="exact",
    )


def commitment(content: bytes) -> EvidenceCommitmentV1:
    return EvidenceCommitmentV1(
        digest_kind="exact_bytes",
        digest=sha256(content),
        byte_length=len(content),
        materialization="ledger",
    )


def capture(
    source: LogicalSourceIdentityV1 | None,
    content: bytes,
    *,
    name: str = "handbook",
    access_class: SourceAccessClass = "instance",
    with_handle: bool = False,
) -> CaptureCitationInputV1:
    """One accepted Capture citing exactly these bytes at that logical source.

    ``source=None`` produces the CAS case: content-addressed evidence that names
    no logical source at all.
    """

    reference = (
        CasSourceReferenceV1(content_digest=sha256(content))
        if source is None
        else source_reference(source)
    )
    materialization = "cas" if source is None else ("ledger" if source.plane == "ledger" else "cas")
    evidence = commitment(content).model_copy(update={"materialization": materialization})
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=_digest("coverage-contract", name),
        source=reference,
        commitment=evidence,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="watcher",
            run_id=f"run-{name}",
            bound_generation=_digest("coverage-generation", name),
            executable_identity=ArtifactIdentity(kind="Procedure", name="coverage.watch"),
            executable_digest=_digest("coverage-executable", name),
        ),
        run_receipt_digest=_digest("coverage-receipt", name),
        producer=ArtifactIdentity(kind="Provider", name="coverage.watcher"),
        producer_binding_digest=(
            reference.producer_binding_digest
            if isinstance(reference, ExternalSourceReferenceV1)
            else _digest("coverage-binding", name)
        ),
        observed_at=NOW,
    )
    handle = None
    if with_handle:
        handle = SourceHandleV1(
            subject=SemanticAddress.whole_artifact(f"subjects/project.note/{name}.json"),
            at=coordinate(),
            source=reference,
            commitment=evidence,
            exact_spans=(
                ContentSpan(
                    content_digest=evidence.digest,
                    start_byte=0,
                    end_byte=len(content),
                ),
            ),
            access_class=access_class,
        )
    return CaptureCitationInputV1(
        capture_digest=_digest("coverage-capture", name),
        envelope=envelope,
        access_class=access_class,
        source_handle=handle,
    )


def index(
    *captures: CaptureCitationInputV1,
    at: AcceptedCoordinate | None = None,
    truncated: bool = False,
) -> EvidenceCitationIndexV1:
    return build_evidence_citation_index(
        at=at or coordinate(),
        captures=captures,
        truncated=truncated,
    )


def index_v2(
    *captures: CaptureCitationInputV1,
    at: AcceptedCoordinate | None = None,
    truncated: bool = False,
) -> EvidenceCitationIndexV2:
    """Build the surviving association-aware index from compact test inputs."""

    return build_evidence_citation_index_v2(
        at=at or coordinate(),
        captures=tuple(
            CaptureCitationInputV2.model_validate(
                {
                    **item.model_dump(mode="json"),
                    "tag": "playbill-coverage-capture-citation-input-v2",
                    "observation_trust": "proposer_observed",
                }
            )
            for item in captures
        ),
        truncated=truncated,
    )


def overlay(
    *sources: WorkingSourceContent,
    citations: EvidenceCitationIndexV1,
) -> WorkingOccurrenceOverlayV2:
    return build_working_occurrence_overlay(
        sources,
        wanted=unmaterialized_wanted(citations),
    )


def unmaterialized_wanted(
    citations: EvidenceCitationIndexV1,
) -> tuple[tuple[str, int, None], ...]:
    """Select the deterministic exhaustive route used by unit-only callers."""

    return tuple(
        (digest, byte_length, None) for digest, byte_length in citations.wanted_selections()
    )


def working(source: LogicalSourceIdentityV1, content: bytes) -> WorkingSourceContent:
    return WorkingSourceContent(source=source, content=content)


def manifest(
    citations: EvidenceCitationIndexV1,
    snapshot: WorkingOccurrenceOverlayV2,
    *,
    access: CoverageAccessProfileV1 | None = None,
    epoch: int = 0,
    watcher_health: str = "absent",
) -> CoverageManifestBodyV1:
    return coverage_manifest_body(
        instance_id=INSTANCE_ID,
        index=citations,
        overlay=snapshot,
        access_profile=access or profile(),
        epoch=epoch,
        watcher_health=watcher_health,  # type: ignore[arg-type]
    )


def manifest_v2(
    citations: EvidenceCitationIndexV2,
    snapshot: WorkingOccurrenceOverlayV2,
    *,
    access: CoverageAccessProfileV1 | None = None,
    epoch: int = 0,
    watcher_health: str = "absent",
) -> CoverageManifestBodyV2:
    return coverage_manifest_body_v2(
        instance_id=INSTANCE_ID,
        index=citations,
        overlay=snapshot,
        access_profile=access or profile(),
        epoch=epoch,
        watcher_health=watcher_health,  # type: ignore[arg-type]
    )


def profile(
    *,
    permitted: tuple[SourceAccessClass, ...] = ("instance", "public"),
    disclose: bool = True,
) -> CoverageAccessProfileV1:
    return CoverageAccessProfileV1(
        profile_id="coverage.test",
        permitted_access_classes=tuple(sorted(set(permitted))),
        disclose_restricted_existence=disclose,
    )


def request(
    *sources: LogicalSourceIdentityV1,
    at: AcceptedCoordinate | None = None,
) -> CoverageRequestV1:
    return CoverageRequestV1(
        instance_id=INSTANCE_ID,
        at=at or coordinate(),
        spans=tuple(CoverageSpanRequestV1(source=item) for item in sources),
    )
