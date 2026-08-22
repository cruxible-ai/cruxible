"""PC-F ClaimType cards and Subject profiles: the compact claim-side interface.

Four laws are under test here:

* a card and a profile are coordinate-pure projections -- rebuilding them from
  the same accepted facts reproduces the same bytes -- and a verdict or a
  currency can appear on a profile only when it was taken at an explicit
  evaluation time;
* match bases follow the frozen priority order, and a tag or a lexical hit never
  resolves equivalence: only an alias admitted under the target namespace's
  authority, an exact address, or an exact entrypoint name can;
* every clip is stated in coverage, so a silently narrowed card is
  unrepresentable; and
* the compact path actually pays for itself -- a card or a profile is materially
  smaller than the row-by-row expansion it replaces, and the SLO test below
  fails if that stops being true.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from cruxible_core.playbill.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.canonical import canonical_bytes
from cruxible_core.playbill.claim_slots import (
    classify_claim_slot,
    classify_claim_slot_member,
)
from cruxible_core.playbill.claim_types import claim_type_digest, claim_type_path
from cruxible_core.playbill.claims import (
    AcceptedClaim,
    ClaimArtifact,
    ClaimBacking,
    ClaimReferentContext,
    ClaimStatement,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
)
from cruxible_core.playbill.discovery import DiscoveryRequestV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.backends import (
    ClaimFactRowV1,
    ClaimQueryFactsV1,
    subject_query_view,
)
from cruxible_core.playbill.query.capsules import (
    DATA_FENCE_OPEN,
    ContextCapsuleBudgetV1,
    build_interface_context_capsule,
    render_bounded_context_capsule,
)
from cruxible_core.playbill.query.cards import (
    ClaimTypeCardV1,
    InterfaceProjectionBudgetV1,
    SubjectProfilePredicateV1,
    SubjectProfileV1,
    build_interface_projections,
    claim_type_card_digest,
    discover_interfaces,
    subject_profile_digest,
)
from cruxible_core.playbill.query.semantic_discovery import (
    MATCH_BASIS_PRIORITY,
    DiscoveryError,
    build_discovery_vocabulary,
)
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.subjects import (
    AcceptedSubject,
    subject_path,
)
from tests.test_playbill.test_claim_query_engine import (
    NOW,
    claim_fact,
    coordinate,
    rule_for,
    status_claim,
    subject,
)
from tests.test_playbill.test_query_definitions import (
    OWNER_AUTHORITY,
    REVIEWER_PREDICATE,
    STATUS_PREDICATE,
    accepted_query,
    active_work_query,
    claim_type,
)

ALIAS_PREDICATE = "semantic.alias"
TAG_PREDICATE = "semantic.tag"
DISTINCT_PREDICATE = "semantic.distinct_from"
EVALUATION_TIME = "2026-08-16T12:00:00.000000Z"
STATUS_CARD_ADDRESS = SemanticAddress.whole_artifact(claim_type_path(STATUS_PREDICATE))
WI1 = subject_path("project.work_item", "wi-1")
WI2 = subject_path("project.work_item", "wi-2")


# -- fixtures -------------------------------------------------------------


def _addressed_claim(
    index: int,
    *,
    target: SemanticAddress,
    predicate: str,
    obj: LiteralClaimObject | SubjectClaimObject,
) -> ClaimFactRowV1:
    """One accepted Claim about any semantic address, not only a Subject artifact.

    Descriptor Claims target ClaimType addresses as well as Subject addresses, so
    the fixtures need a builder that is not Subject-shaped.
    """

    object_kind = "subject" if isinstance(obj, SubjectClaimObject) else "literal"
    contract = claim_type(predicate, object_kind=object_kind)
    digest = claim_type_digest(contract).tagged
    claim_id = f"CLM-{index:032x}"
    artifact = ClaimArtifact(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        statement=ClaimStatement(
            subject=target,
            claim_type=contract.identity,
            claim_type_digest=digest,
            predicate=predicate,
            object=obj,
            role="normative",
        ),
        backing=ClaimBacking(
            referent_context=ClaimReferentContext(
                subject_content_digest=digest,
                observed_at=NOW,
            ),
        ),
        authority=OWNER_AUTHORITY,
        pins=(ArtifactPin(role="claim-type", target=contract.identity, artifact_digest=digest),),
    )
    return ClaimFactRowV1(
        accepted=AcceptedClaim(
            path=claim_path(claim_id),
            claim=artifact,
            statement_digest=claim_statement_digest(artifact.statement).tagged,
            artifact_digest=claim_artifact_digest(artifact).tagged,
        ),
        rule=rule_for(predicate, object_kind=object_kind),
        resolved_authority_basis=("authority:owner",),
    )


def _subjects(items: tuple[str, ...]) -> tuple[AcceptedSubject, ...]:
    return tuple(
        sorted(
            (subject("project.work_item", item) for item in items),
            key=lambda item: item.path.encode("utf-8"),
        )
    )


def _facts(
    claims: tuple[ClaimFactRowV1, ...],
    *,
    items: tuple[str, ...] = ("wi-1", "wi-2", "wi-3"),
) -> ClaimQueryFactsV1:
    return ClaimQueryFactsV1(
        coordinate=coordinate(),
        subjects=_subjects(items),
        claims=tuple(sorted(claims, key=lambda item: item.accepted.path.encode("utf-8"))),
    )


def _descriptors() -> tuple[ClaimFactRowV1, ...]:
    """Accepted alias/tag/distinction vocabulary over one ClaimType and one Subject."""

    return (
        _addressed_claim(
            10,
            target=STATUS_CARD_ADDRESS,
            predicate=ALIAS_PREDICATE,
            obj=LiteralClaimObject(value="work item state"),
        ),
        _addressed_claim(
            11,
            target=STATUS_CARD_ADDRESS,
            predicate=TAG_PREDICATE,
            obj=LiteralClaimObject(value="triage"),
        ),
        _addressed_claim(
            12,
            target=STATUS_CARD_ADDRESS,
            predicate=DISTINCT_PREDICATE,
            obj=SubjectClaimObject(
                address=SemanticAddress.whole_artifact(claim_type_path(REVIEWER_PREDICATE))
            ),
        ),
        _addressed_claim(
            13,
            target=SemanticAddress.whole_artifact(WI1),
            predicate=ALIAS_PREDICATE,
            obj=LiteralClaimObject(value="Ready Queue"),
        ),
        _addressed_claim(
            14,
            target=SemanticAddress.whole_artifact(WI1),
            predicate=TAG_PREDICATE,
            obj=LiteralClaimObject(value="triage"),
        ),
    )


def _claim_types() -> tuple[object, ...]:
    return (
        claim_type(STATUS_PREDICATE),
        claim_type(REVIEWER_PREDICATE, object_kind="subject"),
    )


def _projections(
    facts: ClaimQueryFactsV1,
    *,
    evaluation_time: datetime | None = None,
    budget: InterfaceProjectionBudgetV1 = InterfaceProjectionBudgetV1(),
):
    vocabulary = build_discovery_vocabulary(
        view=subject_query_view(facts),
        facts=facts,
        claim_types=_claim_types(),  # type: ignore[arg-type]
        definitions=(accepted_query(active_work_query()),),
    )
    return vocabulary, build_interface_projections(
        vocabulary=vocabulary,
        facts=facts,
        claim_types=_claim_types(),  # type: ignore[arg-type]
        evaluation_time=evaluation_time,
        budget=budget,
    )


def _standard_facts() -> ClaimQueryFactsV1:
    return _facts(
        (
            status_claim(1, "wi-1", "ready"),
            status_claim(2, "wi-2", "blocked"),
            claim_fact(
                3,
                subject_row=subject("project.work_item", "wi-1"),
                predicate=REVIEWER_PREDICATE,
                obj=SubjectClaimObject(
                    address=SemanticAddress.whole_artifact(WI2),
                ),
            ),
            *_descriptors(),
        )
    )


def _request(**overrides: object) -> DiscoveryRequestV1:
    fields: dict[str, object] = {
        "at": AcceptedCoordinate.from_internal(coordinate()),
        "evaluation_time": EVALUATION_TIME,
        "profile": "all",
    }
    fields.update(overrides)
    return DiscoveryRequestV1(**fields)  # type: ignore[arg-type]


def _card(projections, address: SemanticAddress = STATUS_CARD_ADDRESS) -> ClaimTypeCardV1:
    found = projections.card(address)
    assert found is not None
    return found


def _profile(projections, path: str = WI1) -> SubjectProfileV1:
    found = projections.profile(SemanticAddress.whole_artifact(path))
    assert found is not None
    return found


# -- the interface surface ------------------------------------------------


def test_a_card_states_the_interface_and_its_policies_without_any_policy_body() -> None:
    _vocabulary, projections = _projections(_standard_facts())
    card = _card(projections)

    assert card.predicate == STATUS_PREDICATE
    assert card.allowed_subject_kinds == ("project.work_item",)
    assert card.object_kind == "literal"
    assert card.cardinality == "one"
    assert card.permitted_roles == ("normative",)
    # A schema is committed by digest and described in one line; the body stays
    # behind expand, because a card is a header file, not the artifact.
    assert card.literal_schema_summary == "type=string"
    assert card.literal_schema_digest is not None
    rendered = canonical_bytes(card.model_dump(mode="json"))
    assert b"transition_requirements" not in rendered
    assert b"eligible_verdicts" not in rendered.split(b'"summary"')[0]
    assert tuple(item.policy for item in card.policies) == (
        "admission",
        "evidence_admission",
        "resolution",
    )
    assert "cardinality=one" in card.policies[2].summary
    assert card.usage.claim_count == 2
    assert card.usage.subject_count == 2
    assert card.usage.contended_subject_count == 0
    assert card.expansion_links[0].operation == "expand"
    assert card.coverage.truncated_facets == ()


def test_a_card_carries_the_accepted_alias_tag_and_reviewed_distinction() -> None:
    _vocabulary, projections = _projections(_standard_facts())
    card = _card(projections)

    assert card.aliases == ("work item state",)
    assert card.tags == ("triage",)
    assert tuple(item.predicate for item in card.relations) == ("semantic.distinct_from",)
    assert card.relations[0].target.artifact_path == claim_type_path(REVIEWER_PREDICATE)
    assert card.relations[0].inbound is False
    # The distinction is stored once with the new item as its subject and indexed
    # in both directions, so the other end sees the same edge marked inbound.
    reviewer = projections.card(SemanticAddress.whole_artifact(claim_type_path(REVIEWER_PREDICATE)))
    assert reviewer is not None
    assert reviewer.relations[0].inbound is True


def test_a_subject_profile_indexes_its_predicates_and_links_the_claim_type_card() -> None:
    _vocabulary, projections = _projections(_standard_facts())
    profile = _profile(projections)

    assert profile.subject_kind == "project.work_item"
    assert profile.subject_id == "wi-1"
    assert profile.aliases == ("Ready Queue",)
    assert profile.tags == ("triage",)
    predicates = {item.predicate: item for item in profile.predicates}
    assert set(predicates) == {STATUS_PREDICATE, REVIEWER_PREDICATE, ALIAS_PREDICATE, TAG_PREDICATE}
    status = predicates[STATUS_PREDICATE]
    assert status.claim_type_address.artifact_path == claim_type_path(STATUS_PREDICATE)
    assert status.cardinality == "one"
    assert status.resolution == "single"
    assert status.object_preview == '{"kind":"literal","value":"ready"}'
    assert status.pinned_claim_type_digests == (
        claim_type(STATUS_PREDICATE).identity
        and claim_type_digest(claim_type(STATUS_PREDICATE)).tagged,
    )
    # Individual Claims stay demand-loaded: the profile counts them and links the
    # card, and `expand` on the Subject is the drill-down.
    assert profile.expansion_links[0].subject == SemanticAddress.whole_artifact(WI1)


# -- coordinate purity and rebuild parity ---------------------------------


def test_cards_and_profiles_rebuild_byte_identically_from_the_same_facts() -> None:
    first = _projections(_standard_facts())[1]
    second = _projections(_standard_facts())[1]

    assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
        second.model_dump(mode="json")
    )
    assert claim_type_card_digest(_card(first)) == claim_type_card_digest(_card(second))
    assert subject_profile_digest(_profile(first)) == subject_profile_digest(_profile(second))


def test_a_profile_is_coordinate_pure_until_it_is_taken_at_an_explicit_time() -> None:
    facts = _standard_facts()
    pure = _profile(_projections(facts)[1])
    timed = _profile(_projections(facts, evaluation_time=NOW)[1])

    assert pure.verdict_relative is False
    assert pure.evaluation_time is None
    assert all(item.verdict is None and item.currency is None for item in pure.predicates)

    assert timed.verdict_relative is True
    assert timed.evaluation_time == NOW
    status = next(item for item in timed.predicates if item.predicate == STATUS_PREDICATE)
    assert (status.verdict, status.currency) == ("supported", "current")
    # The two differ only in the time-relative overlay: coordinate-pure structure
    # is identical, which is what makes the pure form safe to cache and index.
    assert tuple(item.predicate for item in pure.predicates) == tuple(
        item.predicate for item in timed.predicates
    )


def test_a_verdict_cannot_be_carried_without_the_read_time_that_produced_it() -> None:
    row = SubjectProfilePredicateV1(
        predicate=STATUS_PREDICATE,
        claim_type_address=STATUS_CARD_ADDRESS,
        pinned_claim_type_digests=(claim_type_digest(claim_type(STATUS_PREDICATE)).tagged,),
        claim_count=1,
        contender_count=1,
        resolution="single",
        verdict="supported",
        currency="current",
    )
    payload = _profile(_projections(_standard_facts())[1]).model_dump(mode="json")
    payload["predicates"] = [row.model_dump(mode="json")]

    with pytest.raises(ValueError, match="explicit read time"):
        SubjectProfileV1.model_validate(payload)


def test_a_contended_predicate_is_rendered_unresolved_and_never_picks_a_winner() -> None:
    facts = _facts(
        (
            status_claim(1, "wi-1", "ready"),
            status_claim(2, "wi-1", "blocked"),
            *_descriptors(),
        )
    )
    _vocabulary, projections = _projections(facts, evaluation_time=NOW)
    status = next(
        item for item in _profile(projections).predicates if item.predicate == STATUS_PREDICATE
    )

    assert status.claim_count == 2
    assert status.contender_count == 2
    assert status.resolution == "unresolved"
    assert status.object_digest is None
    assert status.object_preview is None
    # Contention is structure, not adjudication: the card counts it without
    # claiming a verdict, and cardinality tells the reader whether it is a
    # conflict at all.
    card = _card(projections)
    assert card.cardinality == "one"
    assert card.usage.contended_subject_count == 1
    assert card.usage.contended_subject_identities == ("Subject:project.work_item/wi-1",)


@pytest.mark.parametrize(
    ("values", "expected_resolution", "expected_member_state"),
    (
        (("ready",), "single", "accepted_current"),
        (("ready", "ready"), "single", "accepted_current"),
        (("ready", "blocked"), "unresolved", "conflicted"),
        (("ready", "ready", "blocked"), "unresolved", "conflicted"),
    ),
)
def test_every_slot_shape_agrees_between_profile_and_member_health(
    values: tuple[str, ...],
    expected_resolution: str,
    expected_member_state: str,
) -> None:
    rows = tuple(status_claim(index + 1, "wi-1", value) for index, value in enumerate(values))
    claims = tuple(row.accepted.claim for row in rows)
    slot = classify_claim_slot(claims)
    _vocabulary, projections = _projections(_facts((*rows, *_descriptors())))
    profile = _profile(projections)
    status = next(item for item in profile.predicates if item.predicate == STATUS_PREDICATE)

    assert status.resolution == slot.resolution == expected_resolution
    assert status.contender_count == slot.contender_count == len(set(values))
    assert classify_claim_slot_member(slot, claims[0].identity.qualified) == expected_member_state


# -- match bases -----------------------------------------------------------


def test_match_bases_follow_the_frozen_priority_order_on_every_projection() -> None:
    _vocabulary, projections = _projections(_standard_facts())
    card = _card(projections)

    order = [MATCH_BASIS_PRIORITY[item.basis] for item in card.match_bases]
    assert order == sorted(order)
    assert [item.basis for item in card.match_bases] == [
        "exact_address",
        "exact_alias",
        "structural_signature",
        "tag",
        "lexical",
    ]
    assert card.match_bases[0].terms == (
        "ClaimType:project.work_item.status",
        STATUS_CARD_ADDRESS.artifact_path,
    )
    assert card.match_bases[1].terms == ("work item state",)
    assert card.match_bases[3].terms == ("triage",)


def test_a_tag_or_lexical_basis_never_resolves_equivalence() -> None:
    _vocabulary, projections = _projections(_standard_facts())
    grades = {
        item.basis: item.resolves_equivalence
        for card in projections.cards
        for item in card.match_bases
    }

    assert grades["exact_address"] is True
    assert grades["exact_alias"] is True
    assert grades["structural_signature"] is False
    assert grades["tag"] is False
    assert grades["lexical"] is False


def test_only_an_unambiguous_equivalence_grade_basis_resolves_a_query() -> None:
    facts = _standard_facts()
    vocabulary, projections = _projections(facts)

    by_alias = discover_interfaces(
        _request(query="work item state"),
        vocabulary=vocabulary,
        projections=projections,
    )
    assert by_alias.resolved_address == STATUS_CARD_ADDRESS
    assert tuple(item.address for item in by_alias.cards)[0] == STATUS_CARD_ADDRESS

    by_tag = discover_interfaces(
        _request(query="triage"),
        vocabulary=vocabulary,
        projections=projections,
    )
    # The tag validly names both the ClaimType and the Subject; a tag is
    # recall-only, so the page ranks them and resolves nothing.
    assert {item.address.artifact_path for item in by_tag.cards} == {
        STATUS_CARD_ADDRESS.artifact_path
    }
    assert {item.address.artifact_path for item in by_tag.profiles} == {WI1}
    assert by_tag.resolved_address is None
    assert all(
        item.basis in {"tag", "lexical"} for hit in by_tag.page.hits for item in hit.match_basis
    )


# -- budgets ---------------------------------------------------------------


def test_every_clip_is_stated_and_an_unaffordable_card_refuses() -> None:
    facts = _standard_facts()
    _vocabulary, projections = _projections(
        facts,
        budget=InterfaceProjectionBudgetV1(max_terms_per_basis=1, max_predicates=1),
    )
    card = _card(projections)
    profile = _profile(projections)

    assert card.coverage.truncated_facets == ("match_bases",)
    assert "term_budget_exceeded" in card.coverage.reason_codes
    assert card.match_bases[0].terms == ("ClaimType:project.work_item.status",)
    assert len(profile.predicates) == 1
    assert "predicates" in profile.coverage.truncated_facets
    assert "predicate_budget_exceeded" in profile.coverage.reason_codes
    assert profile.coverage.available_facets == ("vocabulary",)

    with pytest.raises(DiscoveryError, match="byte budget"):
        _projections(facts, budget=InterfaceProjectionBudgetV1(max_bytes=1))


def test_an_over_long_object_is_committed_by_digest_instead_of_previewed() -> None:
    facts = _facts((status_claim(1, "wi-1", "ready" * 60), *_descriptors()))
    _vocabulary, projections = _projections(facts)
    status = next(
        item for item in _profile(projections).predicates if item.predicate == STATUS_PREDICATE
    )

    assert status.object_digest is not None
    assert status.object_preview is None
    assert "object_preview_omitted" in _profile(projections).coverage.reason_codes


# -- the interface discovery page and its capsule -------------------------


def test_an_interface_page_projects_every_hit_exactly_once() -> None:
    facts = _standard_facts()
    vocabulary, projections = _projections(facts)
    page = discover_interfaces(
        _request(query=STATUS_PREDICATE),
        vocabulary=vocabulary,
        projections=projections,
    )

    projected = len(page.cards) + len(page.profiles) + len(page.handle_addresses)
    assert projected == len(page.page.hits)
    assert page.cards[0].address == STATUS_CARD_ADDRESS
    # A QueryDefinition has no v1 card, so it stays an explicit handle rather
    # than vanishing from a page that claims to account for its whole result set.
    assert all(
        item.artifact_path.startswith("query-definitions/") for item in page.handle_addresses
    )
    assert page.coverage.available_facets == ("claim_type_card", "handle")


def test_the_same_request_and_coordinate_yield_a_byte_identical_interface_page() -> None:
    facts = _standard_facts()
    vocabulary, projections = _projections(facts)
    request = _request(query=STATUS_PREDICATE)

    first = discover_interfaces(request, vocabulary=vocabulary, projections=projections)
    second = discover_interfaces(
        request,
        vocabulary=_projections(_standard_facts())[0],
        projections=_projections(_standard_facts())[1],
    )
    assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
        second.model_dump(mode="json")
    )
    assert first.receipt_digest == second.receipt_digest


def test_an_interface_capsule_quotes_cards_as_untrusted_data() -> None:
    facts = _standard_facts()
    vocabulary, projections = _projections(facts)
    page = discover_interfaces(
        _request(query=STATUS_PREDICATE),
        vocabulary=vocabulary,
        projections=projections,
    )
    capsule = build_interface_context_capsule(page, budget=ContextCapsuleBudgetV1())

    assert capsule.instruction_blocks == ()
    assert {block.label for block in capsule.data_blocks} == {
        "claim-type-card",
        "discovery-handle",
    }
    assert all(block.material.classification == "untrusted_data" for block in capsule.data_blocks)
    assert capsule.verdict_relative is False
    rendered = render_bounded_context_capsule(capsule)
    assert rendered.count(DATA_FENCE_OPEN.encode("utf-8")) == len(capsule.data_blocks)


def test_a_verdict_relative_profile_makes_its_capsule_say_so() -> None:
    facts = _standard_facts()
    vocabulary, projections = _projections(facts, evaluation_time=NOW)
    page = discover_interfaces(
        _request(query="Ready Queue"),
        vocabulary=vocabulary,
        projections=projections,
    )
    capsule = build_interface_context_capsule(page)

    assert page.profiles and page.profiles[0].verdict_relative is True
    assert capsule.verdict_relative is True
    assert capsule.evaluation_time == EVALUATION_TIME


# -- token economics -------------------------------------------------------


# The representative fixture is one predicate in ordinary use and one Subject in
# ordinary use: eight Subjects each carrying a status Claim, and one Subject
# carrying several Claims per predicate. Collapsing many Claims into one row is
# exactly the profile's job, so a fixture with one Claim per predicate would
# measure a shape no real Subject has. Eight uses is deliberately at the small
# end -- a real predicate has hundreds, and the compact path only widens its
# lead as usage grows -- so the threshold below is a floor, not a best case.
# Canonical byte length is the proxy for token cost: it is deterministic,
# whereas a tokenizer would make the SLO depend on a model's vocabulary. The
# comparator is the *minimum* expansion -- statement, backing, pins, and
# governance envelope per Claim, with no law evidence, verdict, or source
# handles -- so the real saving is larger than what is measured here. The floor
# is 3x: a compact projection that saves less than two thirds of even the
# minimum expansion is not buying an agent enough to justify a second read
# surface, and this test must fail rather than quietly accept it.
COMPACT_PATH_MINIMUM_RATIO = 3

WIDE_ITEMS = ("wi-1", "wi-2", "wi-3", "wi-4", "wi-5", "wi-6", "wi-7", "wi-8")


def _wide_facts() -> ClaimQueryFactsV1:
    return _facts(
        (
            *(
                status_claim(index, item, f"state-{index}")
                for index, item in enumerate(WIDE_ITEMS, start=20)
            ),
            *(status_claim(index, "wi-1", f"contender-{index}") for index in range(30, 33)),
            *(
                claim_fact(
                    index,
                    subject_row=subject("project.work_item", "wi-1"),
                    predicate=REVIEWER_PREDICATE,
                    obj=SubjectClaimObject(
                        address=SemanticAddress.whole_artifact(
                            subject_path("project.work_item", item)
                        )
                    ),
                )
                for index, item in enumerate(WIDE_ITEMS[1:], start=40)
            ),
            *_descriptors(),
        ),
        items=WIDE_ITEMS,
    )


def _row_by_row_expansion(rows: tuple[ClaimFactRowV1, ...]) -> int:
    """The payload an agent pays for when it expands Claim by Claim instead.

    This is exactly what ``expand`` renders per Claim today: the canonical
    statement, the backing, the pins, and the governance envelope.
    """

    return len(
        canonical_bytes(
            [
                {
                    "artifact_digest": row.accepted.artifact_digest,
                    "authority": row.accepted.claim.authority.model_dump(mode="json"),
                    "backing": row.accepted.claim.backing.model_dump(mode="json"),
                    "identity": row.accepted.claim.identity.qualified,
                    "lifecycle": row.accepted.claim.lifecycle.model_dump(mode="json"),
                    "pins": [item.model_dump(mode="json") for item in row.accepted.claim.pins],
                    "statement": row.accepted.claim.statement.model_dump(mode="json"),
                    "statement_digest": row.accepted.statement_digest,
                }
                for row in rows
            ]
        )
    )


def test_a_claim_type_card_is_materially_smaller_than_row_by_row_expansion() -> None:
    facts = _wide_facts()
    _vocabulary, projections = _projections(facts)
    card = _card(projections)
    expansion = _row_by_row_expansion(
        tuple(
            row
            for row in facts.claims
            if row.accepted.claim.statement.predicate == STATUS_PREDICATE
        )
    )
    compact = len(canonical_bytes(card.model_dump(mode="json")))

    assert card.usage.claim_count == len(WIDE_ITEMS) + 3
    assert compact * COMPACT_PATH_MINIMUM_RATIO <= expansion, (
        f"compact ClaimType card is {compact} bytes against {expansion} bytes of "
        f"row-by-row expansion; the compact path must save at least "
        f"{COMPACT_PATH_MINIMUM_RATIO}x to be worth reading"
    )


def test_a_subject_profile_is_materially_smaller_than_row_by_row_expansion() -> None:
    facts = _wide_facts()
    _vocabulary, projections = _projections(facts)
    profile = _profile(projections)
    expansion = _row_by_row_expansion(tuple(row for row in facts.claims if row.subject_path == WI1))
    compact = len(canonical_bytes(profile.model_dump(mode="json")))

    assert len(profile.predicates) >= 3
    assert compact * COMPACT_PATH_MINIMUM_RATIO <= expansion, (
        f"compact Subject profile is {compact} bytes against {expansion} bytes of "
        f"row-by-row expansion; the compact path must save at least "
        f"{COMPACT_PATH_MINIMUM_RATIO}x to be worth reading"
    )
