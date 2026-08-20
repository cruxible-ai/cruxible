"""Reading an edited working tree back as typed regions.

Parsing answers one question per region -- *what is this, and has it changed
since the checkout?* -- and it answers it by comparing against the render
**baseline**, never by believing the file. Three separations carry the whole
§11.9.2/§11.9.3 contract here:

*editable dirty is not an error.* An editable region whose body differs from its
baseline is `dirty`, and that is the point of an editable region. It produces a
notice, it invalidates the derived display beside it (see :mod:`.verify`), and
it changes nothing accepted. Editing never silently proposes: compile is a
separate gate.

*derived edited is a refusal, not an interpretation.* A derived region whose
body differs from its baseline is `tampered`, and the diagnostic carries a
**regeneration instruction** rather than a reading of the new text. §11.9.3 is
explicit that tampering with a generated region produces a typed
refusal/regeneration instruction and never an attempted semantic
interpretation, so nothing here tries to guess what the edit meant.

*a locator is a claim about accepted state, not a fact.* Every field a marker
asserts -- its baseline digest, its kind, its address, its artifact digest, its
generation -- is re-checked against the manifest. A locator that names no
baseline region is `unbaselined`: forged, copied from another render, or left
behind by a lens version that no longer applies. A locator that appears twice
anywhere in the tree makes **both** occurrences ambiguous and binds neither.

Region-identity lists are canonically ordered, never presentation-ordered
------------------------------------------------------------------------
`regions` is walked in presentation order -- byte-sorted path, then position
inside the file -- because that is what a person reading a file expects and what
a diagnostic should name. Every accessor that returns *identities* instead
(`dirty_region_ids`, `tampered_region_ids`, `moved_region_ids`) returns them
`byte_sorted`, which is the same canonical ordering the digest-committed stash
body and the render plan already require of themselves.

That is not tidiness. Region identity is path-free by §11.9.3, so an identity
list carrying path order is an identity list carrying presentation coordinates,
and two callers deriving "the dirty regions" by two different routes then
disagree on the *order* of the same digests. A caller comparing or committing to
such a list gets an answer that depends on where the lens happened to place a
Claim -- which for a digest-committed record is nondeterminism, and this format
family exists to refuse exactly that.

Deletion is never inferred
--------------------------
A file in the baseline that is absent from the tree, a region that vanished from
a file, a deleted locator: none of these is retirement. §11.9.3 makes removal a
loss of working projection material only, and withdrawal or supersession always
an explicit disposition, so every absence here is reported as a notice that says
so in its own message.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_core.playbill.coverage.contracts import CoverageLineOverlayV1
from cruxible_core.playbill.native.grammar import (
    DERIVED_REGENERATION_INSTRUCTION,
    NativeDiagnosticV1,
    NativeFileMarkerV1,
    NativeRawRegionV1,
    NativeRegionKind,
    extract_regions,
    locator_lens_matches,
)
from cruxible_core.playbill.native.manifest import (
    NATIVE_RENDER_MANIFEST_PATH,
    NativeRegionBaselineV1,
    NativeRenderManifestV1,
)
from cruxible_core.playbill.query.grammar import byte_sorted
from cruxible_core.playbill.semantic import SemanticAddress

NativeRegionState = Literal["clean", "dirty", "tampered", "ambiguous", "unbaselined"]

_STATE_PRECEDENCE: Mapping[str, int] = {
    "tampered": 0,
    "ambiguous": 1,
    "unbaselined": 2,
    "dirty": 3,
    "clean": 4,
}
"""Which state a file reports for the regions it holds: the worst one."""


class _StrictParseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeParsedRegionV1(_StrictParseModel):
    """One region as it currently is, beside what the checkout said it was."""

    tag: Literal["playbill-native-parsed-region-v1"] = "playbill-native-parsed-region-v1"
    path: str
    region_id: str
    region_kind: NativeRegionKind
    editable: bool
    address: SemanticAddress
    state: NativeRegionState
    baseline_digest: str | None = None
    observed_digest: str
    byte_length: int = Field(ge=0)
    moved_from_path: str | None = None
    line_overlay: CoverageLineOverlayV1
    reason_codes: tuple[str, ...] = ()

    @field_validator("reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("parsed region reason codes must be sorted and unique")
        return value

    @property
    def moved(self) -> bool:
        return self.moved_from_path is not None


class NativeFileParseV1(_StrictParseModel):
    """One parsed file: its marker, its regions, and what it has to refuse."""

    tag: Literal["playbill-native-file-parse-v1"] = "playbill-native-file-parse-v1"
    path: str
    tracked: bool
    marker: NativeFileMarkerV1 | None = None
    regions: tuple[NativeParsedRegionV1, ...] = ()
    diagnostics: tuple[NativeDiagnosticV1, ...] = ()

    @property
    def state(self) -> NativeRegionState:
        worst: NativeRegionState = "clean"
        for item in self.regions:
            if _STATE_PRECEDENCE[item.state] < _STATE_PRECEDENCE[worst]:
                worst = item.state
        return worst

    @property
    def dirty_region_ids(self) -> tuple[str, ...]:
        return byte_sorted(tuple(item.region_id for item in self.regions if item.state == "dirty"))

    @property
    def tampered_region_ids(self) -> tuple[str, ...]:
        return byte_sorted(
            tuple(item.region_id for item in self.regions if item.state == "tampered")
        )


class NativeTreeParseV1(_StrictParseModel):
    """The whole working tree, parsed against one render baseline."""

    tag: Literal["playbill-native-tree-parse-v1"] = "playbill-native-tree-parse-v1"
    files: tuple[NativeFileParseV1, ...] = ()
    diagnostics: tuple[NativeDiagnosticV1, ...] = ()

    @property
    def regions(self) -> tuple[NativeParsedRegionV1, ...]:
        return tuple(region for item in self.files for region in item.regions)

    @property
    def dirty_region_ids(self) -> tuple[str, ...]:
        return byte_sorted(tuple(item.region_id for item in self.regions if item.state == "dirty"))

    @property
    def tampered_region_ids(self) -> tuple[str, ...]:
        return byte_sorted(
            tuple(item.region_id for item in self.regions if item.state == "tampered")
        )

    @property
    def moved_region_ids(self) -> tuple[str, ...]:
        return byte_sorted(tuple(item.region_id for item in self.regions if item.moved))

    @property
    def refusals(self) -> tuple[NativeDiagnosticV1, ...]:
        return tuple(
            item
            for item in (*self.diagnostics, *(d for f in self.files for d in f.diagnostics))
            if item.severity == "refusal"
        )

    def region(self, region_id: str) -> NativeParsedRegionV1 | None:
        for item in self.regions:
            if item.region_id == region_id:
                return item
        return None


def _overlay(region: NativeRawRegionV1) -> CoverageLineOverlayV1:
    return CoverageLineOverlayV1(
        start_byte=region.start_byte,
        end_byte=region.end_byte,
        start_line=max(region.start_line, 1),
        end_line=max(region.end_line, region.start_line, 1),
    )


def _evaluate(
    raw: NativeRawRegionV1,
    *,
    path: str,
    manifest: NativeRenderManifestV1,
    ambiguous: bool,
) -> tuple[NativeParsedRegionV1, tuple[NativeDiagnosticV1, ...]]:
    locator = raw.locator
    found = manifest.baseline_for(locator.region_id)
    diagnostics: list[NativeDiagnosticV1] = []
    reasons: list[str] = []
    moved_from: str | None = None
    baseline: NativeRegionBaselineV1 | None = None
    state: NativeRegionState

    if ambiguous:
        state = "ambiguous"
        reasons.append("locator_duplicated")
    elif found is None:
        state = "unbaselined"
        reasons.append("locator_unknown_region")
        diagnostics.append(
            NativeDiagnosticV1(
                code="locator_unknown_region",
                severity="refusal",
                path=path,
                region_id=locator.region_id,
                message=(
                    "this locator names no region in the render baseline; a locator is "
                    "untrusted until it verifies, and this one does not"
                ),
            )
        )
    else:
        baseline_file, baseline = found
        if baseline_file.path != path:
            # §11.9.3: paths are presentation coordinates. A region that turns up
            # in another file kept its identity and is not a new region.
            moved_from = baseline_file.path
            reasons.append("region_moved")
        mismatch: str | None = None
        if not locator_lens_matches(locator, manifest.lens):
            # Region identity commits to the lens that minted it, so a locator
            # naming another lens is claiming an identity that lens never
            # issued -- the same law `verify_native_locator` refuses on, asked
            # here so the compile path cannot admit what the verifier refuses.
            mismatch = "locator_lens_mismatch"
        elif locator.baseline_digest != baseline.body_digest:
            mismatch = "locator_baseline_mismatch"
        elif locator.region_kind != baseline.region_kind or locator.editable != baseline.editable:
            mismatch = "locator_kind_mismatch"
        elif (
            locator.address != baseline.address
            or locator.artifact_digest != baseline.artifact_digest
        ):
            mismatch = "locator_binding_mismatch"
        elif locator.generation_root != manifest.coordinate.generation_root:
            mismatch = "locator_generation_mismatch"

        if mismatch is not None:
            state = "tampered"
            reasons.append(mismatch)
            diagnostics.append(
                NativeDiagnosticV1(
                    code=mismatch,
                    severity="refusal",
                    path=path,
                    region_id=locator.region_id,
                    message=(
                        "this locator disagrees with the render baseline it claims to come "
                        "from; the baseline stands and the marker does not"
                    ),
                    instruction=DERIVED_REGENERATION_INSTRUCTION,
                )
            )
        elif raw.body_digest == baseline.body_digest:
            state = "clean"
        elif baseline.editable:
            state = "dirty"
            reasons.append("editable_region_edited")
            diagnostics.append(
                NativeDiagnosticV1(
                    code="editable_region_dirty",
                    severity="notice",
                    path=path,
                    region_id=locator.region_id,
                    message=(
                        "this editable field is a local draft; it changes nothing accepted "
                        "until it is compiled and the proposal is accepted"
                    ),
                )
            )
        else:
            state = "tampered"
            reasons.append("derived_region_edited")
            diagnostics.append(
                NativeDiagnosticV1(
                    code="derived_region_tampered",
                    severity="refusal",
                    path=path,
                    region_id=locator.region_id,
                    message=(
                        f"the derived {baseline.region_kind} region was edited; a derived "
                        "field is a projection of accepted state and carries no proposal"
                    ),
                    instruction=DERIVED_REGENERATION_INSTRUCTION,
                )
            )

    return (
        NativeParsedRegionV1(
            path=path,
            region_id=locator.region_id,
            region_kind=locator.region_kind,
            editable=locator.editable,
            address=locator.address,
            state=state,
            baseline_digest=None if baseline is None else baseline.body_digest,
            observed_digest=raw.body_digest,
            byte_length=len(raw.body),
            moved_from_path=moved_from,
            line_overlay=_overlay(raw),
            reason_codes=byte_sorted(tuple(reasons)),
        ),
        tuple(diagnostics),
    )


def parse_native_file(
    path: str,
    content: bytes,
    *,
    manifest: NativeRenderManifestV1,
    ambiguous_region_ids: frozenset[str] = frozenset(),
) -> NativeFileParseV1:
    """Parse one rendered file into typed regions against the render baseline."""

    marker, raw_regions, diagnostics = extract_regions(content, path=path)
    ambiguous = ambiguous_region_ids | {
        item.region_id
        for item in diagnostics
        if item.code == "locator_duplicated" and item.region_id is not None
    }

    regions: list[NativeParsedRegionV1] = []
    collected = list(diagnostics)
    for raw in raw_regions:
        parsed, extra = _evaluate(
            raw,
            path=path,
            manifest=manifest,
            ambiguous=raw.locator.region_id in ambiguous,
        )
        regions.append(parsed)
        collected.extend(extra)

    return NativeFileParseV1(
        path=path,
        tracked=manifest.file_for(path) is not None,
        marker=marker,
        regions=tuple(regions),
        diagnostics=tuple(collected),
    )


def parse_native_tree(
    files: Mapping[str, bytes],
    *,
    manifest: NativeRenderManifestV1,
) -> NativeTreeParseV1:
    """Parse a whole working tree, refusing duplicated locators across files.

    Duplicate detection has to happen at tree scope: one locator in two files is
    exactly the "identical text copied elsewhere" case §11.6.1 refuses to bind,
    and a per-file parse cannot see it. A locator that moved -- present at a new
    path and absent from its old one -- is not a duplicate and keeps its
    identity.
    """

    readable = {
        path: content
        for path, content in files.items()
        if path != NATIVE_RENDER_MANIFEST_PATH and path.endswith(".md")
    }
    occurrences: dict[str, list[str]] = {}
    for path in sorted(readable, key=lambda item: item.encode("utf-8")):
        _marker, raw_regions, _diagnostics = extract_regions(readable[path], path=path)
        for raw in raw_regions:
            occurrences.setdefault(raw.locator.region_id, []).append(path)
    ambiguous = frozenset(region_id for region_id, paths in occurrences.items() if len(paths) > 1)

    diagnostics: list[NativeDiagnosticV1] = [
        NativeDiagnosticV1(
            code="locator_duplicated",
            severity="refusal",
            region_id=region_id,
            message=(
                "one locator appears in "
                + ", ".join(sorted(occurrences[region_id]))
                + "; duplicated locators refuse as ambiguity and none of them is bound"
            ),
        )
        for region_id in sorted(ambiguous)
    ]

    parsed = tuple(
        parse_native_file(
            path,
            readable[path],
            manifest=manifest,
            ambiguous_region_ids=ambiguous,
        )
        for path in sorted(readable, key=lambda item: item.encode("utf-8"))
    )

    present = set(readable)
    for entry in manifest.files:
        if entry.path in present or not entry.path.endswith(".md"):
            continue
        diagnostics.append(
            NativeDiagnosticV1(
                code="rendered_file_absent",
                severity="notice",
                path=entry.path,
                message=(
                    "a rendered file is missing from the working tree; removal deletes "
                    "working projection material only and is never inferred as retirement"
                ),
            )
        )

    return NativeTreeParseV1(files=parsed, diagnostics=tuple(diagnostics))


__all__ = [
    "NativeFileParseV1",
    "NativeParsedRegionV1",
    "NativeRegionState",
    "NativeTreeParseV1",
    "parse_native_file",
    "parse_native_tree",
]
