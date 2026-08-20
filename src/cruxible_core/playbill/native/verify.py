"""Verifying locators, and invalidating derived display through the F2 resolver.

Two jobs, deliberately separated because they answer different questions.

**Is this locator true?** :func:`verify_native_locator` resolves what a marker
asserts against accepted state and the render baseline: does the address name an
accepted artifact, does the artifact digest still match, does the generation
agree, is the region in the baseline at all. §11.6.5's rule is that an inline
marker is an untrusted locator rather than a signature, so nothing a marker says
is believed and a copied or attacker-authored one verifies to a refusal with a
named reason. This is the address/digest/coordinate question, which the coverage
resolver does not answer and is not asked to.

**Has the local material drifted from what the derived display describes?** That
*is* the coverage resolver's question, and it is answered by feeding it, not by
extending it. The rendered file is a working source; the render baseline is
accepted-state-derived evidence about exactly which bytes that source held at
generation G; and the F2 machinery already knows how to say "that cited content
is no longer here." So this module builds a disposable
:class:`EvidenceCitationIndexV1` from the baseline, observes the working files
through :func:`coverage.adapter.build_overlay`, and calls
:func:`resolve_coverage` unchanged.

The result falls out with the semantics §11.9.2 asks for, for free:

* a clean region's baseline bytes are found in its source -- `exact`;
* a region that **moved** within its file is still found -- still `exact`,
  because line movement never breaks a match (§11.6.1), which is the same law
  that makes paths presentation coordinates;
* an **edited** editable region's baseline bytes are gone and were looked for --
  `drifted`, on a card naming the Claim whose derived display beside it no
  longer applies;
* the same bytes copied into another rendered file are a labeled
  `content_equivalent` candidate, never an inherited match;
* and every card is structurally `grants_mutation_authority=False`, so a dirty
  region never launders an accepted verdict onto local material.

The citation's dereference handle is the region's own locator digest: a locator
is precisely the handle by which a rendered occurrence is dereferenced back to
accepted state, and naming it keeps a drift card checkable without inventing a
Capture that the ledger never accepted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.coverage.adapter import (
    WorkingSourceObservationV1,
    build_overlay,
    observe_working_source,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageCardBudgetV1,
    CoverageRequestV1,
    CoverageResultV1,
    CoverageSelectionV1,
    CoverageSpanRequestV1,
)
from cruxible_core.playbill.coverage.indexes import (
    EvidenceCitationIndexV1,
    EvidenceCitationV1,
    WorkingOccurrenceOverlayV1,
    WorkingSourceCommitmentV1,
)
from cruxible_core.playbill.coverage.manifest import (
    CoverageManifestBodyV1,
    coverage_manifest_body,
)
from cruxible_core.playbill.coverage.resolver import resolve_coverage
from cruxible_core.playbill.native.context import RenderContextV1
from cruxible_core.playbill.native.grammar import NativeLocatorV1
from cruxible_core.playbill.native.manifest import (
    NativeRegionBaselineV1,
    NativeRenderFileV1,
    NativeRenderManifestV1,
)
from cruxible_core.playbill.native.parse import NativeTreeParseV1, parse_native_tree
from cruxible_core.playbill.native.state import NativeAcceptedStateV1
from cruxible_core.playbill.query.grammar import byte_sorted
from cruxible_core.playbill.semantic import SemanticAddress

LOCATOR_HANDLE_DIGEST_DOMAIN: Final = "playbill-native-locator-handle-v1"

NATIVE_CARD_BUDGET: Final = CoverageCardBudgetV1(
    max_cards_per_span=16,
    max_candidate_cards_per_span=8,
)
"""A rendered field is a small window with few plausible relationships, so the
budget is raised enough that a whole field's answer arrives unclipped."""

NativeLocatorVerdict = Literal["verified", "refused"]


