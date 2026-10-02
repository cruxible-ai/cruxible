"""`claim-type upgrade`: ClaimTypes before v7 move to v7 only by a reviewed change set."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.claim_types import claim_type_digest
from tests.support.store_snapshot import assert_writes_nothing
from tests.test_claims.test_claim_type_v7_revisions import _backing_bytes, _V7World
from tests.test_claims.test_identity_evidence_rules import (
    IDENTITY,
    ORIGINAL,
    PREDICATE,
    _digest,
    _v6_type,
)


@pytest.fixture
def world(tmp_path: Path) -> _V7World:
    return _V7World(tmp_path)


# --- claim-type upgrade ------------------------------------------------------------------


def _upgrade(world: _V7World, **request: object) -> object:
    from cruxible_client.contracts.claim_type_upgrade import ClaimTypeUpgradeRequestV1
    from cruxible_core.service.claims.claim_type_upgrade import service_upgrade_claim_types

    return service_upgrade_claim_types(
        world.instance,
        request=ClaimTypeUpgradeRequestV1.model_validate(request),
        actor_id="owner",
        timestamp=world.timestamp(),
    )


def test_the_upgrade_moves_v6_to_v7_replace_and_carries_claims_byte_identical(
    world: _V7World,
) -> None:
    from cruxible_core.service.proposals.proposals import service_list_playbill_proposals

    world.seed(_v6_type())
    claim_id = world.say(b"status: ready")
    world.say(b"status: done", value="done", claim_ref=claim_id)
    before = world.claim(claim_id)
    assert len(before.backing.capture_digests) == 2
    tree_before = world.tree()
    proposals_before = service_list_playbill_proposals(world.instance)

    # The upgrade carries every dependent Claim, so it previews by default, on
    # the submission's own path, and writes nothing anywhere (R12).
    dry = assert_writes_nothing([world.instance.root.parent], lambda: _upgrade(world))
    assert dry.status == "would_propose", dry  # type: ignore[attr-defined]
    assert dry.proposal_id is None  # type: ignore[attr-defined]
    assert dry.carried_claims == 1  # type: ignore[attr-defined]
    assert world.tree() == tree_before
    assert service_list_playbill_proposals(world.instance) == proposals_before

    result = _upgrade(world, dry_run=False, at=dry.coordinate.git_oid)  # type: ignore[attr-defined]
    assert result.status == "proposed", result  # type: ignore[attr-defined]
    (entry,) = result.upgraded  # type: ignore[attr-defined]
    assert (
        entry.claim_type,
        entry.from_format,
        entry.revision_evidence_before,
        entry.revision_evidence_after,
        entry.evidence_requirement,
    ) == (f"ClaimType:{PREDICATE}", "playbill-claim-type-v6", "accumulate", "replace", "self")
    assert result.carried_claims == 1  # type: ignore[attr-defined]
    world.activate_proposal(result.proposal_id)  # type: ignore[attr-defined]

    upgraded = world.claim_type()
    assert (upgraded.artifact_format, upgraded.revision_evidence) == (
        "playbill-claim-type-v7",
        "replace",
    )
    carried = world.claim(claim_id)
    assert carried.statement.claim_type_digest == claim_type_digest(upgraded).tagged
    assert _backing_bytes(carried) == _backing_bytes(before)
    assert world.verdict(claim_id) == "supported"
    # From now on a statement-changing revision carries only what it cites.
    world.say(b"status: blocked", value="blocked", claim_ref=claim_id)
    assert len(world.claim(claim_id).backing.capture_digests) == 1

    again = _upgrade(world)
    assert again.status == "unchanged"  # type: ignore[attr-defined]
    assert again.unchanged == (f"ClaimType:{PREDICATE}",)  # type: ignore[attr-defined]


def test_the_upgrade_takes_v5_through_the_identity_conversion_and_can_keep_accumulate(
    world: _V7World,
) -> None:
    from tests.test_claims.test_identity_evidence_rules import _digest_rule, _v5_type

    world.seed(_v5_type(_digest_rule(_digest(ORIGINAL))))
    world.say(b"status: ready")
    result = _upgrade(world, revision_evidence="accumulate", claim_types=[PREDICATE], dry_run=False)
    assert result.status == "proposed", result  # type: ignore[attr-defined]
    (entry,) = result.upgraded  # type: ignore[attr-defined]
    assert (entry.from_format, entry.revision_evidence_after) == (
        "playbill-claim-type-v5",
        "accumulate",
    )
    world.activate_proposal(result.proposal_id)  # type: ignore[attr-defined]
    upgraded = world.claim_type()
    assert (upgraded.artifact_format, upgraded.revision_evidence) == (
        "playbill-claim-type-v7",
        "accumulate",
    )
    assert upgraded.evidence_admission_policy.rules[0].names_capture_contract(
        digest=_digest(ORIGINAL), identity=IDENTITY.qualified
    )


def test_the_upgrade_names_what_it_cannot_find(world: _V7World) -> None:
    world.seed(_v6_type())
    result = _upgrade(world, claim_types=["project.work_item.missing"])
    assert result.status == "unchanged"  # type: ignore[attr-defined]
    assert [item.claim_type for item in result.refused] == [  # type: ignore[attr-defined]
        "ClaimType:project.work_item.missing"
    ]
