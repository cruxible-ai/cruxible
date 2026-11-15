"""Located own words are one Capture (compiler revision 72, Claim law v4 revision 3).

- Own words found in a file are one coordinator self-source Capture sourced at
  the file; the ClaimType's admission treats them exactly as unlocated own words.
- A Claim accepted under revision 71 holds a pair (own words plus a located
  copy); it still verifies, and a re-bind collapses the pair into one located
  own-words Capture of the same file, never of another file.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayload,
    SectionSelector,
    WorkingAnchorWindow,
    WorkingDigestCoordinate,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    coordinator_location_valid,
    parse_capture_envelope,
)
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.source_references import ExternalSourceReference
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.authoring.lowering import lower_authoring
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support.legacy_located_pair import (
    LegacyPair,
    lower_legacy_pair,
    own_words_payload,
)
from tests.test_claims.test_claim_type_v7_revisions import _V7World, v7_type
from tests.test_claims.test_identity_evidence_rules import IDENTITY, PREDICATE, SOURCE, _subject

_ACCESS = BodyAccessContext(principal_id="test", can_read_body=True)


def located(
    text: bytes, *, source: str = SOURCE, section: tuple[str, ...] = ("Status",)
) -> WorkingSelectionObservation:
    """A section of a file, observed on the writer's side."""

    heading = ("# " + section[-1] + "\n").encode("ascii")
    page = heading + text
    return WorkingSelectionObservation(
        source_id=source,
        coordinate=WorkingDigestCoordinate(
            source_content_digest="sha256:" + hashlib.sha256(page).hexdigest(),
            source_byte_length=len(page),
        ),
        selected_content_base64=base64.b64encode(text).decode("ascii"),
        selected_bytes_digest="sha256:" + hashlib.sha256(text).hexdigest(),
        selector=WorkingAnchorWindow(
            anchor=text.decode("ascii").strip(),
            start_byte=len(heading),
            end_byte=len(page),
            observed_occurrence_count=1,
            section=SectionSelector(path=section),
        ),
    )


class LocatedWorld(_V7World):
    def payload(
        self,
        observation: WorkingSelectionObservation,
        *,
        value: str = "ready",
        claim_ref: str | None = None,
    ) -> ClaimAuthoringPayload:
        subject = _subject()
        return own_words_payload(
            subject_path=subject_path(subject.subject_kind, subject.subject_id),
            predicate=PREDICATE,
            value=value,
            observation=observation,
            revises=claim_ref,
        )

    def lower(
        self, payload: ClaimAuthoringPayload, **update: object
    ) -> tuple[dict[str, bytes], tuple[tuple[str, bytes], ...]]:
        if update:
            payload = ClaimAuthoringPayload.model_validate(
                {**payload.model_dump(mode="python"), **update}
            )
        intent = self.coordinator.create(
            actor=AuthenticatedActor(actor_id="owner"),
            payload=payload,
            canonical_timestamp=self.timestamp(),
        ).intent
        lowered = lower_authoring(self.instance, intent=intent, actor_id="owner")
        return dict(lowered.proposed_tree), tuple(lowered.changed_members)

    def author_located(
        self, observation: WorkingSelectionObservation, **update: object
    ) -> tuple[dict[str, bytes], str]:
        claim_ref = update.pop("claim_ref", None)
        tree, changed = self.lower(
            self.payload(observation, claim_ref=claim_ref),  # type: ignore[arg-type]
            **update,
        )
        path = next(p for p, _content in changed if p.startswith("claims/"))
        return tree, path

    def legacy(self, observation: WorkingSelectionObservation) -> LegacyPair:
        pair = lower_legacy_pair(
            self.instance, self.payload(observation), coordinator=self.coordinator
        )
        self.accept({**self.tree(), **pair.updates}, name=self.name("legacy"))
        return pair


@pytest.fixture
def world(tmp_path: Path) -> LocatedWorld:
    return LocatedWorld(tmp_path)


def _envelope(world: LocatedWorld, digest: str):  # type: ignore[no-untyped-def]
    return parse_capture_envelope(world.instance.body_store().read(digest, access=_ACCESS))


# --- Admission is unchanged: located own words are own words ----------------------------


