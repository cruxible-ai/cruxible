"""Status, and the explicit-sync law that keeps a re-render from eating edits.

§11.9.5 makes regeneration **explicit-sync only**: a daemon may detect affected
scopes, compute a prospective render, and notify -- it may never commit or push
into a user repository, and rendering and committing stay separate operations
even for the invoking actor. That law is structural in this package rather than
promised: :func:`~cruxible_core.playbill.native.lens.render_native_tree` returns
bytes and :func:`plan_native_render` returns a plan. Neither touches a
filesystem, so there is no seam through which a render could write one.

The second half of the law is that a re-render never overwrites dirty regions
without an explicit stash or discard. :func:`plan_native_render` refuses by
default, naming every dirty region and the file it is in, and proceeds only when
the caller passes ``discard=True`` -- an act, not a flag that happens to be set.
Stash mechanics are deliberately minimal here: the plan reports exactly what
would be lost, which is what a stash needs to capture, and S3 can carry those
bytes somewhere instead of dropping them without changing this refusal.

Tampered and ambiguous regions do not block a re-render. A derived region that
was edited has nothing to preserve -- it regenerates by definition -- and a
duplicated locator is already refusing to bind anything, so regenerating is the
repair rather than the risk.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.native.context import RenderContextV1
from cruxible_core.playbill.native.grammar import NativeDiagnosticV1, NativeRenderError
from cruxible_core.playbill.native.lens import NativeRenderV1
from cruxible_core.playbill.native.manifest import (
    NATIVE_RENDER_MANIFEST_PATH,
    NativeFileDisposition,
    NativeRenderManifestV1,
)
from cruxible_core.playbill.native.parse import (
    NativeRegionState,
    NativeTreeParseV1,
    parse_native_tree,
)
from cruxible_core.playbill.query.grammar import byte_sorted


class NativeSyncRefusal(NativeRenderError):
    """A re-render would overwrite local edits and was refused."""


def render_context_from_manifest(manifest: NativeRenderManifestV1) -> RenderContextV1:
    """Recover the render context a committed tree was produced under.

    A committed render binds every context field, so the context does not have
    to be carried alongside the directory or guessed at: reading the manifest is
    enough to ask the resolver the same question the render was an answer to.
    """

    return RenderContextV1(
        instance_id=manifest.instance_id,
        at=manifest.coordinate,
        evaluation_time=manifest.evaluation_time,
        scope=manifest.scope_kind,
        scope_query_name=manifest.scope_query_name,
        scope_digest=manifest.scope_digest,
        access_profile=CoverageAccessProfileV1(profile_id=manifest.access_profile_id),
        lens=manifest.lens,
    )


class _StrictSyncModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeFileStatusV1(_StrictSyncModel):
    """One file's status, in the shape `playbill native status` prints."""

    tag: Literal["playbill-native-file-status-v1"] = "playbill-native-file-status-v1"
    path: str
    tracked: bool
    disposition: NativeFileDisposition | None = None
    state: NativeRegionState
    region_count: int = Field(ge=0)
    clean_regions: int = Field(ge=0)
    dirty_regions: int = Field(ge=0)
    tampered_regions: int = Field(ge=0)
    ambiguous_regions: int = Field(ge=0)
    unbaselined_regions: int = Field(ge=0)
    moved_regions: int = Field(ge=0)
    baseline_generation_root: str


class NativeTreeStatusV1(_StrictSyncModel):
    """The whole working tree against the baseline it was rendered from."""

    tag: Literal["playbill-native-tree-status-v1"] = "playbill-native-tree-status-v1"
    instance_id: str
    baseline_generation_root: str
    lens_id: str
    lens_version: int = Field(ge=1)
    renderer_digest: str
    render_digest: str
    evaluation_time: str
    files: tuple[NativeFileStatusV1, ...] = ()
    missing_paths: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    diagnostics: tuple[NativeDiagnosticV1, ...] = ()

    @field_validator("missing_paths", "untracked_paths")
    @classmethod
    def _sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("status path lists must be sorted and unique")
        return value

    @property
    def dirty(self) -> bool:
        return any(item.dirty_regions for item in self.files)

    @property
    def tampered(self) -> bool:
        return any(item.tampered_regions for item in self.files)


