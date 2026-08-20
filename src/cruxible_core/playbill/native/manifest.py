"""The render manifest: a profile of the §11.6.3 family, never a second schema.

§11.9.5 requires the committed render to bind instance, accepted generation,
query/scope digest, access profile, renderer digest, evaluation time, and
per-file baseline digests, and to inherit **every** §11.6.3 field rather than
minting a manifest schema of its own. That inheritance is literal here:
:class:`NativeRenderManifestV1` subclasses
:class:`~cruxible_core.playbill.coverage.contracts.CoverageManifestProfileV1`,
the same base the F2-S2 floor boundary subclasses, so the two profiles cannot
drift into two vocabularies.

What the render adds to the family
----------------------------------
* the **lens** and its renderer digest, so a spelling change is legible;
* the **evaluation time** governance facts were qualified by;
* the **per-file baselines** -- every rendered file's content digest and every
  region's identity, kind, address, and body digest. These are the authoritative
  copy: an in-file locator carries the same baseline digest for convenience, and
  a disagreement between the two is a tampered marker rather than a new fact.
* the **disposition** of each file (§11.9.4's native/foreign guard, projected
  redundantly outward). Only ``native_editable`` files carry compilable regions.

"Valid at G" versus "G is current"
----------------------------------
The manifest states the first and never the second. It binds the generation it
was rendered from and the coverage completeness of that boundary; whether G is
still the accepted head is a live question the resolver answers, not something a
committed file can assert about the future.

Like the floor's boundary, a render observes no working snapshot: it declares no
epoch and proves no freshness. Freshness is what happens *after* the files are
written, and it is the coverage manifest's job, not this one's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.coverage.contracts import (
    CoverageManifestProfileV1,
    LogicalSourceIdentityV1,
)
from cruxible_core.playbill.native.grammar import (
    NATIVE_REGION_EDITABLE,
    NativeLensV1,
    NativeRegionKind,
    NativeRenderError,
)
from cruxible_core.playbill.query.grammar import byte_sorted
from cruxible_core.playbill.semantic import SemanticAddress

NATIVE_RENDER_MANIFEST_PATH: Final = "render-manifest.json"
NATIVE_RENDER_DIGEST_DOMAIN: Final = "playbill-native-render-v1"

NativeFileDisposition = Literal["native_editable", "foreign_observed", "orientation"]


class _StrictManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeRegionBaselineV1(_StrictManifestModel):
    """One region as rendered: its identity, its type, and the bytes it held."""

    tag: Literal["playbill-native-region-baseline-v1"] = "playbill-native-region-baseline-v1"
    region_id: str
    region_kind: NativeRegionKind
    editable: bool
    address: SemanticAddress
    artifact_digest: str
    body_digest: str
    byte_length: int = Field(ge=0)

    @model_validator(mode="after")
    def _baseline_law(self) -> "NativeRegionBaselineV1":
        for value in (self.region_id, self.artifact_digest, self.body_digest):
            Sha256Value.from_tagged(value)
        if self.editable != NATIVE_REGION_EDITABLE[self.region_kind]:
            raise ValueError("a region baseline may not disagree with the typed split")
        return self

    @property
    def sort_key(self) -> bytes:
        return self.region_id.encode("ascii")


class NativeRenderFileV1(_StrictManifestModel):
    """One rendered file: its presentation path, its bytes, and its regions.

    ``source`` is the declared logical source binding this file is observed
    under. Coverage never infers a binding from a filename (§11.6.1), so the
    render declares it here and the freshness path reads it from the manifest
    rather than guessing one from the path it happens to be written to.
    """

    tag: Literal["playbill-native-render-file-v1"] = "playbill-native-render-file-v1"
    path: str
    content_digest: str
    byte_length: int = Field(ge=0)
    disposition: NativeFileDisposition
    source: LogicalSourceIdentityV1
    regions: tuple[NativeRegionBaselineV1, ...] = ()

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value or value != value.strip() or value.startswith("/"):
            raise ValueError("a rendered path must be a non-empty relative POSIX path")
        if ".." in value.split("/"):
            raise ValueError("a rendered path may not traverse upward")
        return value

    @field_validator("regions")
    @classmethod
    def _regions(
        cls, value: tuple[NativeRegionBaselineV1, ...]
    ) -> tuple[NativeRegionBaselineV1, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("rendered regions must be sorted and unique by region identity")
        return value

    @model_validator(mode="after")
    def _file_law(self) -> "NativeRenderFileV1":
        Sha256Value.from_tagged(self.content_digest)
        if self.disposition != "native_editable" and any(item.editable for item in self.regions):
            raise ValueError("only a native_editable file may carry editable regions")
        return self


class NativeRenderManifestV1(CoverageManifestProfileV1):
    """The committed render's boundary and baseline, in the §11.6.3 vocabulary."""

    tag: Literal["playbill-native-render-manifest-v1"] = "playbill-native-render-manifest-v1"
    lens: NativeLensV1
    evaluation_time: datetime
    scope: tuple[LogicalSourceIdentityV1, ...] = ()
    scope_kind: str
    scope_digest: str
    scope_query_name: str | None = None
    render_roots: tuple[str, ...] = ()
    orientation_entrypoints: tuple[str, ...] = ()
    files: tuple[NativeRenderFileV1, ...] = ()
    render_digest: str

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("a render manifest evaluation time must be an absolute instant")
        return value

    @field_validator("render_roots", "orientation_entrypoints")
    @classmethod
    def _sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("render roots and entrypoints must be sorted and unique")
        return value

    @field_validator("files")
    @classmethod
    def _files(cls, value: tuple[NativeRenderFileV1, ...]) -> tuple[NativeRenderFileV1, ...]:
        paths = tuple(item.path.encode("utf-8") for item in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("rendered files must be byte-sorted and unique by path")
        return value

    @model_validator(mode="after")
    def _manifest_law(self) -> "NativeRenderManifestV1":
        for value in (self.scope_digest, self.render_digest):
            Sha256Value.from_tagged(value)
        if self.epoch is not None or self.watcher_health != "absent":
            raise ValueError("a render observes no working snapshot and proves no epoch")
        if self.render_digest != native_render_digest(self.files):
            raise ValueError("a render digest must reproduce from its own file inventory")
        seen: set[str] = set()
        for item in self.files:
            for region in item.regions:
                if region.region_id in seen:
                    raise ValueError(
                        "a region identity appears in two rendered files; duplicated "
                        "locators refuse as ambiguity"
                    )
                seen.add(region.region_id)
        return self

    def file_for(self, path: str) -> NativeRenderFileV1 | None:
        for item in self.files:
            if item.path == path:
                return item
        return None

    def baseline_for(
        self, region_id: str
    ) -> tuple[NativeRenderFileV1, NativeRegionBaselineV1] | None:
        for item in self.files:
            for region in item.regions:
                if region.region_id == region_id:
                    return item, region
        return None

    @property
    def editable_region_count(self) -> int:
        return sum(1 for item in self.files for region in item.regions if region.editable)

    @property
    def derived_region_count(self) -> int:
        return sum(1 for item in self.files for region in item.regions if not region.editable)


def native_render_digest(files: tuple[NativeRenderFileV1, ...]) -> str:
    """Digest the whole rendered inventory, baselines included."""

    return typed_digest(
        Sha256Value,
        NATIVE_RENDER_DIGEST_DOMAIN,
        {"files": [item.model_dump(mode="json") for item in files]},
    ).tagged


def render_native_manifest_bytes(manifest: NativeRenderManifestV1) -> bytes:
    """Serialize the manifest the way every other Playbill projection file is."""

    return canonical_bytes(manifest.model_dump(mode="json")) + b"\n"


def parse_native_manifest(
    content: bytes,
    *,
    path: str = NATIVE_RENDER_MANIFEST_PATH,
) -> NativeRenderManifestV1:
    """Read a committed render manifest, refusing one that does not reproduce."""

    try:
        return NativeRenderManifestV1.model_validate_json(content)
    except ValueError as exc:
        raise NativeRenderError(f"native render manifest at {path} is malformed: {exc}") from exc


__all__ = [
    "NATIVE_RENDER_DIGEST_DOMAIN",
    "NATIVE_RENDER_MANIFEST_PATH",
    "NativeFileDisposition",
    "NativeRegionBaselineV1",
    "NativeRenderFileV1",
    "NativeRenderManifestV1",
    "native_render_digest",
    "parse_native_manifest",
    "render_native_manifest_bytes",
]