class _StrictVerifyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeLocatorVerdictV1(_StrictVerifyModel):
    """What one locator resolved to, and why, granting nothing either way.

    ``grants_mutation_authority`` is a ``Literal[False]`` for the same reason a
    coverage card's is: verification points at accepted state and never confers
    a right to change it. A verified locator means "this marker truthfully names
    accepted material"; it does not mean the edit beside it is admissible, which
    is compile's question.
    """

    tag: Literal["playbill-native-locator-verdict-v1"] = "playbill-native-locator-verdict-v1"
    verdict: NativeLocatorVerdict
    grants_mutation_authority: Literal[False] = False
    region_id: str
    address: SemanticAddress
    path: str | None = None
    reason_codes: tuple[str, ...] = ()

    @field_validator("reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("locator verdict reason codes must be sorted and unique")
        return value

    @property
    def verified(self) -> bool:
        return self.verdict == "verified"


def locator_handle_digest(locator: NativeLocatorV1) -> str:
    """Digest one locator as the dereference handle for its rendered occurrence."""

    payload = locator.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, LOCATOR_HANDLE_DIGEST_DOMAIN, payload).tagged


def _accepted_digests(state: NativeAcceptedStateV1) -> Mapping[bytes, str]:
    index: dict[bytes, str] = {}
    for record in (
        *state.subjects,
        *state.claim_types,
        *state.query_definitions,
        *state.documents,
    ):
        index[record.address.model_dump_json().encode("utf-8")] = record.artifact_digest
    for claim in state.claims:
        index[claim.address.model_dump_json().encode("utf-8")] = claim.artifact_digest
    return index


def verify_native_locator(
    locator: NativeLocatorV1,
    *,
    state: NativeAcceptedStateV1,
    manifest: NativeRenderManifestV1,
    path: str | None = None,
    observed_body_digest: str | None = None,
) -> NativeLocatorVerdictV1:
    """Resolve one locator's claim against accepted state; refuse a forged one.

    Order matters only in that every reason is collected: a locator that is both
    from another generation and names an unknown region says both, because a
    reader deciding whether to trust a working tree wants the whole answer
    rather than the first thing that failed.
    """

    reasons: list[str] = []
    accepted = _accepted_digests(state)
    key = locator.address.model_dump_json().encode("utf-8")

    lens = manifest.lens
    if locator.lens_id != lens.lens_id or locator.lens_version != lens.lens_version:
        reasons.append("lens_mismatch")
    if locator.generation_root != manifest.coordinate.generation_root:
        reasons.append("generation_mismatch")
    if key not in accepted:
        reasons.append("address_not_accepted")
    elif accepted[key] != locator.artifact_digest:
        reasons.append("artifact_digest_mismatch")

    found = manifest.baseline_for(locator.region_id)
    if found is None:
        reasons.append("region_not_in_baseline")
    else:
        baseline_file, baseline = found
        if baseline.address != locator.address:
            reasons.append("region_binding_mismatch")
        if baseline.region_kind != locator.region_kind or baseline.editable != locator.editable:
            reasons.append("region_kind_mismatch")
        if baseline.body_digest != locator.baseline_digest:
            reasons.append("baseline_digest_mismatch")
        if path is not None and baseline_file.path != path:
            # A move keeps identity; a *copy* is caught as ambiguity at tree
            # scope, where both occurrences are visible. Here it is recorded
            # rather than refused, so a legitimate rename still verifies.
            reasons.append("rendered_at_another_path")
        if (
            observed_body_digest is not None
            and not baseline.editable
            and observed_body_digest != baseline.body_digest
        ):
            reasons.append("derived_body_tampered")

    fatal = {
        "address_not_accepted",
        "artifact_digest_mismatch",
        "baseline_digest_mismatch",
        "derived_body_tampered",
        "generation_mismatch",
        "lens_mismatch",
        "region_binding_mismatch",
        "region_kind_mismatch",
        "region_not_in_baseline",
    }
    verdict: NativeLocatorVerdict = "refused" if fatal & set(reasons) else "verified"
    return NativeLocatorVerdictV1(
        verdict=verdict,
        region_id=locator.region_id,
        address=locator.address,
        path=path,
        reason_codes=byte_sorted(tuple(reasons)),
    )


