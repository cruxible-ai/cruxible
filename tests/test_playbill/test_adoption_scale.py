"""The adoption-scale fixture generator, exercised at a miniature profile.

The Tier-1 benchmark runs the same generator at a thousand generations under
`benchmarks/playbill_adoption_scale`. These cases run it small enough for CI and
assert the properties the benchmark depends on: the declared composition, the
determinism, and that what it builds is a ledger recovery accepts.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection_tree import TreeReadLimits
from cruxible_core.playbill.proposals import ProposalReceiveLimits
from cruxible_core.service.playbill_proposal_receive import ProposalReceiveOperationalConfigV1

from ._adoption_fixture import MINIATURE, TIER_1, build_fixture


def test_the_miniature_fixture_has_its_declared_composition(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, MINIATURE)
    assert fixture.member_count == MINIATURE.expected_members
    # One vocabulary generation, one readings generation, one seed-Claim chunk.
    assert fixture.head_sequence == 3 + MINIATURE.generations

    reopened = PlaybillInstance.open(
        fixture.managed_root,
        trust_root=fixture.instance.trust_root,
    )
    tree = reopened.tree_at(reopened.accepted_coordinate().git_oid)
    kinds = {
        "subjects/": MINIATURE.subjects,
        "claim-types/": MINIATURE.claim_types,
        "documents/": MINIATURE.documents,
        "query-definitions/": MINIATURE.query_definitions,
        "capture-contracts/": 1,
        "claims/": MINIATURE.seed_claims + MINIATURE.generations * MINIATURE.claims_per_generation,
    }
    for prefix, expected in kinds.items():
        assert sum(1 for path in tree if path.startswith(prefix)) == expected, prefix
    # Claims shard by the leading byte of their identity, so a real population
    # spreads over the shard space rather than piling into one directory.
    shards = {path.split("/")[1] for path in tree if path.startswith("claims/")}
    assert len(shards) > 1


def test_the_fixture_is_deterministic(tmp_path: Path) -> None:
    """Two builds of one profile place the same members and the same identities.

    Byte identity stops exactly where the instance's own identity begins: each
    build mints fresh principal keys, so its genesis and every accepted
    coordinate after it differ, and a Claim's Capture backing commits to the
    coordinate it was observed at. Everything the generator itself decides --
    which members exist, at which paths, with which identities, and the bytes of
    every member that does not commit to a coordinate -- is reproduced exactly.
    """

    first = build_fixture(tmp_path / "first", MINIATURE)
    second = build_fixture(tmp_path / "second", MINIATURE)
    left = PlaybillInstance.open(first.managed_root, trust_root=first.instance.trust_root)
    right = PlaybillInstance.open(second.managed_root, trust_root=second.instance.trust_root)
    left_tree = left.tree_at(left.accepted_coordinate().git_oid)
    right_tree = right.tree_at(right.accepted_coordinate().git_oid)

    def members(tree: dict[str, bytes]) -> set[str]:
        return {path for path in tree if not path.startswith(("changesets/", "principals/"))}

    assert members(left_tree) == members(right_tree)

    coordinate_free = ("subjects/", "claim-types/", "query-definitions/", "capture-contracts/")
    for path in sorted(members(left_tree)):
        if path.startswith(coordinate_free):
            assert left_tree[path] == right_tree[path], path


def test_the_tier_one_profile_declares_the_gated_scale() -> None:
    """The benchmark profile is the one §12.3's Tier 1 names, not a smaller stand-in."""

    assert TIER_1.expected_members >= 5_000
    assert TIER_1.generations == 1_000
    assert TIER_1.claims_per_generation > 1


def test_pre_pcg_bounded_limits_admit_the_gated_scale() -> None:
    """The raised read and receive ceilings clear the fixture with headroom."""

    read = TreeReadLimits()
    receive = ProposalReceiveLimits()
    assert read.max_files >= 250_000
    assert read.max_total_bytes >= 512 * 1024 * 1024
    assert read.max_blob_bytes == 64 * 1024 * 1024
    assert receive.max_files >= TIER_1.expected_members
    assert receive.max_changed_members >= TIER_1.claims_per_generation
    # The advertised member budget and the settleable one are the same number:
    # a submission of `max_changed_members` entries projects to a change-set
    # record that fits under the per-blob ceiling it is written against.
    assert receive.max_change_set_record_bytes == read.max_blob_bytes
    assert (
        receive.projected_change_set_record_bytes(receive.max_changed_members)
        <= read.max_blob_bytes
    )
    assert receive.max_path_depth >= 3
    # The changed-member bound is an operator admission knob whose default is
    # exactly this ratified one; the daemon file moves the ceiling, not the
    # pin, so this still pins what an unconfigured daemon admits.
    assert ProposalReceiveOperationalConfigV1().max_changed_members == receive.max_changed_members
    assert ProposalReceiveOperationalConfigV1().limits() == receive


def test_an_interrupted_build_resumes_where_it_stopped(tmp_path: Path) -> None:
    """A resumed build lands on the same population an uninterrupted one does.

    A Tier-1 build runs for the better part of an hour, so an interruption that
    forced a restart from nothing would cost the whole of it. Resumption reads
    its position out of the accepted tree, so this asserts the position it reads
    is the one an uninterrupted build would have reached.
    """

    interrupted = replace(MINIATURE, generations=2)
    partial = build_fixture(tmp_path, interrupted)
    assert partial.head_sequence == 3 + interrupted.generations

    finished = build_fixture(tmp_path, MINIATURE, resume=True)
    assert finished.head_sequence == 3 + MINIATURE.generations
    assert finished.member_count == MINIATURE.expected_members

    reference = build_fixture(tmp_path / "reference", MINIATURE)
    resumed_instance = PlaybillInstance.open(
        finished.managed_root, trust_root=finished.instance.trust_root
    )
    reference_instance = PlaybillInstance.open(
        reference.managed_root, trust_root=reference.instance.trust_root
    )

    def members(instance: PlaybillInstance) -> set[str]:
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        return {path for path in tree if not path.startswith(("changesets/", "principals/"))}

    assert members(resumed_instance) == members(reference_instance)