class NativeRenderPlanV1(_StrictSyncModel):
    """What an explicit sync would do, computed but never performed."""

    tag: Literal["playbill-native-render-plan-v1"] = "playbill-native-render-plan-v1"
    write_paths: tuple[str, ...] = ()
    unchanged_paths: tuple[str, ...] = ()
    delete_paths: tuple[str, ...] = ()
    discarded_region_ids: tuple[str, ...] = ()
    stash_required: bool = False

    @field_validator("write_paths", "unchanged_paths", "delete_paths", "discarded_region_ids")
    @classmethod
    def _sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("render plan lists must be sorted and unique")
        return value


def native_status(
    files: Mapping[str, bytes],
    *,
    manifest: NativeRenderManifestV1,
    parsed: NativeTreeParseV1 | None = None,
) -> NativeTreeStatusV1:
    """Report per-file clean/dirty/tampered state against the render baseline."""

    tree = parsed or parse_native_tree(files, manifest=manifest)
    dispositions = {item.path: item.disposition for item in manifest.files}

    statuses: list[NativeFileStatusV1] = []
    for item in tree.files:
        counts = {
            state: sum(1 for region in item.regions if region.state == state)
            for state in ("clean", "dirty", "tampered", "ambiguous", "unbaselined")
        }
        statuses.append(
            NativeFileStatusV1(
                path=item.path,
                tracked=item.tracked,
                disposition=dispositions.get(item.path),
                state=item.state,
                region_count=len(item.regions),
                clean_regions=counts["clean"],
                dirty_regions=counts["dirty"],
                tampered_regions=counts["tampered"],
                ambiguous_regions=counts["ambiguous"],
                unbaselined_regions=counts["unbaselined"],
                moved_regions=sum(1 for region in item.regions if region.moved),
                baseline_generation_root=manifest.coordinate.generation_root,
            )
        )

    present = {item.path for item in tree.files}
    rendered = {item.path for item in manifest.files if item.path.endswith(".md")}
    return NativeTreeStatusV1(
        instance_id=manifest.instance_id,
        baseline_generation_root=manifest.coordinate.generation_root,
        lens_id=manifest.lens.lens_id,
        lens_version=manifest.lens.lens_version,
        renderer_digest=manifest.lens.renderer_digest,
        render_digest=manifest.render_digest,
        evaluation_time=manifest.evaluation_time.isoformat(),
        files=tuple(statuses),
        missing_paths=byte_sorted(tuple(rendered - present)),
        untracked_paths=byte_sorted(tuple(present - rendered)),
        diagnostics=tuple(
            (*tree.diagnostics, *(item for file in tree.files for item in file.diagnostics))
        ),
    )


def plan_native_render(
    files: Mapping[str, bytes],
    *,
    manifest: NativeRenderManifestV1,
    render: NativeRenderV1,
    discard: bool = False,
) -> NativeRenderPlanV1:
    """Plan an explicit sync, refusing to overwrite dirty regions without consent.

    The refusal names every dirty region and its file, because "there are local
    edits" is not an answer a caller can act on and "these three fields in these
    two files" is.
    """

    tree = parse_native_tree(files, manifest=manifest)
    dirty = tree.dirty_region_ids
    if dirty and not discard:
        located = sorted(
            f"{region.path}#{region.region_kind}"
            for region in tree.regions
            if region.state == "dirty"
        )
        raise NativeSyncRefusal(
            "re-rendering would overwrite "
            f"{len(dirty)} dirty region(s): {', '.join(located)}. "
            "Stash or discard them explicitly; a re-render never overwrites local edits."
        )

    next_paths = set(render.files)
    writes = byte_sorted(
        tuple(path for path in next_paths if files.get(path) != render.files[path])
    )
    unchanged = byte_sorted(
        tuple(path for path in next_paths if files.get(path) == render.files[path])
    )
    stale = {item.path for item in manifest.files} | {NATIVE_RENDER_MANIFEST_PATH}
    deletes = byte_sorted(tuple(path for path in stale - next_paths if path in files))
    return NativeRenderPlanV1(
        write_paths=writes,
        unchanged_paths=unchanged,
        delete_paths=deletes,
        discarded_region_ids=byte_sorted(dirty) if discard else (),
        stash_required=bool(dirty),
    )


__all__ = [
    "NativeFileStatusV1",
    "NativeRenderPlanV1",
    "NativeSyncRefusal",
    "NativeTreeStatusV1",
    "native_status",
    "plan_native_render",
    "render_context_from_manifest",
]