# -- invalidation through the coverage resolver ---------------------------


def _citation(
    entry: NativeRenderFileV1,
    baseline: NativeRegionBaselineV1,
    *,
    manifest: NativeRenderManifestV1,
) -> EvidenceCitationV1:
    locator = NativeLocatorV1(
        lens_id=manifest.lens.lens_id,
        lens_version=manifest.lens.lens_version,
        region_id=baseline.region_id,
        region_kind=baseline.region_kind,
        editable=baseline.editable,
        address=baseline.address,
        artifact_digest=baseline.artifact_digest,
        generation_root=manifest.coordinate.generation_root,
        baseline_digest=baseline.body_digest,
    )
    return EvidenceCitationV1(
        commitment_digest=baseline.body_digest,
        digest_kind="exact_bytes",
        byte_length=baseline.byte_length,
        accepted_source=entry.source,
        access_class="instance",
        capture_digests=(),
        claim_addresses=(baseline.address,),
        dereference_handle_digest=locator_handle_digest(locator),
    )


def native_invalidation_index(manifest: NativeRenderManifestV1) -> EvidenceCitationIndexV1:
    """Project the render baseline as the disposable index coverage resolves against.

    Every **editable** region contributes one citation of its own rendered bytes
    at the source its file is observed under. Derived regions are deliberately
    not cited: a derived field regenerates, so asking whether its bytes moved
    answers nothing, while the editable field beside it is exactly the material
    whose drift invalidates the derived display.

    Empty regions are skipped, because a zero-length commitment cannot be looked
    for in a working source and claiming to have looked would make an absence
    read as drift.
    """

    rows: dict[tuple[bytes, bytes], EvidenceCitationV1] = {}
    for entry in manifest.files:
        for baseline in entry.regions:
            if not baseline.editable or baseline.byte_length == 0:
                continue
            citation = _citation(entry, baseline, manifest=manifest)
            key = (
                citation.commitment_digest.encode("ascii"),
                entry.source.sort_key,
            )
            existing = rows.get(key)
            if existing is None:
                rows[key] = citation
                continue
            # Two regions in one file that rendered identical bytes are one
            # commitment with two dependents; merging keeps the index's
            # sorted-and-unique law and states the dependent count honestly.
            rows[key] = existing.model_copy(
                update={
                    "claim_addresses": tuple(
                        sorted(
                            {*existing.claim_addresses, *citation.claim_addresses},
                            key=lambda item: item.model_dump_json().encode("utf-8"),
                        )
                    )
                }
            )
    return EvidenceCitationIndexV1(
        at=manifest.coordinate,
        citations=tuple(rows[key] for key in sorted(rows)),
    )


def native_invalidation_observations(
    files: Mapping[str, bytes],
    *,
    manifest: NativeRenderManifestV1,
) -> tuple[WorkingSourceObservationV1, ...]:
    """Observe the rendered working tree under the bindings the manifest declared.

    The binding is read from the manifest rather than inferred from the path,
    which is the §11.6.1 rule that a working path is bound to a logical source
    only by declaration. A file the manifest never rendered is not observed at
    all: coverage has nothing to say about a source it never declared.
    """

    observations: list[WorkingSourceObservationV1] = []
    for entry in sorted(manifest.files, key=lambda item: item.path.encode("utf-8")):
        content = files.get(entry.path)
        if content is None:
            continue
        observations.append(observe_working_source(entry.source, content))
    return tuple(observations)


def native_invalidation_overlay(
    files: Mapping[str, bytes],
    *,
    manifest: NativeRenderManifestV1,
    index: EvidenceCitationIndexV1 | None = None,
) -> WorkingOccurrenceOverlayV1:
    """Build the working overlay for one rendered tree, wanting the baselines."""

    resolved = index or native_invalidation_index(manifest)
    return build_overlay(
        native_invalidation_observations(files, manifest=manifest),
        wanted=resolved.wanted_selections(),
    )


