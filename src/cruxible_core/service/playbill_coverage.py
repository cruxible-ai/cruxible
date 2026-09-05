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

import hashlib
from collections.abc import Mapping, Sequence

from cruxible_client.contracts.captures import (
    CaptureContractV1,
    CaptureEnvelopeAny,
    capture_contract_digest,
    capture_contract_is_self_asserted,
    parse_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_verdicts import (
    EvidenceProvenanceGrade,
    observation_trust_grade,
)
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.declared_blocks import (
    ParsedProjectionBlock,
    ProjectionClaimBackingV1,
    ProjectionMarkerError,
    parse_projection_blocks,
)
from cruxible_client.contracts.errors import PlaybillCasError, ProposalIntegrityError
from cruxible_client.contracts.source_references import (
    ExternalSourceReferenceV1,
    LedgerSourceReferenceV1,
    SourceAccessClass,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.citation_relations import (
    RELATION_SOURCE_USE_SCHEMA,
    logical_source_relation_subject,
)
from cruxible_core.playbill.compiler import artifact_codec_for_compiler
from cruxible_core.playbill.coverage.adapter import (
    WorkingSourceObservationV1,
    build_overlay,
    coverage_span_requests,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageCardBudgetV1,
    CoverageCommitmentMaterializationCorrupt,
    CoverageLineOverlayV1,
    CoverageRequestV1,
    CoverageResultV3,
    LogicalSourceIdentityV1,
    PlaybillCitationWindowObservationV1,
)
from cruxible_core.playbill.coverage.indexes import (
    CaptureCitationInputV1,
    CaptureCitationInputV2,
    CoverageScanBudgetV1,
    EvidenceCitationIndexV1,
    EvidenceCitationIndexV2,
    WorkingOccurrenceOverlayV1,
    WorkingOccurrenceOverlayV2,
    build_evidence_citation_index,
    build_evidence_citation_index_v2,
)
from cruxible_core.playbill.coverage.manifest import (
    COVERAGE_DIRECTORY,
    CoverageManifestBodyV1,
    CoverageManifestBodyV2,
    coverage_manifest_body,
    coverage_manifest_body_v2,
    load_coverage_manifest_file,
    load_coverage_manifest_file_v2,
    write_coverage_manifest,
    write_coverage_manifest_v2,
)
from cruxible_core.playbill.coverage.resolver import (
    BoundPublicationObservation,
    resolve_coverage_v3,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_publications import bound_publication_registrations

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


def build_accepted_evidence_index_v2(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate,
) -> EvidenceCitationIndexV2:
    """Rebuild the association-native index and its verifier-owned trust axis."""

    index, _envelopes = _accepted_evidence_inputs_v2(instance, at=at)
    return index


def _accepted_evidence_inputs_v2(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate,
) -> tuple[EvidenceCitationIndexV2, dict[str, CaptureEnvelopeAny]]:
    """Read authoritative artifacts and retained envelopes once for this request.

    Coverage needs Claim artifacts, not their inspection projections. Keep these
    values request-local so each request still verifies current CAS availability
    and resolves its full accepted coordinate before using the accepted tree.
    """

    at = _resolve_coordinate(instance, at)
    artifact_codec = artifact_codec_for_compiler(instance.coordinate_for_oid(at.git_oid).compiler)
    access = BodyAccessContext(principal_id=COVERAGE_PRINCIPAL, can_read_body=True)
    store = instance.body_store()
    tree = instance.tree_at(at.git_oid)
    contracts: dict[str, CaptureContractV1] = {}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("capture-contracts/"):
            continue
        contract = parse_capture_contract(tree[path], path=path)
        contracts[capture_contract_digest(contract).tagged] = contract

    claims: list[AcceptedClaim] = []
    captures: dict[str, CaptureCitationInputV2] = {}
    for claim_path_value in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not claim_path_value.startswith("claims/"):
            continue
        artifact = parse_claim(tree[claim_path_value], path=claim_path_value, codec=artifact_codec)
        claims.append(
            AcceptedClaim(
                path=claim_path_value,
                claim=artifact,
                statement_digest=claim_statement_digest(artifact.statement).tagged,
                artifact_digest=claim_artifact_digest(artifact).tagged,
            )
        )
        for digest in artifact.backing.capture_digests:
            if digest in captures:
                continue
            envelope = parse_capture_envelope(store.read(digest, access=access))
            accepted_contract = contracts.get(envelope.capture_contract_digest)
            if accepted_contract is None:
                raise ProposalIntegrityError("accepted CaptureContract is unavailable")
            provenance: EvidenceProvenanceGrade = (
                "self-asserted"
                if capture_contract_is_self_asserted(accepted_contract)
                else "daemon-fetched"
            )
            captures[digest] = CaptureCitationInputV2(
                capture_digest=digest,
                envelope=envelope,
                access_class=COVERAGE_EVIDENCE_ACCESS_CLASS,
                observation_trust=observation_trust_grade(provenance),
            )

    index = build_evidence_citation_index_v2(
        at=at,
        captures=tuple(captures[digest] for digest in sorted(captures)),
        claims=tuple(claims),
    )
    return index, {digest: capture.envelope for digest, capture in captures.items()}


def accepted_evidence_sources(
    index: EvidenceCitationIndexV1 | EvidenceCitationIndexV2,
) -> tuple[LogicalSourceIdentityV1, ...]:
    """The logical sources accepted evidence names, in canonical order."""

    seen: dict[bytes, LogicalSourceIdentityV1] = {
        citation.accepted_source.sort_key: citation.accepted_source
        for citation in index.citations
        if citation.accepted_source is not None
    }
    return tuple(seen[key] for key in sorted(seen))


def _materialized_wanted_selections(
    instance: PlaybillInstance,
    *,
    index: EvidenceCitationIndexV2,
    envelopes: Mapping[str, CaptureEnvelopeAny] | None = None,
) -> tuple[tuple[str, int, bytes | None], ...]:
    """Resolve retained exact bytes before entering the pure occurrence scanner.

    A missing retained body is an availability condition and selects the
    exhaustive fallback. A present CAS object that fails its content address is
    an integrity failure and must never be laundered into that fallback.
    """

    access = BodyAccessContext(principal_id=COVERAGE_PRINCIPAL, can_read_body=True)
    store = instance.body_store()
    resolved_envelopes = (
        _capture_envelopes(instance, index=index) if envelopes is None else envelopes
    )
    materialized: list[tuple[str, int, bytes | None]] = []
    for digest, byte_length in index.wanted_selections():
        needle: bytes | None = None
        citations = index.by_commitment(digest)
        capture_digests = sorted(
            {capture for citation in citations for capture in citation.capture_digests}
        )
        for capture_digest in capture_digests:
            envelope = resolved_envelopes[capture_digest]
            if (
                envelope.commitment.digest != digest
                or envelope.commitment.byte_length != byte_length
            ):
                raise ProposalIntegrityError(
                    "coverage index and retained Capture commitment disagree"
                )
            content: bytes | None = None
            if envelope.commitment.materialization == "cas":
                try:
                    metadata = store.metadata(digest, access=access)
                    if not metadata.present:
                        continue
                    content = store.read(digest, access=access)
                except PlaybillCasError as exc:
                    raise CoverageCommitmentMaterializationCorrupt(
                        "retained commitment bytes failed CAS verification"
                    ) from exc
            elif isinstance(envelope.source, LedgerSourceReferenceV1):
                content = instance.tree_at(envelope.source.coordinate.git_oid).get(
                    envelope.source.address.artifact_path
                )
                # A ledger address may select a region whose materializer is not
                # retained independently. Only an exact whole-body reproduction
                # is a usable needle; otherwise the exhaustive route remains sound.
                if content is not None and (
                    len(content) != byte_length
                    or hashlib.sha256(content).hexdigest() != digest.removeprefix("sha256:")
                ):
                    content = None
            if content is None:
                continue
            observed = f"sha256:{hashlib.sha256(content).hexdigest()}"
            if len(content) != byte_length or observed != digest:
                if envelope.commitment.materialization == "cas":
                    raise CoverageCommitmentMaterializationCorrupt(
                        "retained commitment bytes do not reproduce their digest and length"
                    )
                continue
            needle = content
            break
        materialized.append((digest, byte_length, needle))
    return tuple(materialized)


def _citation_window_observations(
    *,
    index: EvidenceCitationIndexV2,
    observations: Sequence[WorkingSourceObservationV1],
    envelopes: Mapping[str, CaptureEnvelopeAny],
    retired_associations: Sequence[tuple[LogicalSourceIdentityV1, str, str]] = (),
) -> tuple[PlaybillCitationWindowObservationV1, ...]:
    """Observe each accepted citation's original window in its named working source."""

    by_source = {item.source.sort_key: item for item in observations}
    source_content: dict[bytes, bytes] = {}
    windows: dict[tuple[bytes, bytes, int, int], PlaybillCitationWindowObservationV1] = {}
    associations: list[tuple[LogicalSourceIdentityV1, str, str, str]] = []
    for citation in index.citations:
        if citation.accepted_source is None or citation.byte_length is None:
            continue
        for association in citation.citation_associations:
            associations.append(
                (
                    citation.accepted_source,
                    association.reference.citation_id,
                    association.capture_digest,
                    citation.commitment_digest,
                )
            )
    for source, citation_id, capture_digest in retired_associations:
        envelope = envelopes.get(capture_digest)
        if envelope is None:
            continue
        associations.append((source, citation_id, capture_digest, envelope.commitment.digest))

    for accepted_source, citation_id, capture_digest, commitment_digest in associations:
        observed = by_source.get(accepted_source.sort_key)
        envelope = envelopes[capture_digest]
        if not isinstance(envelope.source, ExternalSourceReferenceV1):
            continue
        selector = envelope.source.selector
        if not isinstance(selector, Mapping):
            continue
        window = selector.get("working_selection", selector)
        raw_start = window.get("start_byte") if isinstance(window, Mapping) else None
        raw_end = window.get("end_byte") if isinstance(window, Mapping) else None
        if (
            not isinstance(raw_start, int)
            or isinstance(raw_start, bool)
            or not isinstance(raw_end, int)
            or isinstance(raw_end, bool)
            or not 0 <= raw_start <= raw_end
        ):
            continue
        start, end = raw_start, raw_end
        addressable = observed is not None and end <= observed.byte_length
        observed_digest = None
        if addressable and observed is not None:
            source_key = accepted_source.sort_key
            if source_key not in source_content:
                source_content[source_key] = observed.content
            observed_digest = (
                f"sha256:{hashlib.sha256(source_content[source_key][start:end]).hexdigest()}"
            )
        item = PlaybillCitationWindowObservationV1(
            source=accepted_source,
            citation_id=citation_id,
            commitment_digest=commitment_digest,
            original_start=start,
            original_end=end,
            addressable=addressable,
            observed_window_digest=observed_digest,
        )
        key = (
            item.source.sort_key,
            item.citation_id.encode("ascii"),
            item.original_start,
            item.original_end,
        )
        windows[key] = item
    return tuple(windows[key] for key in sorted(windows))


def _retired_citation_window_inputs(
    instance: PlaybillInstance,
    *,
    coordinate: PlaybillAcceptedCoordinate,
    observations: Sequence[WorkingSourceObservationV1],
) -> tuple[tuple[LogicalSourceIdentityV1, str, str], ...]:
    """Read retired citation windows from the coordinate-bound relation projection.

    These inputs extend only the authenticated citation-window observation set.
    They never enter coverage cards, so a retired Claim remains absent from the
    ordinary coverage surface.
    """

    internal = instance.resolve_accepted_coordinate(
        git_oid=coordinate.git_oid,
        semantic_root=coordinate.semantic_root,
        generation_root=coordinate.generation_root,
        compiler_digest=coordinate.compiler_digest,
    )
    inputs: dict[tuple[bytes, bytes], tuple[LogicalSourceIdentityV1, str, str]] = {}
    with instance.bind_accepted_projection(internal) as projection:
        observed_sources = {
            item.source.identity for item in observations if item.source.plane == "external"
        }
        for source_id in sorted(observed_sources, key=lambda item: item.encode("utf-8")):
            for fact in projection.semantic_facts(
                RELATION_SOURCE_USE_SCHEMA,
                subject_identity=logical_source_relation_subject(source_id),
            ):
                value = fact.value
                if not isinstance(value, Mapping):
                    raise ProposalIntegrityError("retired citation relation is malformed")
                if value.get("claim_lifecycle") != "retired":
                    continue
                source = value.get("source")
                capture = value.get("capture_digest")
                citation_id = value.get("citation_id")
                if (
                    not isinstance(source, Mapping)
                    or not isinstance(capture, Mapping)
                    or not isinstance(capture.get("$digest"), str)
                    or not isinstance(citation_id, str)
                ):
                    raise ProposalIntegrityError("retired citation relation is malformed")
                external = ExternalSourceReferenceV1.model_validate(source)
                logical = LogicalSourceIdentityV1(
                    plane="external",
                    identity=external.source_identity,
                )
                item = (logical, citation_id, capture["$digest"])
                inputs[(logical.sort_key, citation_id.encode("ascii"))] = item
    return tuple(inputs[key] for key in sorted(inputs))


def _capture_envelopes(
    instance: PlaybillInstance,
    *,
    index: EvidenceCitationIndexV2,
) -> dict[str, CaptureEnvelopeAny]:
    """Read each retained capture envelope once for both coverage consumers."""

    access = BodyAccessContext(principal_id=COVERAGE_PRINCIPAL, can_read_body=True)
    store = instance.body_store()
    return {
        digest: parse_capture_envelope(store.read(digest, access=access))
        for digest in sorted(
            {capture for citation in index.citations for capture in citation.capture_digests}
        )
    }


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


def _publish_manifest_v2(
    instance: PlaybillInstance,
    *,
    instance_id: str,
    index: EvidenceCitationIndexV2,
    overlay: WorkingOccurrenceOverlayV1 | WorkingOccurrenceOverlayV2,
    access_profile: CoverageAccessProfileV1,
) -> CoverageManifestBodyV2:
    directory = instance.root / COVERAGE_DIRECTORY
    existing = load_coverage_manifest_file_v2(directory)
    epoch = 0 if existing is None else existing.body.epoch + 1
    candidate = coverage_manifest_body_v2(
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
    write_coverage_manifest_v2(directory, candidate)
    return candidate


def _line_overlay(content: bytes, *, start_byte: int, end_byte: int) -> CoverageLineOverlayV1:
    last_byte = max(start_byte, end_byte - 1)
    return CoverageLineOverlayV1(
        start_byte=start_byte,
        end_byte=end_byte,
        start_line=content.count(b"\n", 0, start_byte) + 1,
        end_line=content.count(b"\n", 0, last_byte) + 1,
    )


def _bound_publication_observations(
    instance: PlaybillInstance,
    *,
    at: PlaybillAcceptedCoordinate,
    observations: Sequence[WorkingSourceObservationV1],
) -> tuple[BoundPublicationObservation, ...]:
    """Join confirmed publication protocol state to parsed working blocks."""

    registrations = bound_publication_registrations(instance)
    if registrations is None:
        return ()
    tree = instance.tree_at(at.git_oid)
    by_source = {
        item.source.identity: item for item in observations if item.source.plane == "external"
    }
    parsed_by_source: dict[str, tuple[ParsedProjectionBlock, ...]] = {}
    result: dict[tuple[bytes, bytes, bytes], BoundPublicationObservation] = {}
    for registration in registrations:
        preparation = registration.preparation
        observed = by_source.get(preparation.source_id)
        if observed is None:
            continue
        path = claim_path(registration.claim_identity)
        raw_claim = tree.get(path)
        if raw_claim is None:
            continue
        claim = parse_claim(raw_claim, path=path)
        if (
            claim.lifecycle.state != "live"
            or claim_statement_digest(claim.statement).tagged != registration.claim_statement_digest
            or preparation.body_digest != preparation.stamp.body_digest
        ):
            continue
        expected_backing = ProjectionClaimBackingV1(
            identity=claim.identity,
            statement_digest=registration.claim_statement_digest,
        )
        if expected_backing not in preparation.stamp.backing:
            continue
        if preparation.source_id not in parsed_by_source:
            try:
                parsed_by_source[preparation.source_id] = parse_projection_blocks(
                    observed.content,
                    source_id=preparation.source_id,
                )
            except ProjectionMarkerError:
                parsed_by_source[preparation.source_id] = ()
        matches = tuple(
            block
            for block in parsed_by_source[preparation.source_id]
            if block.block_id == preparation.block_id and block.stamp == preparation.stamp
        )
        if len(matches) != 1:
            continue
        block = matches[0]
        body_start = block.body_start
        body_end = block.body_end
        item = BoundPublicationObservation(
            source=LogicalSourceIdentityV1(
                plane="external",
                identity=preparation.source_id,
            ),
            block_id=preparation.block_id,
            claim_path=path,
            claim_statement_digest=registration.claim_statement_digest,
            expected_body_digest=preparation.body_digest,
            observed_body_digest=block.body_digest,
            line_overlay=_line_overlay(
                observed.content,
                start_byte=body_start,
                end_byte=body_end,
            ),
        )
        result[item.sort_key] = item
    return tuple(result[key] for key in sorted(result))


def service_resolve_playbill_coverage(
    instance: PlaybillInstance,
    *,
    instance_id: str,
    observations: Sequence[WorkingSourceObservationV1],
    at: PlaybillAcceptedCoordinate | None = None,
    budget: CoverageCardBudgetV1 | None = None,
    scan_budget: CoverageScanBudgetV1 | None = None,
) -> CoverageResultV3:
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
    index, envelopes = _accepted_evidence_inputs_v2(instance, at=coordinate)
    retired_associations = _retired_citation_window_inputs(
        instance,
        coordinate=coordinate,
        observations=observations,
    )
    overlay = build_overlay(
        observations,
        wanted=_materialized_wanted_selections(instance, index=index, envelopes=envelopes),
        budget=scan_budget,
    )
    access_profile = coverage_access_profile()
    manifest = _publish_manifest_v2(
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
    return resolve_coverage_v3(
        request,
        index=index,
        overlay=overlay,
        access=access_profile,
        manifest=manifest,
        window_observations=_citation_window_observations(
            index=index,
            observations=observations,
            envelopes=envelopes,
            retired_associations=retired_associations,
        ),
        additional_window_citation_ids=frozenset(
            citation_id for _source, citation_id, _capture in retired_associations
        ),
        publication_observations=_bound_publication_observations(
            instance,
            at=coordinate,
            observations=observations,
        ),
    )


__all__ = [
    "COVERAGE_ACCESS_PROFILE_ID",
    "accepted_evidence_sources",
    "build_accepted_evidence_index",
    "build_accepted_evidence_index_v2",
    "coverage_access_profile",
    "service_resolve_playbill_coverage",
]
