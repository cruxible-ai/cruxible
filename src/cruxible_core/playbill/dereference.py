"""Coordinate-bound source dereference for ledger, CAS, and external selections.

``open_source`` is a read of one exact, already-cited selection. It never
refreshes a source, never re-selects, and never fabricates a body: an
unavailable, attested-only, or denied selection comes back as metadata plus
explicit coverage, and a caller who may see a handle but not its bytes still
receives the nonsecret source identity, the commitment digests, and the
coverage that says why.

Byte spans are honoured only for a byte-addressable ``exact_bytes`` commitment.
The whole retained content is verified against the commitment first, and only
then is the committed span selection returned, so a span can never be used to
smuggle out bytes the commitment does not cover.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Protocol

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.source_references import (
    BodyAccessResultV1,
    CoverageDescriptorV1,
    ExternalSourceReferenceV1,
    OpenSourceRequestV1,
    SourceDereferenceResultV1,
    SourceHandleV1,
    source_handle_digest,
)

SOURCE_MATERIAL_FACET = "source_material"


class ExternalSelectionReaderProtocol(Protocol):
    """The injectable read-only adapter seam an external dereference calls."""

    def read_external_selection(self, source: ExternalSourceReferenceV1) -> object | None:
        """Return the canonical value at that exact native coordinate, or None."""


class SourceMaterialResolverProtocol(Protocol):
    """The read-only material seam: fetch retained bytes, never write or refresh."""

    def read_ledger(self, artifact_path: str) -> bytes | None:
        """Return the accepted artifact bytes at the bound coordinate, or None."""

    def read_cas(self, content_digest: str, *, access: BodyAccessContext) -> bytes | None:
        """Return retained CAS bytes, or None when the body is no longer present."""

    def read_external(self, source: ExternalSourceReferenceV1) -> object | None:
        """Return the exact external selection's canonical value, or None if retired.

        This is a read-only adapter call at the recorded native coordinate and
        selector. It never re-selects, never widens, and never mints a Capture.
        """


def _coverage(
    *,
    available: bool = False,
    omitted_for_access: bool = False,
    truncated: bool = False,
    reason_codes: tuple[str, ...] = (),
) -> CoverageDescriptorV1:
    return CoverageDescriptorV1(
        requested_facets=(SOURCE_MATERIAL_FACET,),
        available_facets=(SOURCE_MATERIAL_FACET,) if available else (),
        omitted_for_access=(SOURCE_MATERIAL_FACET,) if omitted_for_access else (),
        truncated_facets=(SOURCE_MATERIAL_FACET,) if truncated else (),
        reason_codes=tuple(sorted(set(reason_codes), key=lambda item: item.encode("utf-8"))),
    )


def _metadata_only(
    handle: SourceHandleV1,
    *,
    status: str,
    coverage: CoverageDescriptorV1,
) -> SourceDereferenceResultV1:
    return SourceDereferenceResultV1(
        source_handle_digest=source_handle_digest(handle),
        status=status,  # type: ignore[arg-type]
        commitment_verified=False,
        material_kind="metadata_only",
        coverage=coverage,
    )


def _selected_bytes(handle: SourceHandleV1, content: bytes) -> tuple[bytes, bool]:
    """Return the committed span selection, or the whole content when unspanned."""

    if not handle.exact_spans:
        return content, False
    return (
        b"".join(content[span.start_byte : span.end_byte] for span in handle.exact_spans),
        True,
    )


def dereference_source_handle(
    request: OpenSourceRequestV1,
    *,
    access: BodyAccessContext,
    resolver: SourceMaterialResolverProtocol,
) -> SourceDereferenceResultV1:
    """Dereference one exact source handle without mutating or refreshing anything."""

    handle = request.source_handle
    if handle.access_class == "restricted" and not access.can_read_body:
        return _metadata_only(
            handle,
            status="denied",
            coverage=_coverage(omitted_for_access=True, reason_codes=("restricted_access_class",)),
        )
    if isinstance(handle.source, ExternalSourceReferenceV1):
        return _dereference_external(handle, handle.source, request=request, resolver=resolver)
    if not access.can_read_body:
        return _metadata_only(
            handle,
            status="denied",
            coverage=_coverage(omitted_for_access=True),
        )

    content = (
        resolver.read_cas(handle.source.content_digest, access=access)
        if handle.source.kind == "cas"
        else resolver.read_ledger(handle.source.address.artifact_path)
    )
    if content is None:
        return _metadata_only(
            handle,
            status="unavailable",
            coverage=_coverage(reason_codes=("body_unavailable",)),
        )
    observed = Sha256Value(hashlib.sha256(content).hexdigest()).tagged

    selection, spanned = _selected_bytes(handle, content)
    if len(selection) + request.structural_context_bytes > request.resource_budget_bytes:
        return _metadata_only(
            handle,
            status="unavailable",
            coverage=_coverage(truncated=True, reason_codes=("resource_budget_exceeded",)),
        )
    verified = observed == handle.commitment.digest
    reason_codes = ("exact_span_selection",) if spanned else ()
    return SourceDereferenceResultV1(
        source_handle_digest=source_handle_digest(handle),
        status="verified" if verified else "drifted",
        commitment_verified=verified,
        observed_commitment_digest=observed,
        material_kind="bytes",
        body_access=BodyAccessResultV1(
            status="available",
            content_digest=handle.commitment.digest,
            byte_length=len(selection),
            body_base64=base64.b64encode(selection).decode("ascii"),
        ),
        coverage=_coverage(available=True, reason_codes=reason_codes),
    )


def _dereference_external(
    handle: SourceHandleV1,
    source: ExternalSourceReferenceV1,
    *,
    request: OpenSourceRequestV1,
    resolver: SourceMaterialResolverProtocol,
) -> SourceDereferenceResultV1:
    """Read the exact native selection back and compare it to the commitment."""

    if source.replayability == "attested_only":
        # There is no exact retained version to read, so the honest answer is the
        # retained commitment and proof grade rather than a refetched value.
        return _metadata_only(
            handle,
            status="attested_only",
            coverage=_coverage(reason_codes=("external_attested_only",)),
        )
    material = resolver.read_external(source)
    if material is None:
        return _metadata_only(
            handle,
            status="unavailable",
            coverage=_coverage(reason_codes=("external_version_retired",)),
        )
    encoded = canonical_bytes(material)
    if len(encoded) + request.structural_context_bytes > request.resource_budget_bytes:
        return _metadata_only(
            handle,
            status="unavailable",
            coverage=_coverage(truncated=True, reason_codes=("resource_budget_exceeded",)),
        )
    observed = Sha256Value(hashlib.sha256(encoded).hexdigest()).tagged
    verified = observed == handle.commitment.digest
    return SourceDereferenceResultV1(
        source_handle_digest=source_handle_digest(handle),
        status="verified" if verified else "drifted",
        commitment_verified=verified,
        observed_commitment_digest=observed,
        material_kind=(
            "query_result" if handle.commitment.digest_kind == "query_result" else "canonical_value"
        ),
        canonical_material=material,
        coverage=_coverage(available=True, reason_codes=("external_exact_replay",)),
    )


__all__ = [
    "SOURCE_MATERIAL_FACET",
    "ExternalSelectionReaderProtocol",
    "SourceMaterialResolverProtocol",
    "dereference_source_handle",
]
