"""The read verbs show what a ClaimType v7 says: orient descriptors and the get card."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.get_reads import PlaybillGetClaimTypeCardV1, PlaybillGetRequestV1
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import OrientCaller, service_playbill_orient
from tests.test_claims.test_claim_type_v7_revisions import _V7World, v7_type
from tests.test_claims.test_identity_evidence_rules import PREDICATE, SOURCE, _v6_type

OWNER = OrientCaller("owner", "active", "admin")
KIND = "project.work_item"
DESCRIPTION = (
    "Where the work item stands in its lifecycle, as the tracker last reported it. "
    "Blocked items name what they wait on."
)


def _described() -> object:
    return v7_type(
        description=DESCRIPTION,
        member_descriptions=(
            {"member": "done", "description": "Finished and verified."},
            {"member": "ready", "description": "Can start now."},
        ),
        default_role="observation",
        evidence_requirement="none",
    )


def _descriptor(world: _V7World, *, full: bool) -> object:
    result = service_playbill_orient(world.instance, kind=KIND if full else None, caller=OWNER)
    kinds = (
        [result.kind_detail] if full else [item for item in result.kinds or () if item.kind == KIND]
    )
    (kind,) = kinds
    (descriptor,) = [item for item in kind.predicates if item.predicate == PREDICATE]
    return descriptor


def _card(world: _V7World) -> PlaybillGetClaimTypeCardV1:
    card = service_playbill_get(
        world.instance,
        request=PlaybillGetRequestV1(
            ref=f"ClaimType:{PREDICATE}", evaluation_time=datetime(2026, 8, 22, tzinfo=UTC)
        ),
        access=BodyAccessContext(principal_id="reader", can_read_body=True),
    ).card
    assert isinstance(card, PlaybillGetClaimTypeCardV1)
    return card


@pytest.fixture
def world(tmp_path: Path) -> _V7World:
    return _V7World(tmp_path)


def test_orient_shows_a_compact_meaning_and_the_full_one_under_kind(world: _V7World) -> None:
    world.seed(_described())  # type: ignore[arg-type]

    compact = _descriptor(world, full=False)
    assert compact.description == (  # type: ignore[attr-defined]
        "Where the work item stands in its lifecycle, as the tracker last reported it."
    )
    assert compact.evidence_requirement == "none"  # type: ignore[attr-defined]
    wire = compact.model_dump(mode="json")  # type: ignore[attr-defined]
    assert not {"member_descriptions", "default_role", "revision_evidence"} & set(wire)

    full = _descriptor(world, full=True)
    assert full.description == DESCRIPTION  # type: ignore[attr-defined]
    assert [(item.member, item.description) for item in full.member_descriptions] == [  # type: ignore[attr-defined]
        ("done", "Finished and verified."),
        ("ready", "Can start now."),
    ]
    assert full.default_role == "observation"  # type: ignore[attr-defined]
    assert full.revision_evidence == "replace"  # type: ignore[attr-defined]


def test_orient_leaves_a_pre_v7_descriptor_as_it_was_but_names_accumulate_in_full(
    world: _V7World,
) -> None:
    world.seed(_v6_type())
    wire = _descriptor(world, full=False).model_dump(mode="json")  # type: ignore[attr-defined]
    assert not {"description", "evidence_requirement", "member_descriptions"} & set(wire)
    assert _descriptor(world, full=True).revision_evidence == "accumulate"  # type: ignore[attr-defined]


def test_a_long_first_sentence_is_capped_in_the_compact_descriptor(world: _V7World) -> None:
    world.seed(v7_type(description="x" * 200 + ". More."))
    compact = _descriptor(world, full=False)
    assert len(compact.description) == 160  # type: ignore[attr-defined]
    assert compact.description.endswith("…")  # type: ignore[attr-defined]


def test_the_get_card_names_roles_rules_members_and_evidence_semantics(world: _V7World) -> None:
    world.seed(_described())  # type: ignore[arg-type]
    card = _card(world)
    assert card.description == DESCRIPTION
    assert card.members == ("blocked", "done", "ready")
    assert [(item.member, item.description) for item in card.member_descriptions] == [
        ("done", "Finished and verified."),
        ("ready", "Can start now."),
    ]
    assert card.roles == ("normative", "observation")
    assert card.default_role == "observation"
    assert (card.evidence_requirement, card.revision_evidence) == ("none", "replace")
    (rule,) = card.evidence_rules
    assert (rule.rule_id, rule.roles, rule.contracts, rule.admission) == (
        "source",
        ("normative", "observation"),
        (f"CaptureContract:playbill.foreign-source.{SOURCE}",),
        "direct",
    )


def test_the_get_card_of_a_pre_v7_claim_type_states_its_implied_semantics(
    world: _V7World,
) -> None:
    world.seed(_v6_type())
    card = _card(world)
    assert (card.evidence_requirement, card.revision_evidence) == ("self", "accumulate")
    wire = card.model_dump(mode="json")
    assert not {"description", "member_descriptions", "default_role"} & set(wire)
