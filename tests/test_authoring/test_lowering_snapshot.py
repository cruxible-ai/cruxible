"""Lowering borrows immutable accepted state and owns only its member changes."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import cruxible_core.authoring.lowering as lowering
import cruxible_core.derived.derived_state as derived_state
from cruxible_client.contracts.claims import claim_path, parse_claim, render_claim
from cruxible_core.derived.derived_state import SnapshotTree, fork_tree
from cruxible_core.proposals.proposals import AuthenticatedActor
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_change_set_intents import (
    _accept,
    _change_set,
    _claim,
    _coordinator,
    _shell,
)
from tests.test_authoring.test_authoring_disposition_slots import _claim_in_slot, _tree
from tests.test_authoring.test_authoring_preflight import TIMESTAMP, _seed_claim_surface


def test_lowered_result_seals_the_mutable_candidate_without_claim_parsing(monkeypatch) -> None:
    claim = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier=None)
    path = claim_path(claim.identity.name)
    original = render_claim(claim)
    candidate = fork_tree({path: original})

    def unexpected_parse(*args, **kwargs):
        pytest.fail("sealing a lowered tree must not eagerly parse Claims")

    monkeypatch.setattr(derived_state, "parse_claim", unexpected_parse)
    lowered = lowering.LoweredAuthoring(
        proposed_tree=candidate,
        resolved_authoring={},
        changed_members=((path, original),),
    )
    candidate[path] = b"later member bytes"
    assert isinstance(lowered.proposed_tree, SnapshotTree)
    assert lowered.proposed_tree[path] == original
    with pytest.raises(TypeError):
        lowered.proposed_tree[path] = b"cannot mutate a published result"  # type: ignore[index]


def test_snapshot_index_advances_candidates_without_mutating_accepted_or_siblings() -> None:
    original = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier=None)
    path = claim_path(original.identity.name)
    parent = fork_tree(_tree(original)).snapshot()
    left = parent.fork()
    right = parent.fork()
    index = lowering._ClaimPredicateIndex(left)
    assert index.claims_for(original.statement) == (original,)
    retired = original.model_copy(
        update={"lifecycle": original.lifecycle.model_copy(update={"state": "retired"})}
    )
    left[path] = render_claim(retired)
    index.advance(left, (path,))
    assert index.claims_for(original.statement) == ()
    assert parent.claims_for(original.statement) == (original,)
    assert right.claims_for(original.statement) == (original,)


def test_fresh_singleton_lowerings_share_warm_accepted_contenders(
    tmp_path: Path, monkeypatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner, additional_subjects=(_shell("unrelated"),))
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    seed = coordinator.create(
        actor=actor,
        payload=_change_set(
            *(_claim(subject_id="unrelated", qualifier=f"seed-{number}") for number in range(8))
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, seed.intent_id, actor)
    base = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    accepted_claims = {
        path: parse_claim(content, path=path)
        for path, content in base.items()
        if path.startswith("claims/")
    }
    assert len(accepted_claims) == 8
    statement = next(iter(accepted_claims.values())).statement
    # Cold discovery is allowed once; a fresh draft must reuse that accepted root.
    base.claims_for(statement)
    calls: Counter[str] = Counter()
    original_parse = derived_state.parse_claim

    def counted(content, *, path):
        calls[path] += 1
        return original_parse(content, path=path)

    monkeypatch.setattr(derived_state, "parse_claim", counted)
    for number in range(3):
        intent = coordinator.create(
            actor=actor,
            payload=_claim(qualifier=f"fresh-{number}"),
            canonical_timestamp=TIMESTAMP,
        ).intent
        result = lowering.lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
        assert isinstance(result.proposed_tree, SnapshotTree)
        assert len(result.changed_members) == 1
    assert not (set(calls) & set(accepted_claims)), calls
    assert base.claims_for(statement) == lowering._same_predicate_claims(base, statement)
