"""One vendor-neutral coverage operation over accepted state (§11.7).

This is the service half of coverage delivery. It builds the reverse evidence
index from accepted Claims and the Capture envelopes they pin, publishes the
freshness manifest that lets an answer fail closed, and calls the one pure
resolver. It decides nothing the resolver decides.

What it may write, and what it may not
--------------------------------------
It writes exactly one thing: the local coverage manifest, in the instance's
rebuildable cache root, and only when the observed snapshot or the accepted
coordinate has actually moved. That file is not accepted state, not a wire
record, and not an exhaust journal entry; deleting it costs provable freshness
and nothing else. No receipt is appended -- see
`playbill_resolve_coverage` for why that absence is a decision.

Access, derived rather than declared
------------------------------------
The access profile is built here from the served surface's own read authority,
never accepted from the caller. A request that could name its own permitted
access classes would be a request that could widen its own disclosure, and
§11.6.3's `denied` branch would then be advisory rather than enforced.
"""

from __future__ import annotations

from collections.abc import Sequence

from cruxible_core.playbill.captures import parse_capture_envelope
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.claims import (
    AcceptedClaim,
    claim_artifact_digest,
    claim_statement_digest,
)
from cruxible_core.playbill.coverage.adapter import (
    WorkingSourceObservationV1,
    build_overlay,
    coverage_span_requests,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageCardBudgetV1,
    CoverageRequestV1,
    CoverageResultV1,
    LogicalSourceIdentityV1,
)
from cruxible_core.playbill.coverage.indexes import (
    CaptureCitationInputV1,
    CoverageScanBudgetV1,
    EvidenceCitationIndexV1,
    WorkingOccurrenceOverlayV1,
    build_evidence_citation_index,
)
from cruxible_core.playbill.coverage.manifest import (
    COVERAGE_DIRECTORY,
    CoverageManifestBodyV1,
    coverage_manifest_body,
    load_coverage_manifest_file,
    write_coverage_manifest,
)
from cruxible_core.playbill.coverage.resolver import resolve_coverage
from cruxible_core.playbill.errors import ProposalIntegrityError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.source_references import SourceAccessClass
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    service_list_playbill_claims,
)

COVERAGE_ACCESS_PROFILE_ID = "playbill.coverage.read"
COVERAGE_PRINCIPAL = "playbill-coverage"
COVERAGE_EVIDENCE_ACCESS_CLASS: SourceAccessClass = "instance"


def coverage_access_profile() -> CoverageAccessProfileV1:
    """The access profile the served coverage surface reads under.

    Accepted evidence is indexed at the `instance` access class, exactly as the
    Claim-explanation surface labels the source handles it returns, so a caller
    holding this instance's read authority sees it and nothing wider.
    """

    return CoverageAccessProfileV1(
        profile_id=COVERAGE_ACCESS_PROFILE_ID,
        permitted_access_classes=("instance", "public"),
        disclose_restricted_existence=True,
    )


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: PlaybillAcceptedCoordinate | None,
) -> PlaybillAcceptedCoordinate:
    if at is not None and not isinstance(at, PlaybillAcceptedCoordinate):
        raise ProposalIntegrityError("coverage accepts only verified accepted coordinates")
    if at is None:
        return PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    return PlaybillAcceptedCoordinate.from_internal(
        instance.resolve_accepted_coordinate(
            git_oid=at.git_oid,
            semantic_root=at.semantic_root,
            generation_root=at.generation_root,
            compiler_digest=at.compiler_digest,
        )
    )


def build_accepted_evidence_index(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate,
) -> EvidenceCitationIndexV1:
    """Rebuild the reverse evidence index from accepted state at one coordinate.

    Reachability, not enumeration: the index carries the Captures accepted
    Claims actually pin, because a Capture no accepted Claim reaches is not
    accepted evidence anyone can cite. Retired Claims contribute their Captures
    but not their dependent counts -- the builder already drops non-live Claims
    from the citation side -- so retiring a Claim never silently deletes the
    evidence history a drift card points back at.
    """

    listing = service_list_playbill_claims(instance, at=at, include_retired=True)
    access = BodyAccessContext(principal_id=COVERAGE_PRINCIPAL, can_read_body=True)
    store = instance.body_store()

    claims: list[AcceptedClaim] = []
    captures: dict[str, CaptureCitationInputV1] = {}
    for view in listing.claims:
        artifact = _claim_from_view(view)
        path = view.envelope.get("path")
        if not isinstance(path, str):
            raise ProposalIntegrityError("Claim projection envelope has no path")
        claims.append(
            AcceptedClaim(
                path=path,
                claim=artifact,
                statement_digest=claim_statement_digest(artifact.statement).tagged,
                artifact_digest=claim_artifact_digest(artifact).tagged,
            )
        )
        for digest in artifact.backing.capture_digests:
            if digest in captures:
                continue
            captures[digest] = CaptureCitationInputV1(
                capture_digest=digest,
                envelope=parse_capture_envelope(store.read(digest, access=access)),
                access_class=COVERAGE_EVIDENCE_ACCESS_CLASS,
            )

    return build_evidence_citation_index(
        at=at,
        captures=tuple(captures[digest] for digest in sorted(captures)),
        claims=tuple(claims),
    )


