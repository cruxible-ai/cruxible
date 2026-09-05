"""Batch contender indexing preserves uncached lowering and staged visibility."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import cruxible_core.playbill.authoring.lowering as lowering
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringExistingClaimDispositionV1,
    ClaimRetirementMemberV1,
    ClaimTypeSuccessionMemberV1,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    ClaimStatement,
    LiteralClaimObject,
    claim_path,
    claim_statement_digest,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_change_set_intents import (
    _accept,
    _change_set,
    _claim,
    _coordinator,
)
from tests.test_playbill.test_authoring_disposition_slots import _claim_in_slot, _tree
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _seed_claim_surface
from tests.test_playbill.test_claim_type_migrations_in_change_sets import (
    _affects_package_world,
    _dependent,
    _enum_successor,
    _re_author,
)
from tests.test_playbill.test_claim_type_migrations_in_change_sets import (
    _claim as _migration_claim,
)


def _changed(claim: ClaimArtifactV2, **statement_fields: object) -> ClaimArtifactV2:
    if "predicate" in statement_fields:
        statement_fields["claim_type"] = ArtifactIdentity(
            kind="ClaimType", name=str(statement_fields["predicate"])
        )
    statement = ClaimStatement.model_validate(
        {**claim.statement.model_dump(mode="python"), **statement_fields}
    )
    return claim.model_copy(update={"statement": statement})


def _retired(claim: ClaimArtifactV2) -> ClaimArtifactV2:
    return claim.model_copy(
        update={"lifecycle": claim.lifecycle.model_copy(update={"state": "retired"})}
    )


def _assert_lookup_matches(index, tree: dict[str, bytes], *statements: ClaimStatement) -> None:
    for statement in statements:
        actual = index.claims_for(statement)
        assert actual == lowering._same_predicate_claims(tree, statement)
        assert tuple(
            claim for claim in actual if claim.statement.qualifier == statement.qualifier
        ) == lowering._same_slot_claims(tree, statement)


def test_index_matches_live_predicates_and_qualifier_slots_in_path_order() -> None:
    primary = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier="primary")
    secondary = _claim_in_slot(claim_id="CLM-" + "2" * 32, qualifier="secondary")
    unqualified = _claim_in_slot(claim_id="CLM-" + "3" * 32, qualifier=None)
    other_subject = _changed(
        _claim_in_slot(claim_id="CLM-" + "4" * 32, qualifier=None),
        subject=SemanticAddress.whole_artifact("subjects/project.work_item/other.json"),
    )
    other_predicate = _changed(
        _claim_in_slot(claim_id="CLM-" + "5" * 32, qualifier=None),
        predicate="project.work_item.owner",
    )
    retired = _retired(_claim_in_slot(claim_id="CLM-" + "6" * 32, qualifier="primary"))
    claims = (retired, other_predicate, other_subject, unqualified, secondary, primary)
    tree = {"documents/not-a-claim.json": b"not parsed", **_tree(*claims)}
    index = lowering._ClaimPredicateIndex(tree)

    _assert_lookup_matches(index, tree, *(claim.statement for claim in claims))
    assert tuple(claim.identity.name for claim in index.claims_for(primary.statement)) == (
        primary.identity.name,
        secondary.identity.name,
        unqualified.identity.name,
    )


def test_advance_replaces_old_keys_and_observes_retirement_deletion_and_revival() -> None:
    original = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier=None)
    stable = _claim_in_slot(claim_id="CLM-" + "2" * 32, qualifier="stable")
    tree = _tree(original, stable)
    index = lowering._ClaimPredicateIndex(tree)
    _assert_lookup_matches(index, tree, original.statement)
    moved = _changed(
        original,
        subject=SemanticAddress.whole_artifact("subjects/project.work_item/other.json"),
        predicate="project.work_item.owner",
        qualifier="moved",
    )
    target = claim_path(original.identity.name)
    for replacement in (moved, _retired(moved), None, original):
        updated = dict(tree)
        if replacement is None:
            updated.pop(target)
        else:
            updated[target] = render_claim(replacement)
        updated["documents/not-a-claim.json"] = b"still not parsed"
        index.advance(updated, (target, "documents/not-a-claim.json", target))
        _assert_lookup_matches(
            index, updated, original.statement, moved.statement, stable.statement
        )
        tree = updated


def test_index_build_is_lazy_and_parses_only_changed_claim_paths(monkeypatch) -> None:
    claims = tuple(
        _claim_in_slot(claim_id=f"CLM-{number:032x}", qualifier=str(number))
        for number in range(1, 13)
    )
    tree = _tree(*reversed(claims))
    calls: Counter[str] = Counter()
    original_parse = lowering.parse_claim

    def counted(content, *, path):
        calls[path] += 1
        return original_parse(content, path=path)

    monkeypatch.setattr(lowering, "parse_claim", counted)
    index = lowering._ClaimPredicateIndex(tree)
    index.advance({**tree, "documents/new.json": b"ignored"}, ("documents/new.json",))
    assert not calls
    for claim in claims:
        index.claims_for(claim.statement)
    assert calls == Counter({path: 1 for path in tree})
    changed_path = claim_path(claims[0].identity.name)
    new_claim = _claim_in_slot(claim_id="CLM-" + "f" * 32, qualifier="new")
    new_path = claim_path(new_claim.identity.name)
    updated = {**tree, changed_path: render_claim(_retired(claims[0])), **_tree(new_claim)}
    index.advance(updated, (changed_path, new_path, changed_path))
    for claim in claims:
        index.claims_for(claim.statement)
    assert sum(calls.values()) == len(claims) + 2
    assert calls[changed_path] == 2 and calls[new_path] == 1


def test_unused_index_does_not_parse_a_malformed_base_claim() -> None:
    claim = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier=None)
    malformed = {claim_path(claim.identity.name): b"not a Claim"}
    index = lowering._ClaimPredicateIndex(malformed)
    index.advance(malformed, tuple(malformed))
    with pytest.raises(Exception) as uncached:
        lowering._same_predicate_claims(malformed, claim.statement)
    with pytest.raises(type(uncached.value)) as indexed:
        index.claims_for(claim.statement)
    assert str(indexed.value) == str(uncached.value)


class _UncachedIndex:
    """Reference path: use the pre-optimization lookup on the current staged tree."""

    def __init__(self, tree):
        self.tree = tree

    def advance(self, tree, changed_paths):
        self.tree = tree

    def claims_for(self, statement):
        return lowering._same_predicate_claims(self.tree, statement)


def _compare_lowering(monkeypatch, instance, intent):
    original_index = lowering._ClaimPredicateIndex
    original_parse = lowering.parse_claim
    results = []
    parse_counts = []
    indices = []
    for implementation in (original_index, _UncachedIndex):
        calls = 0

        def counted(content, *, path):
            nonlocal calls
            calls += 1
            return original_parse(content, path=path)

        def make_index(tree):
            index = implementation(tree)
            indices.append(index)
            return index

        with monkeypatch.context() as patch:
            patch.setattr(lowering, "parse_claim", counted)
            patch.setattr(lowering, "_ClaimPredicateIndex", make_index)
            try:
                result = lowering.lower_authoring(instance, intent=intent, actor_id="owner")
            except lowering.AuthoringLoweringError as error:
                result = error
            results.append(result)
            parse_counts.append(calls)
    if isinstance(results[0], lowering.AuthoringLoweringError):
        assert isinstance(results[1], lowering.AuthoringLoweringError)
        assert (
            results[0].code,
            results[0].offending_element,
            results[0].message,
            results[0].repairs,
        ) == (
            results[1].code,
            results[1].offending_element,
            results[1].message,
            results[1].repairs,
        )
    else:
        assert results[0] == results[1]
    return results[0], parse_counts, indices[0]


def test_real_batch_lowers_identical_bytes_without_reparsing_prior_siblings(
    tmp_path: Path, monkeypatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    intent = coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=_change_set(*(_claim(qualifier=f"slot-{number}") for number in range(12))),
        canonical_timestamp=TIMESTAMP,
    ).intent
    result, counts, index = _compare_lowering(monkeypatch, instance, intent)
    assert isinstance(result, lowering.LoweredAuthoring)
    assert counts[0] < counts[1]
    claims = [
        parse_claim(content, path=path)
        for path, content in result.changed_members
        if path.startswith("claims/")
    ]
    assert len(claims) == 12
    _assert_lookup_matches(index, result.proposed_tree, *(claim.statement for claim in claims))


def test_new_contending_sibling_is_visible_to_the_same_typed_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    intent = (
        _coordinator(instance)
        .create(
            actor=AuthenticatedActor(actor_id="owner"),
            payload=_change_set(_claim(value="ready"), _claim(value="blocked")),
            canonical_timestamp=TIMESTAMP,
        )
        .intent
    )
    result, _counts, _index = _compare_lowering(monkeypatch, instance, intent)
    assert isinstance(result, lowering.AuthoringLoweringError)
    assert result.code == "playbill.authoring.existing_claim_dispositions_incomplete"
    assert result.repairs[0].replacement["sibling_members"]


def test_revised_sibling_dispositions_use_its_new_statement_and_retirement_updates_index(
    tmp_path: Path, monkeypatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    seed = coordinator.create(
        actor=actor,
        payload=_change_set(*(_claim(qualifier=f"slot-{number}") for number in range(3))),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, seed.intent_id, actor)
    base = instance.tree_at(instance.accepted_coordinate().git_oid)
    accepted = sorted(
        (
            parse_claim(content, path=path)
            for path, content in base.items()
            if path.startswith("claims/")
        ),
        key=lambda claim: claim.identity.name,
    )
    first, second, retired = accepted
    observed = AuthoringExistingClaimDispositionV1(
        claim_id=first.identity.name, disposition="support"
    )
    intent = coordinator.create(
        actor=actor,
        payload=_change_set(
            _claim(
                claim_ref=first.identity.name, qualifier=first.statement.qualifier, value="done"
            ),
            _claim(
                claim_ref=second.identity.name,
                qualifier=second.statement.qualifier,
                value="blocked",
                dispositions=(observed,),
            ),
            ClaimRetirementMemberV1(claim_ref=retired.identity.name, reason="was-rescinded"),
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    result, _counts, index = _compare_lowering(monkeypatch, instance, intent)
    assert isinstance(result, lowering.LoweredAuthoring)
    first_path = claim_path(first.identity.name)
    revised = parse_claim(result.proposed_tree[first_path], path=first_path)
    second_result = next(
        member
        for member in result.resolved_authoring["members"]
        if member["path"] == claim_path(second.identity.name)
    )
    assert second_result["existing_claim_dispositions"][0]["statement_digest"] == (
        claim_statement_digest(revised.statement).tagged
    )
    _assert_lookup_matches(index, result.proposed_tree, *(claim.statement for claim in accepted))
    assert retired.identity.name not in {
        claim.identity.name for claim in index.claims_for(retired.statement)
    }


def test_succession_consumed_reauthor_and_retired_siblings_are_visible_to_following_claims(
    tmp_path: Path, monkeypatch
) -> None:
    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"), ("wi-2", "blocked"), ("wi-3", "done")),
    )
    successor = _enum_successor(instance, enum=["ready"])
    succession = ClaimTypeSuccessionMemberV1(
        successor=successor,
        dependents=tuple(
            sorted(
                (
                    _dependent(claims["wi-42"], disposition="successor"),
                    _re_author(claims["wi-2"]),
                    _dependent(claims["wi-3"], disposition="retire", reason="was-rescinded"),
                ),
                key=lambda item: item.identity.qualified.encode("utf-8"),
            )
        ),
    )
    repaired = _migration_claim(
        subject_id="wi-2", value=LiteralClaimObject(value="ready"), claim_ref=claims["wi-2"]
    )
    independent = _migration_claim(subject_id="wi-2", value=LiteralClaimObject(value="ready"))
    independent = independent.model_copy(
        update={
            "statement": independent.statement.model_copy(update={"qualifier": "followup"}),
            "existing_claim_dispositions": (
                AuthoringExistingClaimDispositionV1(claim_id=claims["wi-2"], disposition="support"),
            ),
        }
    )
    # No disposition for wi-3's old Claim: the succession retires it before this new Claim.
    replacement = _migration_claim(subject_id="wi-3", value=LiteralClaimObject(value="ready"))
    intent = coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=_change_set(succession, repaired, independent, replacement),
        canonical_timestamp=TIMESTAMP,
    ).intent
    result, _counts, index = _compare_lowering(monkeypatch, instance, intent)
    assert isinstance(result, lowering.LoweredAuthoring)
    final_claims = [
        parse_claim(content, path=path)
        for path, content in result.proposed_tree.items()
        if path.startswith("claims/")
    ]
    _assert_lookup_matches(
        index, result.proposed_tree, *(claim.statement for claim in final_claims)
    )
    revised_path = claim_path(claims["wi-2"])
    revised = parse_claim(result.proposed_tree[revised_path], path=revised_path)
    follower = next(
        member
        for member in result.resolved_authoring["members"]
        if member.get("existing_claim_dispositions")
    )
    assert follower["existing_claim_dispositions"][0]["statement_digest"] == (
        claim_statement_digest(revised.statement).tagged
    )