def native_baseline_snapshot(manifest: NativeRenderManifestV1) -> WorkingOccurrenceOverlayV1:
    """The snapshot the render itself committed to, taken from its own manifest.

    Every commitment here was computed while the bytes were being emitted, so
    this is a record of what the checkout wrote rather than a re-observation of
    what is on disk now. It carries no occurrences and no scan, because a
    snapshot commitment is not a scan result: it is the "these were the bytes"
    half of the freshness comparison the resolver makes against the working
    overlay.
    """

    return WorkingOccurrenceOverlayV1(
        sources=tuple(
            sorted(
                (
                    WorkingSourceCommitmentV1(
                        source=item.source,
                        content_digest=item.content_digest,
                        byte_length=item.byte_length,
                    )
                    for item in manifest.files
                ),
                key=lambda item: item.source.sort_key,
            )
        ),
    )


def native_freshness_manifest(
    manifest: NativeRenderManifestV1,
    *,
    ctx: RenderContextV1,
    index: EvidenceCitationIndexV1 | None = None,
    epoch: int = 0,
) -> CoverageManifestBodyV1:
    """Publish the render's own baseline as the coverage manifest to resolve against.

    This is what makes freshness provable for a rendered tree without a watcher
    (§11.6.6): the render knows exactly which bytes it wrote to each declared
    source, so it can commit to them, and the resolver then compares the working
    snapshot against that commitment rather than against itself. A file whose
    bytes still reproduce is `complete` and may carry `exact`; a file that was
    edited is `stale` for that source, and what would have been `exact` is
    lowered to a labeled candidate rather than asserted.
    """

    resolved = index or native_invalidation_index(manifest)
    return coverage_manifest_body(
        instance_id=manifest.instance_id,
        index=resolved,
        overlay=native_baseline_snapshot(manifest),
        access_profile=ctx.access_profile,
        epoch=epoch,
    )


class NativeInvalidationV1(_StrictVerifyModel):
    """The coverage answer over a rendered tree, plus what it invalidates.

    ``coverage`` is the unmodified :class:`CoverageResultV1` the F2 resolver
    produced. ``invalidated_region_ids`` is the native layer's own reading of
    it: for every drift card, the derived regions belonging to the same address
    are the display that no longer applies to the local material. That mapping
    is presentation, and it grants nothing -- the cards already say so
    structurally.
    """

    tag: Literal["playbill-native-invalidation-v1"] = "playbill-native-invalidation-v1"
    coverage: CoverageResultV1
    drifted_addresses: tuple[SemanticAddress, ...] = ()
    invalidated_region_ids: tuple[str, ...] = ()
    intact_region_ids: tuple[str, ...] = ()

    @field_validator("invalidated_region_ids", "intact_region_ids")
    @classmethod
    def _sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("invalidation region identities must be sorted and unique")
        return value

    @property
    def grants_governance_facts(self) -> bool:
        """Always false, and asserted from the cards rather than declared."""

        return any(
            card.grants_mutation_authority or card.resolves_equivalence
            for span in self.coverage.spans
            for card in span.cards
        )


def native_invalidation_spans(
    files: Mapping[str, bytes],
    *,
    manifest: NativeRenderManifestV1,
    parsed: NativeTreeParseV1 | None = None,
) -> tuple[CoverageSpanRequestV1, ...]:
    """Ask one coverage question per editable region, at the window it now occupies.

    A whole-file question would be the coarse one: a file holds several fields,
    and asking about all of them at once merges a verified match on one field
    with an ambiguity on another into a single answer that can only report the
    weaker story. One span per editable field keeps each answer about the field
    it is about, and the window is where the region *currently* sits, because a
    byte offset is presentation and the caller is asking about the file in front
    of it.

    A rendered file with no editable region contributes one whole-file span. The
    index cites only editable regions, so such a file has nothing to match and
    the span exists to report that honestly rather than to omit the file.
    """

    tree = parsed or parse_native_tree(files, manifest=manifest)
    sources = {item.path: item.source for item in manifest.files}
    spans: list[CoverageSpanRequestV1] = []
    covered: set[str] = set()
    for file_parse in tree.files:
        source = sources.get(file_parse.path)
        if source is None or file_parse.path not in files:
            continue
        for region in file_parse.regions:
            if not region.editable or region.byte_length == 0:
                continue
            covered.add(file_parse.path)
            spans.append(
                CoverageSpanRequestV1(
                    source=source,
                    selection=CoverageSelectionV1(
                        start_byte=region.line_overlay.start_byte,
                        end_byte=region.line_overlay.end_byte,
                    ),
                )
            )
    for entry in manifest.files:
        if entry.path in covered or entry.path not in files:
            continue
        spans.append(CoverageSpanRequestV1(source=entry.source))
    return tuple(spans)