def accepted_evidence_sources(
    index: EvidenceCitationIndexV1,
) -> tuple[LogicalSourceIdentityV1, ...]:
    """The logical sources accepted evidence names, in canonical order."""

    seen: dict[bytes, LogicalSourceIdentityV1] = {
        citation.accepted_source.sort_key: citation.accepted_source
        for citation in index.citations
        if citation.accepted_source is not None
    }
    return tuple(seen[key] for key in sorted(seen))


def _publish_manifest(
    instance: PlaybillInstance,
    *,
    instance_id: str,
    index: EvidenceCitationIndexV1,
    overlay: WorkingOccurrenceOverlayV1,
    access_profile: CoverageAccessProfileV1,
) -> CoverageManifestBodyV1:
    """Publish the freshness manifest, advancing the epoch only when it moved.

    The epoch is a counter over *observations*, not over calls. Two resolves of
    an unchanged working set at an unchanged accepted coordinate are the same
    observation, and republishing them would make the epoch a call counter that
    no reader could use to order two snapshots.
    """

    directory = instance.root / COVERAGE_DIRECTORY
    existing = load_coverage_manifest_file(directory)
    epoch = 0 if existing is None else existing.body.epoch + 1
    candidate = coverage_manifest_body(
        instance_id=instance_id,
        index=index,
        overlay=overlay,
        access_profile=access_profile,
        epoch=epoch,
    )
    if existing is not None:
        previous = existing.body
        unchanged = (
            previous.instance_id == candidate.instance_id
            and previous.at == candidate.at
            and previous.index_digest == candidate.index_digest
            and previous.overlay_digest == candidate.overlay_digest
            and previous.scope == candidate.scope
            and previous.access_profile == candidate.access_profile
        )
        if unchanged:
            return previous
    write_coverage_manifest(directory, candidate)
    return candidate


def service_resolve_playbill_coverage(
    instance: PlaybillInstance,
    *,
    instance_id: str,
    observations: Sequence[WorkingSourceObservationV1],
    at: PlaybillAcceptedCoordinate | None = None,
    budget: CoverageCardBudgetV1 | None = None,
    scan_budget: CoverageScanBudgetV1 | None = None,
) -> CoverageResultV1:
    """Resolve one batch of working-set observations against accepted state.

    The whole operation is: rebuild the index, hash the observed snapshot into
    the overlay, publish the freshness manifest, resolve. Every step is a pure
    function of accepted state and the observed bytes, so deleting both
    disposable indexes and the manifest and running it again reproduces the
    same answer.
    """

    if not observations:
        raise ProposalIntegrityError("a coverage request must name at least one working source")

    coordinate = _resolve_coordinate(instance, at)
    index = build_accepted_evidence_index(instance, at=coordinate)
    overlay = build_overlay(
        observations,
        wanted=index.wanted_selections(),
        budget=scan_budget,
    )
    access_profile = coverage_access_profile()
    manifest = _publish_manifest(
        instance,
        instance_id=instance_id,
        index=index,
        overlay=overlay,
        access_profile=access_profile,
    )
    request = CoverageRequestV1(
        instance_id=instance_id,
        at=coordinate,
        spans=coverage_span_requests(observations),
        budget=budget or CoverageCardBudgetV1(),
    )
    return resolve_coverage(
        request,
        index=index,
        overlay=overlay,
        access=access_profile,
        manifest=manifest,
    )


__all__ = [
    "COVERAGE_ACCESS_PROFILE_ID",
    "accepted_evidence_sources",
    "build_accepted_evidence_index",
    "coverage_access_profile",
    "service_resolve_playbill_coverage",
]