@pytest.mark.parametrize(
    ("requirement", "outcome"),
    [("captured", "refused"), ("self", "uncovered"), ("none", "supported")],
)
def test_located_own_words_are_admitted_exactly_as_unlocated_own_words(
    world: LocatedWorld, requirement: str, outcome: str
) -> None:
    world.seed(v7_type(evidence_requirement=requirement))
    tree, path = world.author_located(located(b"status: ready\n"))
    claim = parse_claim(tree[path], path=path)
    (citation,) = claim.backing.citations  # type: ignore[union-attr]
    assert (citation.role, citation.origin) == ("copy", "self_source")
    assert coordinator_location_valid(_envelope(world, citation.capture_digest))
    if outcome == "refused":
        assert "cruxible.claim.captured_evidence_required" in world.refusals(tree, name="own")
        return
    world.accept(tree, name="own")
    assert world.verdict(claim.identity.name) == outcome


# --- Revision 71 pairs ----------------------------------------------------------------------


def test_an_old_pair_still_verifies_and_reads_as_before(world: LocatedWorld) -> None:
    world.seed(v7_type())
    pair = world.legacy(located(b"status: ready\n"))
    claim = world.claim(pair.claim_id)
    assert {
        (item.role, item.origin, item.capture_digest)
        for item in claim.backing.citations  # type: ignore[union-attr]
    } == {("copy", "self_source", pair.own_words), ("copy", "independent", pair.location)}
    assert any(pin.role == "capture-contract" and pin.target == IDENTITY for pin in claim.pins), (
        "the located copy's contract stays pinned"
    )
    # Read as under revision 71: this type's rule names the file's contract, and
    # the pair's located copy is an independent copy under it (the copy
    # carve-out). One located own-words Capture is own words only ("uncovered"
    # under the same type, above).
    assert world.verdict(pair.claim_id) == "supported"
    # The same own words written again where they stand change nothing: the
    # pair already holds both the words and their place.
    _tree, changed = world.lower(
        world.payload(located(b"status: ready\n"), claim_ref=pair.claim_id)
    )
    assert changed == ()


def test_a_pair_collapses_into_one_located_capture_of_the_same_file(world: LocatedWorld) -> None:
    world.seed(v7_type())
    pair = world.legacy(located(b"status: ready\n"))
    held = sorted(
        item.citation_id
        for item in world.claim(pair.claim_id).backing.citations  # type: ignore[union-attr]
    )
    tree, path = world.author_located(
        located(b"status: ready, checked\n"),
        claim_ref=pair.claim_id,
        replaces_citations=tuple(held),
    )
    world.accept(tree, name="collapse")
    rebound = world.claim(pair.claim_id)
    (citation,) = rebound.backing.citations  # type: ignore[union-attr]
    assert (citation.role, citation.origin) == ("copy", "self_source")
    assert rebound.backing.capture_digests == (citation.capture_digest,)
    envelope = _envelope(world, citation.capture_digest)
    assert isinstance(envelope.source, ExternalSourceReference)
    assert envelope.source.source_identity == SOURCE
    contract_pins = {pin.target for pin in rebound.pins if pin.role == "capture-contract"}
    assert contract_pins == {COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity}
    assert world.verdict(pair.claim_id) == "uncovered"


def test_a_pair_never_collapses_into_another_files_own_words(world: LocatedWorld) -> None:
    world.seed(v7_type())
    pair = world.legacy(located(b"status: ready\n"))
    held = sorted(
        item.citation_id
        for item in world.claim(pair.claim_id).backing.citations  # type: ignore[union-attr]
    )
    tree, _path = world.author_located(
        located(b"status: ready, elsewhere\n", source="repo.elsewhere"),
        claim_ref=pair.claim_id,
        replaces_citations=tuple(held),
    )
    assert "cruxible.claim.rebind_unmatched" in world.refusals(tree, name="elsewhere")


def test_located_own_words_re_bind_replace_one_citation(world: LocatedWorld) -> None:
    world.seed(v7_type())
    tree, path = world.author_located(located(b"status: ready\n"))
    world.accept(tree, name="first")
    claim_id = parse_claim(tree[path], path=path).identity.name
    (first,) = world.claim(claim_id).backing.citations  # type: ignore[union-attr]
    tree, _path = world.author_located(
        located(b"status: ready again\n"),
        claim_ref=claim_id,
        replaces_citations=(first.citation_id,),
    )
    world.accept(tree, name="again")
    (after,) = world.claim(claim_id).backing.citations  # type: ignore[union-attr]
    assert after.citation_id != first.citation_id
    assert (after.role, after.origin) == ("copy", "self_source")