def resolve_native_invalidation(
    files: Mapping[str, bytes],
    *,
    manifest: NativeRenderManifestV1,
    ctx: RenderContextV1,
    spans: Iterable[CoverageSpanRequestV1] | None = None,
    parsed: NativeTreeParseV1 | None = None,
) -> NativeInvalidationV1:
    """Ask the F2 resolver what the working tree still matches, and read the answer."""

    if manifest.coordinate != ctx.at:
        raise ValueError("a native invalidation resolves one accepted coordinate at a time")
    index = native_invalidation_index(manifest)
    observations = native_invalidation_observations(files, manifest=manifest)
    overlay = build_overlay(observations, wanted=index.wanted_selections())
    requested: Sequence[CoverageSpanRequestV1] = tuple(spans or ()) or native_invalidation_spans(
        files, manifest=manifest, parsed=parsed
    )
    if not requested:
        raise ValueError("a native invalidation needs at least one observed rendered file")

    result = resolve_coverage(
        CoverageRequestV1(
            instance_id=ctx.instance_id,
            at=ctx.at,
            spans=tuple(requested),
            budget=NATIVE_CARD_BUDGET,
        ),
        index=index,
        overlay=overlay,
        access=ctx.access_profile,
        manifest=native_freshness_manifest(manifest, ctx=ctx, index=index),
    )

    drifted = {
        card.expected_commitment_digest
        for span in result.spans
        for card in span.cards
        if card.match_state == "drifted"
    }
    intact = {
        card.expected_commitment_digest
        for span in result.spans
        for card in span.cards
        if card.match_state == "exact"
    }
    drifted_addresses: list[SemanticAddress] = []
    invalidated: set[str] = set()
    unchanged: set[str] = set()
    for entry in manifest.files:
        for baseline in entry.regions:
            if baseline.editable and baseline.body_digest in drifted:
                drifted_addresses.append(baseline.address)
                invalidated.update(
                    neighbour.region_id
                    for neighbour in entry.regions
                    if not neighbour.editable and neighbour.address == baseline.address
                )
            elif baseline.editable and baseline.body_digest in intact:
                unchanged.update(
                    neighbour.region_id
                    for neighbour in entry.regions
                    if not neighbour.editable and neighbour.address == baseline.address
                )
    return NativeInvalidationV1(
        coverage=result,
        drifted_addresses=tuple(
            sorted(
                {item.model_dump_json(): item for item in drifted_addresses}.values(),
                key=lambda item: item.model_dump_json().encode("utf-8"),
            )
        ),
        invalidated_region_ids=byte_sorted(tuple(invalidated)),
        intact_region_ids=byte_sorted(tuple(unchanged - invalidated)),
    )


__all__ = [
    "LOCATOR_HANDLE_DIGEST_DOMAIN",
    "NATIVE_CARD_BUDGET",
    "NativeInvalidationV1",
    "NativeLocatorVerdict",
    "NativeLocatorVerdictV1",
    "locator_handle_digest",
    "native_baseline_snapshot",
    "native_freshness_manifest",
    "native_invalidation_index",
    "native_invalidation_observations",
    "native_invalidation_overlay",
    "native_invalidation_spans",
    "resolve_native_invalidation",
    "verify_native_locator",
]
