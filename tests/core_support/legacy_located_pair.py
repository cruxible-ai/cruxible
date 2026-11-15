"""Revision 71's located own words: the own-words Capture and a second, located copy.

Revision 72 writes located own words as one Capture (the own words, sourced at
their file). A Claim accepted before it holds a pair instead: a coordinator
self-source Capture over the bytes (CAS-sourced, cited ``copy``/``self_source``)
and a working-selection Capture of the same bytes under the file's foreign-source
contract (cited ``copy``/``independent``, its contract pinned and installed).
Such Claims must keep verifying, re-binding and drifting forever, so tests build
the pair here with the two Capture builders that remain, exactly as revision
71's lowering appended the located copy to the unlocated own words. No
production writer builds this shape any more.
"""

from __future__ import annotations

import base64
import itertools
from dataclasses import dataclass

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatement,
    ClaimAuthoringPayload,
    ClaimDependencyDrafts,
    SelfSourceBody,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.captures import (
    build_working_selection_capture,
    builtin_contract_for_write,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
    parse_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.claims import (
    LiteralClaimObject,
    build_claim_citation,
    merge_claim_citations,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import _merge_pins, lower_authoring
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance

_CLOCK = itertools.count(10)


@dataclass(frozen=True)
class LegacyPair:
    """One lowered (not yet accepted) revision-71 located own-words Claim."""

    claim_id: str
    path: str
    own_words: str
    location: str
    location_contract_digest: str
    #: The members the Claim's acceptance changes: the Claim and, when the file
    #: had none, the file's foreign-source contract.
    updates: dict[str, bytes]


def own_words_payload(
    *,
    subject_path: str,
    predicate: str,
    value: str,
    observation: WorkingSelectionObservation,
    role: str = "observation",
    rationale: str = "The writer checked it.",
    revises: str | None = None,
) -> ClaimAuthoringPayload:
    """Own words found in a file, as a write's located own-words member states them."""

    return ClaimAuthoringPayload(
        statement=AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_path),
            predicate=predicate,
            qualifier=None,
            object=LiteralClaimObject(value=value),
            role=role,  # type: ignore[arg-type]
        ),
        rationale=rationale,
        source=SelfSourceBody(
            content_base64=base64.b64encode(observation.selected_content).decode("ascii")
        ),
        revises=revises,
        located_at=observation,
        dependency_drafts=ClaimDependencyDrafts(),
    )


def lower_legacy_pair(
    instance: PlaybillInstance,
    payload: ClaimAuthoringPayload,
    *,
    actor_id: str = "owner",
    coordinator: AuthoringIntentCoordinator | None = None,
) -> LegacyPair:
    """Lower ``payload`` (own words with ``located_at``) the way revision 71 did."""

    location = payload.located_at
    assert location is not None and isinstance(payload.source, SelfSourceBody)
    unlocated = ClaimAuthoringPayload.model_validate(
        {
            **payload.model_dump(mode="python", exclude={"located_at"}),
        }
    )
    coordinator = coordinator or AuthoringIntentCoordinator.for_instance(instance)
    intent = coordinator.create(
        actor=AuthenticatedActor(actor_id=actor_id),
        payload=unlocated,
        canonical_timestamp=f"2026-08-21T13:{next(_CLOCK) % 60:02d}:00.000000Z",
    ).intent
    lowered = lower_authoring(instance, intent=intent, actor_id=actor_id)
    tree = dict(lowered.proposed_tree)
    path = next(p for p, _content in lowered.changed_members if p.startswith("claims/"))
    claim = parse_claim(tree[path], path=path)
    (own_words,) = (
        item.capture_digest
        for item in claim.backing.citations
        if (item.role, item.origin) == ("copy", "self_source")
        and item.capture_digest not in _inherited(claim, instance)
    )

    head = instance.accepted_coordinate()
    contract_path = capture_contract_path(
        foreign_source_capture_contract(location.source_id).identity.name
    )
    previous = instance.blob_at(head.git_oid, contract_path)
    accepted = None if previous is None else parse_capture_contract(previous, path=contract_path)
    contract = builtin_contract_for_write(
        foreign_source_capture_contract(location.source_id), accepted
    )
    located = build_working_selection_capture(
        store=instance.body_store(),
        actor_id=actor_id,
        claim_id=claim.identity.name,
        rationale=payload.rationale,
        observed_at=claim.backing.referent_context.observed_at,
        accepted_coordinate=AcceptedCoordinate.from_internal(head),
        source_id=location.source_id,
        coordinate=location.coordinate.model_dump(mode="json"),
        selector=location.selector.model_dump(mode="json"),
        selected_content=location.selected_content,
        accepted_contract=contract,
    )
    contract_digest = capture_contract_digest(located.contract).tagged
    # Revision 71 pinned the located copy's exact contract version beside the
    # coordinator's.
    pins = _merge_pins(
        claim.pins,
        (
            ArtifactPin(
                role="capture-contract",
                target=located.contract.identity,
                artifact_digest=contract_digest,
            ),
        ),
    )
    backing = claim.backing.model_copy(
        update={
            "capture_digests": tuple(
                sorted(
                    {*claim.backing.capture_digests, located.capture_digest},
                    key=lambda item: item.encode("ascii"),
                )
            ),
            "citations": merge_claim_citations(
                claim.backing.citations,
                (
                    build_claim_citation(
                        claim.identity,
                        capture_digest=located.capture_digest,
                        role="copy",
                        origin="independent",
                    ),
                ),
            ),
        }
    )
    updates = {path: render_claim(claim.model_copy(update={"backing": backing, "pins": pins}))}
    if accepted != located.contract:
        updates[contract_path] = render_capture_contract(located.contract)
    return LegacyPair(
        claim_id=claim.identity.name,
        path=path,
        own_words=own_words,
        location=located.capture_digest,
        location_contract_digest=contract_digest,
        updates={
            **{p: c for p, c in lowered.changed_members if not p.startswith("claims/")},
            **updates,
        },
    )


def _inherited(claim: object, instance: PlaybillInstance) -> frozenset[str]:
    """Captures a revision's predecessor already cited (none for a new Claim)."""

    from cruxible_client.contracts.claims import ClaimArtifact, claim_path

    assert isinstance(claim, ClaimArtifact)
    if claim.lifecycle.predecessor_digest is None:
        return frozenset()
    path = claim_path(claim.identity.name)
    content = instance.blob_at(instance.accepted_coordinate().git_oid, path)
    if content is None:
        return frozenset()
    return frozenset(parse_claim(content, path=path).backing.capture_digests)


__all__ = ["LegacyPair", "lower_legacy_pair", "own_words_payload"]
