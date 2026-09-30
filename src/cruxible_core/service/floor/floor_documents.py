"""Accepted Documents as readable files: ``documents/<name>.<ext>``.

Each file is a one-line header (the ``get`` ref, title, kind, media type and
the floor coordinate) followed by the Document's body as it is, so its text
greps like any other file. A Document body keeps its own read boundary
(``cruxible_playbill_body_read``): an export whose caller may not read bodies
writes the header and a line saying how to read the body instead. A body that
is not UTF-8 text shows a typed marker with its size, and a very long body is
cut at a line boundary with the exact ``get`` range that reads the rest.

The Document's envelope, with its digests, is provenance and never lands here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.primitives import pretty_json
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillDocumentView,
    service_list_playbill_documents,
)
from cruxible_core.service.floor.floor_current import FloorStamp, yaml_scalar
from cruxible_core.storage.cas import BodyAccessContext

DOCUMENTS_PREFIX = "documents/"
PROVENANCE_DOCUMENTS_PREFIX = "provenance/documents/"
# A body longer than this is cut at a line boundary, with the range that reads on.
MAX_DOCUMENT_BODY_BYTES = 256 * 1024
_EXTENSIONS = {"text/markdown": ".md", "application/json": ".json"}


@dataclass(frozen=True)
class DocumentPart:
    """One Document as the floor renders it, apart from its stamp."""

    name: str
    extension: str
    label: str
    body: str
    provenance: bytes
    # The body-store object this render read, and whether it was held intact;
    # None when no body was read (withheld from this caller, or none named).
    read_body: tuple[str, bool] | None = None


def _fact(view: PlaybillDocumentView, schema_id: str, fact_key: str) -> object:
    for fact in view.facts:
        if fact.get("schema_id") == schema_id and fact.get("fact_key") == fact_key:
            return fact.get("value")
    return None


def _body_text(
    instance: PlaybillInstance,
    *,
    ref: str,
    digest: str | None,
    access: BodyAccessContext,
) -> str:
    if digest is None:
        return "(body unavailable: the accepted Document names no body)\n"
    if not access.can_read_body:
        return (
            "(body withheld: reading Document bodies needs the governed_write tier; "
            f"read it with: get {ref} --detail body)\n"
        )
    try:
        content = instance.body_store().read(digest, access=access)
    except (PlaybillError, OSError, ValueError):
        return f"(body unavailable: the body store no longer holds it; get {ref})\n"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None or "\x00" in text:
        return f"(binary body, {len(content)} bytes; get {ref} --detail body)\n"
    if len(content) <= MAX_DOCUMENT_BODY_BYTES:
        return text
    cut = content[:MAX_DOCUMENT_BODY_BYTES]
    end = cut.rfind(b"\n") + 1 or len(cut)
    kept = cut[:end].decode("utf-8", errors="ignore")
    shown = len(kept.encode("utf-8"))
    return kept + (
        f"\n(truncated at {shown} of {len(content)} bytes; read on with: "
        f"get {ref} --detail body --range {shown}:{len(content)})\n"
    )


def render_document(
    instance: PlaybillInstance,
    view: PlaybillDocumentView,
    *,
    access: BodyAccessContext,
) -> DocumentPart:
    envelope = view.envelope
    identity = str(envelope["identity"])
    name = identity.split(":", 1)[-1]
    ref = f"Document:{name}"
    metadata = _fact(view, "playbill.document.metadata", "metadata")
    subject = _fact(view, "playbill.document.subject", "whole_document")
    meta = metadata if isinstance(metadata, Mapping) else {}
    media_type = str(meta.get("media_type") or "text/plain")
    digest_value = subject.get("body_digest") if isinstance(subject, Mapping) else None
    digest = digest_value.get("$digest") if isinstance(digest_value, Mapping) else None
    label = "  ".join(
        (
            ref,
            f"title={yaml_scalar(str(meta.get('title') or ''))}",
            f"kind={meta.get('document_kind') or 'document'}",
            f"media={media_type}",
        )
    )
    named = digest if isinstance(digest, str) else None
    body = _body_text(instance, ref=ref, digest=named, access=access)
    read_body = (
        None
        if named is None or not access.can_read_body
        else (named, not body.startswith("(body unavailable:"))
    )
    provenance = (
        pretty_json(
            json.loads(
                canonical_bytes(
                    {
                        "document": ref,
                        "file": f"{DOCUMENTS_PREFIX}{name}{_EXTENSIONS.get(media_type, '.txt')}",
                        "envelope": envelope,
                        "metadata": dict(meta),
                        "body_digest": digest,
                    }
                )
            )
        ).encode("utf-8")
        + b"\n"
    )
    return DocumentPart(
        name=name,
        extension=_EXTENSIONS.get(media_type, ".txt"),
        label=label,
        body=body if body.endswith("\n") or not body else body + "\n",
        provenance=provenance,
        read_body=read_body,
    )


def document_parts(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    access: BodyAccessContext,
) -> tuple[DocumentPart, ...]:
    listing = service_list_playbill_documents(
        instance, access=access, at=PlaybillAcceptedCoordinate.from_internal(coordinate)
    )
    return tuple(render_document(instance, view, access=access) for view in listing.documents)


def document_files(part: DocumentPart, stamp: FloorStamp) -> dict[str, bytes]:
    header = f"# {part.label}  {stamp.at}\n"
    return {
        f"{DOCUMENTS_PREFIX}{part.name}{part.extension}": (header + part.body).encode("utf-8"),
        f"{PROVENANCE_DOCUMENTS_PREFIX}{part.name}.json": part.provenance,
    }


__all__ = [
    "DOCUMENTS_PREFIX",
    "MAX_DOCUMENT_BODY_BYTES",
    "PROVENANCE_DOCUMENTS_PREFIX",
    "DocumentPart",
    "document_files",
    "document_parts",
    "render_document",
]
