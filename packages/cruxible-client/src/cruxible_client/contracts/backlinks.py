"""The backlinks read: every pointer into one target (D-R12, D-R17).

Name the target one way:

- ``ref`` a Subject (``kind/id``): the notes about it, Documents linked
  ``about`` it, and the workspace sections its live Claims cite;
- ``ref`` a ClaimType or Procedure: the governed Documents that ``guides`` it
  (review guidance);
- ``file`` and ``section``: the live Claims resting on that workspace section.

A page is one keyset range of the backlink index, bounded by ``limit``; the
``next_cursor`` of a cut page continues it at the same coordinate. A section
row says whether the section moved since it was cited: ``changed_since_cited``
when accepted state holds a later observation of that section with other bytes,
or (from an adapter beside the workspace) when the file's section differs now;
``unchanged`` only from such an adapter; otherwise ``unknown`` -- the daemon
never reads workspace files.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts import AcceptedCoordinate
from cruxible_client.contracts.doc_sections import SectionPath
from cruxible_client.contracts.get_reads import GetCoordinate, ReadSurface

BACKLINKS_MAX_LIMIT = 100
BacklinkRelation = Literal["cites", "about", "guides"]
BacklinkSourceKind = Literal["Claim", "Note", "Document"]
SectionDrift = Literal["changed_since_cited", "unchanged", "unknown"]


class _StrictBacklinkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BacklinksRequest(_StrictBacklinkModel):
    tag: Literal["playbill-backlinks-request-v1"] = "playbill-backlinks-request-v1"
    ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="A Subject (kind/id), ClaimType (ClaimType:p or p) or Procedure:name.",
    )
    file: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="With section: a catalogued workspace file (path or files.yaml name).",
    )
    section: SectionPath | None = Field(default=None, min_length=1)
    limit: int = Field(default=25, ge=1, le=BACKLINKS_MAX_LIMIT)
    cursor: str | None = Field(default=None, min_length=1)
    at: AcceptedCoordinate | str | None = None
    surface: ReadSurface = "mcp"

    @model_validator(mode="after")
    def _one_target(self) -> BacklinksRequest:
        if (self.ref is None) == (self.file is None):
            raise ValueError("backlinks names ref, or file and section")
        if (self.file is None) != (self.section is None):
            raise ValueError("a section target names both file and section")
        return self


class Backlink(_StrictBacklinkModel):
    """One pointer into the target."""

    relation: BacklinkRelation
    source_kind: BacklinkSourceKind
    source: str = Field(description="CLM-…, NOTE-… or the Document's identity.")
    title: str | None = Field(default=None, description="A Document's title.")
    subject: str | None = Field(default=None, description="A citing Claim's Subject (kind/id).")
    field: str | None = Field(default=None, description="A citing Claim's predicate.")
    role: str | None = Field(default=None, description="A citation's role: evidence or copy.")
    file: str | None = Field(default=None, description="The workspace source (files.yaml name).")
    section: SectionPath | None = None
    cited_digest: str | None = Field(default=None, description="The section's bytes when cited.")
    drift: SectionDrift | None = Field(
        default=None, description="Section rows: has the section moved since it was cited?"
    )


class BacklinksResult(_StrictBacklinkModel):
    tag: Literal["playbill-backlinks-result-v1"] = "playbill-backlinks-result-v1"
    target: str = Field(description="What the links point into, as resolved.")
    coordinate: GetCoordinate
    links: tuple[Backlink, ...]
    truncated: bool = False
    next_cursor: str | None = None


__all__ = [
    "BACKLINKS_MAX_LIMIT",
    "Backlink",
    "BacklinkRelation",
    "BacklinkSourceKind",
    "BacklinksRequest",
    "BacklinksResult",
    "SectionDrift",
]
