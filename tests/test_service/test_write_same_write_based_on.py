"""``based_on`` naming a Claim the same write makes (intra-batch pinning).

A shorthand (``kind/id#field``, or ``label#field`` for a Subject a create change
labels) names its slot's one Claim as the whole write leaves it: a sibling's new
Claim pins under the id lowering mints for it, a sibling's revision under its
own id at the version this change set writes. Lowering stages the sibling first,
so the pin carries the sibling's exact artifact digest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.claims import (
    BASED_ON_CLAIM_PIN_ROLE,
    claim_artifact_digest,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.text_display import render_review_summary, render_write_outcome
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.proposals.summary import service_review_summary
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import KIND, caller, seed_write_surface

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
_READER = BodyAccessContext(principal_id="reviewer", can_read_body=True)


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return seed_write_surface(tmp_path)[0]


def _write(
    instance: PlaybillInstance,
    *changes: dict[str, Any],
    who: WriteCaller | None = None,
    **options: Any,
) -> WriteOutcome:
    request = WriteRequest.model_validate(
        {"because": "The writer checked it.", "changes": list(changes), **options}
    )
    return service_playbill_write(instance, request=request, caller=who or caller())


def _create(label: str, slug: str) -> dict[str, Any]:
    return {"op": "create", "label": label, "subject_kind": KIND, "key": slug}


def _set(subject: object, field: str, value: object, **extra: object) -> dict[str, Any]:
    return {"op": "set", "subject": subject, "field": field, "value": value, **extra}


def _add(subject: object, field: str, value: object, **extra: object) -> dict[str, Any]:
    return {"op": "add", "subject": subject, "field": field, "value": value, **extra}


def _claim_of(outcome: WriteOutcome, field: str) -> str:
    (claim,) = [item.claim for item in outcome.changes if item.field == field]
    assert claim is not None
    return claim.removeprefix("Claim:")


def _artifact(instance: PlaybillInstance, claim_id: str) -> Any:
    path = claim_path(claim_id)
    head = instance.accepted_coordinate()
    content = instance.blob_at(head.git_oid, path)
    assert content is not None, claim_id
    return parse_claim(content, path=path)


def _pins(instance: PlaybillInstance, claim_id: str) -> dict[str, str]:
    return {
        pin.target.name: pin.artifact_digest
        for pin in _artifact(instance, claim_id).pins
        if pin.role == BASED_ON_CLAIM_PIN_ROLE
    }


def _assert_pins_sibling(instance: PlaybillInstance, judged: str, sibling: str) -> None:
    # The exact minted id, at the exact version this change set wrote.
    assert _pins(instance, judged) == {
        sibling: claim_artifact_digest(_artifact(instance, sibling)).tagged
    }


def _refusal(outcome: WriteOutcome) -> Any:
    assert outcome.status != "accepted", outcome
    assert outcome.refusal is not None, outcome
    return outcome.refusal


# -- new Subjects ---------------------------------------------------------------------


@pytest.mark.parametrize("named", ["label", "kind_id"])
@pytest.mark.parametrize("reverse", [False, True])
def test_a_new_subjects_claim_pins_a_sibling_new_subjects_claim(
    instance: PlaybillInstance, named: str, reverse: bool
) -> None:
    pin = "a#status" if named == "label" else f"{KIND}/wi-9#status"
    observed = _set({"created": "a"}, "status", "ready")
    judged = _set({"created": "b"}, "title", "Ready to ship", based_on=[pin])
    pair = (judged, observed) if reverse else (observed, judged)
    outcome = _write(instance, _create("a", "wi-9"), _create("b", "wi-10"), *pair)
    assert outcome.status == "accepted", outcome.model_dump(mode="json")

    status, title = _claim_of(outcome, "status"), _claim_of(outcome, "title")
    _assert_pins_sibling(instance, title, status)
    (shown,) = [item for item in outcome.changes if item.field == "title"]
    assert shown.based_on == (status,)


def test_an_existing_subjects_revision_pins_at_the_version_this_write_makes(
    instance: PlaybillInstance,
) -> None:
    first = _write(instance, _set(WI1, "status", "ready"))
    assert first.status == "accepted", first
    status = _claim_of(first, "status")
    before = claim_artifact_digest(_artifact(instance, status)).tagged

    outcome = _write(
        instance,
        # The pinning change first: the order of changes carries no meaning.
        _set(WI2, "title", "Follows wi-1", based_on=[f"{WI1}#status"]),
        _set(WI1, "status", "done"),
    )
    assert outcome.status == "accepted", outcome.model_dump(mode="json")
    assert _claim_of(outcome, "status") == status  # revised, same lineage
    _assert_pins_sibling(instance, _claim_of(outcome, "title"), status)
    assert _pins(instance, _claim_of(outcome, "title"))[status] != before


def test_an_existing_subjects_new_claim_pins_under_its_minted_id(
    instance: PlaybillInstance,
) -> None:
    outcome = _write(
        instance,
        _set(WI2, "title", "Follows wi-1", based_on=[f"{WI1}#status"]),
        _set(WI1, "status", "ready"),
    )
    assert outcome.status == "accepted", outcome.model_dump(mode="json")
    _assert_pins_sibling(instance, _claim_of(outcome, "title"), _claim_of(outcome, "status"))


def test_a_chain_of_same_write_pins_stages_each_after_what_it_rests_on(
    instance: PlaybillInstance,
) -> None:
    outcome = _write(
        instance,
        _create("a", "wi-9"),
        _create("b", "wi-10"),
        _set({"created": "b"}, "priority", "high", role="normative", based_on=["b#title"]),
        _set({"created": "b"}, "title", "Second", based_on=["a#status"]),
        _set({"created": "a"}, "status", "ready"),
    )
    assert outcome.status == "accepted", outcome.model_dump(mode="json")
    _assert_pins_sibling(instance, _claim_of(outcome, "title"), _claim_of(outcome, "status"))
    _assert_pins_sibling(instance, _claim_of(outcome, "priority"), _claim_of(outcome, "title"))


# -- proposal, dry run, review ---------------------------------------------------------


def test_a_same_write_pin_survives_proposal_review_and_activation(
    instance: PlaybillInstance,
) -> None:
    changes = (
        _create("a", "wi-9"),
        _create("b", "wi-10"),
        _set({"created": "b"}, "title", "Ready to ship", based_on=["a#status"]),
        _set({"created": "a"}, "status", "ready"),
    )
    preview = _write(instance, *changes, dry_run=True)
    assert preview.status == "would_accept", preview.model_dump(mode="json")
    (planned,) = [item for item in preview.changes if item.field == "title"]
    (observed,) = [item for item in preview.changes if item.field == "status"]
    assert observed.claim is not None
    assert planned.based_on == (observed.claim.removeprefix("Claim:"),)
    assert f"based on {observed.claim.removeprefix('Claim:')}" in render_write_outcome(preview)

    kept = _write(instance, *changes, accept="never")
    assert kept.status == "awaiting_approval" and kept.proposal is not None, kept
    status, title = _claim_of(kept, "status"), _claim_of(kept, "title")
    (shown,) = [item for item in kept.changes if item.field == "title"]
    assert shown.based_on == (status,)
    summary = service_review_summary(
        instance, proposal_id=kept.proposal.proposal_id, access=_READER
    )
    (reviewed,) = [item for item in summary.changes if item.change.field == "title"]
    (pin,) = reviewed.based_on
    assert pin.claim == status and pin.field == "status"
    assert f"based on {status}" in render_review_summary(
        summary, command="cruxible proposal review P"
    )

    receipt = service_activate_playbill_proposal(
        instance, proposal_id=kept.proposal.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    _assert_pins_sibling(instance, title, status)


# -- refusals ----------------------------------------------------------------------------


def test_a_change_pinning_the_slot_it_writes_refuses_as_self(instance: PlaybillInstance) -> None:
    before = instance.accepted_coordinate()
    refused = _refusal(
        _write(
            instance,
            _create("a", "wi-9"),
            _set({"created": "a"}, "status", "ready", based_on=["a#status"]),
        )
    )
    assert refused.code == "cruxible.authoring.based_on_self"
    assert refused.field_path == "changes[1].based_on"
    assert instance.accepted_coordinate() == before


def test_pins_resting_on_each_other_refuse_as_a_cycle(instance: PlaybillInstance) -> None:
    refused = _refusal(
        _write(
            instance,
            _create("a", "wi-9"),
            _create("b", "wi-10"),
            _set({"created": "a"}, "title", "A", based_on=["b#title"]),
            _set({"created": "b"}, "title", "B", based_on=["a#title"]),
        )
    )
    assert refused.code == "cruxible.write.based_on_cycle"
    assert set(refused.candidates) == {"changes[2]", "changes[3]"}


def test_two_values_this_write_adds_to_one_slot_refuse_as_ambiguous(
    instance: PlaybillInstance,
) -> None:
    refused = _refusal(
        _write(
            instance,
            _add(WI1, "governs", f"{KIND}/wi-2"),
            _add(WI1, "governs", f"{KIND}/wi-3"),
            _set(WI2, "title", "Governed", based_on=[f"{WI1}#governs"]),
        )
    )
    assert refused.code == "cruxible.write.based_on_ambiguous"
    assert set(refused.candidates) == {"changes[0]", "changes[1]"}


def test_a_live_value_beside_one_this_write_adds_refuses_as_ambiguous(
    instance: PlaybillInstance,
) -> None:
    linked = _write(instance, _add(WI1, "governs", f"{KIND}/wi-2"))
    assert linked.status == "accepted", linked
    live = _claim_of(linked, "governs")
    refused = _refusal(
        _write(
            instance,
            _add(WI1, "governs", f"{KIND}/wi-3"),
            _set(WI2, "title", "Governed", based_on=[f"{WI1}#governs"]),
        )
    )
    assert refused.code == "cruxible.write.based_on_ambiguous"
    assert set(refused.candidates) == {live, "changes[0]"}


def test_a_field_the_new_subject_does_not_set_refuses_as_not_found(
    instance: PlaybillInstance,
) -> None:
    refused = _refusal(
        _write(
            instance,
            _create("a", "wi-9"),
            _set({"created": "a"}, "status", "ready"),
            _set(WI2, "title", "Follows", based_on=["a#title"]),
        )
    )
    assert refused.code == "cruxible.write.based_on_not_found"
    assert "set title in this write" in (refused.repair or "")


def test_a_label_no_create_change_gives_refuses_as_unknown(instance: PlaybillInstance) -> None:
    refused = _refusal(_write(instance, _set(WI2, "title", "Follows", based_on=["nope#status"])))
    assert refused.code == "cruxible.write.label_unknown"
    assert refused.field_path == "changes[0].based_on"


def test_dispositions_follow_the_staging_order_a_pin_forces(instance: PlaybillInstance) -> None:
    # Two adds to one many-valued slot, one resting on a third change's Claim:
    # whichever stages second dispositions the first, in the order lowering
    # stages them, not in canonical order.
    outcome = _write(
        instance,
        _add(WI1, "governs", f"{KIND}/wi-2", based_on=[f"{KIND}/wi-3#title"]),
        _add(WI1, "governs", f"{KIND}/wi-3"),
        _set(f"{KIND}/wi-3", "title", "Root"),
    )
    assert outcome.status == "accepted", outcome.model_dump(mode="json")
    (pinning,) = [
        item.claim
        for item in outcome.changes
        if item.field == "governs" and item.after == f"{KIND}/wi-2"
    ]
    assert pinning is not None
    _assert_pins_sibling(instance, pinning.removeprefix("Claim:"), _claim_of(outcome, "title"))


def test_lowering_stages_a_pinned_sibling_first_and_refuses_a_cycle() -> None:
    from cruxible_client.contracts.authoring.models import ClaimAuthoringPayload
    from cruxible_core.authoring.lowering import AuthoringLoweringError, claim_staging_order

    ids = ["CLM-" + digit * 32 for digit in "abc"]

    def member(claim_id: str, *based_on: str) -> ClaimAuthoringPayload:
        return ClaimAuthoringPayload.model_construct(revises=claim_id, based_on=based_on or None)

    claim_ids = {f"Claim:{claim_id}": claim_id for claim_id in ids}
    members = [member(ids[0], ids[2]), member(ids[1]), member(ids[2])]
    assert claim_staging_order(members, range(3), claim_ids) == [1, 2, 0]
    # No sibling pin: canonical order, untouched.
    plain = [member(ids[0]), member(ids[1]), member(ids[2], "CLM-" + "d" * 32)]
    assert claim_staging_order(plain, range(3), claim_ids) == [0, 1, 2]
    cyclic = [member(ids[0], ids[1]), member(ids[1], ids[0]), member(ids[2])]
    with pytest.raises(AuthoringLoweringError) as caught:
        claim_staging_order(cyclic, range(3), claim_ids)
    assert caught.value.code == "cruxible.authoring.based_on_cycle"


# -- key Claims of new Subjects, and new Subjects named by id ---------------------------


def _live_claims(instance: PlaybillInstance, subject: str, predicate: str) -> list[str]:
    kind, _, subject_id = subject.partition("/")
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        return [
            str(row[0]).removeprefix("Claim:")
            for row in projection.typed.connection.execute(
                "SELECT identity FROM claims WHERE subject_path=? AND predicate=? "
                "AND lifecycle='live'",
                (f"subjects/{kind}/{subject_id}.json", predicate),
            )
        ]


@pytest.mark.parametrize("named", ["label", "kind_id"])
@pytest.mark.parametrize("restated", [False, True])
def test_a_new_subjects_key_claim_pins_under_the_id_its_creation_gives(
    instance: PlaybillInstance, named: str, restated: bool
) -> None:
    pin = "a#slug" if named == "label" else f"{KIND}/wi-9#slug"
    # Restating the key is subsumed by the creation: still one Claim in the slot.
    restate = (_set({"created": "a"}, "slug", "wi-9"),) if restated else ()
    outcome = _write(
        instance,
        _create("a", "wi-9"),
        *restate,
        _set(WI2, "title", "Follows wi-9", based_on=[pin]),
    )
    assert outcome.status == "accepted", outcome.model_dump(mode="json")
    (key_claim,) = _live_claims(instance, f"{KIND}/wi-9", f"{KIND}.slug")
    _assert_pins_sibling(instance, _claim_of(outcome, "title"), key_claim)
    (shown,) = [item for item in outcome.changes if item.field == "title"]
    assert shown.based_on == (key_claim,)


def test_a_key_claim_pin_previews_and_survives_a_proposal(instance: PlaybillInstance) -> None:
    changes = (_create("a", "wi-9"), _set(WI2, "title", "Follows wi-9", based_on=["a#slug"]))
    preview = _write(instance, *changes, dry_run=True)
    assert preview.status == "would_accept", preview.model_dump(mode="json")

    kept = _write(instance, *changes, accept="never")
    assert kept.status == "awaiting_approval" and kept.proposal is not None, kept
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=kept.proposal.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    (key_claim,) = _live_claims(instance, f"{KIND}/wi-9", f"{KIND}.slug")
    _assert_pins_sibling(instance, _claim_of(kept, "title"), key_claim)


# A neutral pair world: a seat is keyed by its member and its team.
MEMBER = "proj.member"
TEAM = "proj.team"
SEAT = "proj.seat"
M1 = f"{MEMBER}/m-1"
T1 = f"{TEAM}/t-1"
S11 = f"{SEAT}/m-1--t-1"


@pytest.fixture
def seats(tmp_path: Path) -> PlaybillInstance:
    from cruxible_client.contracts.authoring.inputs import ClaimTypeInput, SubjectKindInput
    from tests.core_support._identity_support import identity_claim_type
    from tests.core_support._pair_world import accept, accepted, change_set, create, write
    from tests.core_support._support import initialize_local

    def text(kind: str, field: str, *, key: bool = False) -> Any:
        return identity_claim_type(
            field,
            value_type={"type": "string"},
            canonical_form=("nfkc", "trim", "casefold") if key else None,
            predicate=f"{kind}.{field}",
            subject_kinds=(kind,),
        )

    def end(field: str, kind: str) -> Any:
        return identity_claim_type(
            field,
            value_type=(kind,),
            canonical_form=None,
            predicate=f"{SEAT}.{field}",
            subject_kinds=(SEAT,),
        )

    seeded, _owner = initialize_local(tmp_path)
    types = (
        text(MEMBER, "name", key=True),
        text(TEAM, "name", key=True),
        text(MEMBER, "note"),
        text(SEAT, "basis"),
        end("member", MEMBER),
        end("team", TEAM),
    )
    accept(
        seeded,
        change_set(
            *(ClaimTypeInput(kind="claim_type", claim_type=item) for item in types),
            SubjectKindInput(kind="subject_kind", name=MEMBER, keys=(f"{MEMBER}.name",)),
            SubjectKindInput(kind="subject_kind", name=TEAM, keys=(f"{TEAM}.name",)),
            SubjectKindInput(
                kind="subject_kind", name=SEAT, keys=(f"{SEAT}.member", f"{SEAT}.team")
            ),
        ),
    )
    accepted(write(seeded, create("m", MEMBER, "m-1"), create("t", TEAM, "t-1")))
    return seeded


def _seat_write(instance: PlaybillInstance, *changes: dict[str, Any], **options: Any) -> Any:
    from tests.core_support._pair_world import create, write

    return write(instance, create("e", SEAT, {"member": M1, "team": T1}), *changes, **options)


@pytest.mark.parametrize(
    "pins",
    [["e#basis"], [f"{S11}#basis"], ["e#basis", f"{S11}#basis"]],
    ids=["label", "kind_id", "both"],
)
def test_a_new_pair_is_pinned_alike_by_its_label_and_its_determined_id(
    seats: PlaybillInstance, pins: list[str]
) -> None:
    outcome = _seat_write(
        seats,
        _set({"created": "e"}, "basis", "agreed"),
        _set(M1, "note", "Seated", based_on=pins),
    )
    assert outcome.status == "accepted", outcome.model_dump(mode="json")
    basis, note = _claim_of(outcome, "basis"), _claim_of(outcome, "note")
    _assert_pins_sibling(seats, note, basis)
    (shown,) = [item for item in outcome.changes if item.field == "note"]
    assert shown.based_on == (basis,)


def test_a_new_pairs_key_claim_is_pinned_by_its_determined_id(seats: PlaybillInstance) -> None:
    outcome = _seat_write(seats, _set(M1, "note", "Seated", based_on=[f"{S11}#member"]))
    assert outcome.status == "accepted", outcome.model_dump(mode="json")
    (key_claim,) = _live_claims(seats, S11, f"{SEAT}.member")
    _assert_pins_sibling(seats, _claim_of(outcome, "note"), key_claim)


def test_a_new_pair_named_by_id_previews_like_its_label(seats: PlaybillInstance) -> None:
    changes = (
        _set({"created": "e"}, "basis", "agreed"),
        _set(M1, "note", "Seated", based_on=[f"{S11}#basis"]),
    )
    preview = _seat_write(seats, *changes, dry_run=True)
    assert preview.status == "would_accept", preview.model_dump(mode="json")
    (planned,) = [item for item in preview.changes if item.field == "note"]
    (basis,) = [item for item in preview.changes if item.field == "basis"]
    assert basis.claim is not None
    assert planned.based_on == (basis.claim.removeprefix("Claim:"),)


def test_a_self_pin_and_a_cycle_are_caught_across_both_spellings(
    seats: PlaybillInstance,
) -> None:
    selfish = _seat_write(
        seats, _set({"created": "e"}, "basis", "agreed", based_on=[f"{S11}#basis"])
    )
    assert _refusal(selfish).code == "cruxible.authoring.based_on_self"
    cyclic = _seat_write(
        seats,
        _set({"created": "e"}, "basis", "agreed", based_on=[f"{M1}#note"]),
        _set(M1, "note", "Seated", based_on=[f"{S11}#basis"]),
    )
    refused = _refusal(cyclic)
    assert refused.code == "cruxible.write.based_on_cycle"
    assert set(refused.candidates) == {"changes[1]", "changes[2]"}


# -- a ClaimType succession re-authoring Claims that rest on each other -----------------


@pytest.mark.parametrize("pinned_first", [False, True])
def test_a_succession_re_authors_a_claim_resting_on_a_sibling_re_authored_one(
    instance: PlaybillInstance, pinned_first: bool
) -> None:
    import base64

    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        ChangeSetAuthoringPayload,
        ClaimAuthoringPayload,
        ClaimTypeSuccessionDependent,
        ClaimTypeSuccessionMember,
        SelfSourceBody,
        authoring_member_identity,
    )
    from cruxible_client.contracts.claim_types import (
        claim_type_digest,
        claim_type_path,
        parse_claim_type,
    )
    from cruxible_client.contracts.claims import LiteralClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_client.contracts.subjects import subject_path
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from tests.core_support._write_support import OWNER
    from tests.support.claim_types import claim_type_changes

    seeded = _write(instance, _set(WI1, "status", "ready"), _set(WI2, "status", "blocked"))
    assert seeded.status == "accepted", seeded
    (first,) = [item.claim for item in seeded.changes if item.subject == WI1]
    (second,) = [item.claim for item in seeded.changes if item.subject == WI2]
    assert first is not None and second is not None
    first, second = first.removeprefix("Claim:"), second.removeprefix("Claim:")
    # Which Claim rests on which, so either sorts first among the members.
    pinning, pinned = (second, first) if pinned_first else (first, second)

    type_path = claim_type_path(f"{KIND}.status")
    head = instance.accepted_coordinate()
    current = parse_claim_type(instance.tree_at(head.git_oid)[type_path], path=type_path)
    successor = current.model_copy(
        update=claim_type_changes(
            value_type={"enum": ["archived", "blocked", "done", "ready"], "type": "string"},
            lifecycle=ArtifactLifecycle(predecessor_digest=claim_type_digest(current).tagged),
        )
    )

    def re_authored(claim_id: str, subject_id: str, *based_on: str) -> ClaimAuthoringPayload:
        return ClaimAuthoringPayload(
            statement=AuthoringClaimStatement(
                subject=SemanticAddress.whole_artifact(subject_path(KIND, subject_id)),
                predicate=f"{KIND}.status",
                object=LiteralClaimObject(value="done"),
                role="observation",
            ),
            rationale="Said again under the widened vocabulary.",
            source=SelfSourceBody(content_base64=base64.b64encode(b"status: done\n").decode()),
            revises=claim_id,
            based_on=based_on or None,
        )

    subject_of = {first: "wi-1", second: "wi-2"}
    members = [
        re_authored(pinning, subject_of[pinning], pinned),
        re_authored(pinned, subject_of[pinned]),
        ClaimTypeSuccessionMember(
            successor=successor,
            dependents=tuple(
                sorted(
                    (
                        ClaimTypeSuccessionDependent(
                            identity=ArtifactIdentity(kind="Claim", name=claim_id),
                            disposition="re_author",
                            successor_claim_id=claim_id,
                        )
                        for claim_id in (first, second)
                    ),
                    key=lambda item: item.identity.qualified.encode("utf-8"),
                )
            ),
        ),
    ]
    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(members, key=lambda item: authoring_member_identity(item).encode("utf-8"))
        )
    )
    submitted = AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=OWNER, payload=payload, canonical_timestamp="2026-09-29T12:30:00.000000Z"
    )
    assert submitted.status.proposal_id is not None, submitted.intent.last_preflight
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt
    _assert_pins_sibling(instance, pinning, pinned)


def test_a_subject_keyed_by_a_minted_creation_cannot_be_pinned_by_a_preview_id(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.authoring.inputs import ClaimTypeInput, SubjectKindInput
    from cruxible_client.contracts.authoring.models import (
        SubjectCreationAuthoringPayload,
        SubjectKeyPart,
    )
    from cruxible_client.contracts.claims import SubjectClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_core.authoring.lowering import subject_creation_paths
    from cruxible_core.service.authoring.write_verbs import _UNASSIGNED_OPERATION
    from tests.core_support._identity_support import identity_claim_type
    from tests.core_support._pair_world import (
        accept,
        change_set,
        create,
        write,
    )
    from tests.core_support._support import initialize_local

    host_kind = "proj.host"
    run_kind = "proj.run"
    seeded, _owner = initialize_local(tmp_path)
    accept(
        seeded,
        change_set(
            ClaimTypeInput(
                kind="claim_type",
                claim_type=identity_claim_type(
                    "note",
                    predicate=f"{host_kind}.note",
                    subject_kinds=(host_kind,),
                    canonical_form=None,
                ),
            ),
            ClaimTypeInput(
                kind="claim_type",
                claim_type=identity_claim_type(
                    "host",
                    value_type=(host_kind,),
                    canonical_form=None,
                    predicate=f"{run_kind}.host",
                    subject_kinds=(run_kind,),
                ),
            ),
            SubjectKindInput(kind="subject_kind", name=host_kind, minted=True),
            SubjectKindInput(kind="subject_kind", name=run_kind, keys=(f"{run_kind}.host",)),
        ),
    )

    # Compute the bogus id _UNASSIGNED_OPERATION would assign to a run keyed by a
    # newly minted host: both preview and real writes must refuse it as a pin.
    head = seeded.accepted_coordinate()
    head_tree = seeded.immutable_tree_at(head.git_oid)
    host_member = SubjectCreationAuthoringPayload(
        subject_kind=host_kind,
        key=(),
        rationale="Create host.",
    )
    run_member = SubjectCreationAuthoringPayload(
        subject_kind=run_kind,
        key=(
            SubjectKeyPart(
                predicate=f"{run_kind}.host",
                value=SubjectClaimObject(
                    address=SemanticAddress.subject_creation(
                        host_kind, host_member.member_identity.rpartition("@")[2]
                    )
                ),
            ),
        ),
        rationale="Create run.",
    )
    assert (
        subject_creation_paths(
            seeded,
            operation=_UNASSIGNED_OPERATION,
            base=head,
            base_tree=head_tree,
            members=(host_member, run_member),
            include_operation_dependent=False,
        )
        == {}
    )
    unassigned = subject_creation_paths(
        seeded,
        operation=_UNASSIGNED_OPERATION,
        base=head,
        base_tree=head_tree,
        members=(host_member, run_member),
        include_operation_dependent=True,
    )
    bogus_run_ref = (
        unassigned[run_member.member_identity][0].removeprefix("subjects/").removesuffix(".json")
    )

    changes = (
        create("h", host_kind),
        create("r", run_kind, {"host": {"created": "h"}}),
        _set({"created": "h"}, "note", "Checked", based_on=[f"{bogus_run_ref}#host"]),
    )
    preview = write(seeded, *changes, dry_run=True)
    assert _refusal(preview).code == "cruxible.write.subject_not_found"
    outcome = write(seeded, *changes)
    assert _refusal(outcome).code == "cruxible.write.subject_not_found"

    # Pinning by the creation label works in both preview and write, and preview
    # leaves both operation-dependent Subjects unassigned.
    by_label = (
        create("h", host_kind),
        create("r", run_kind, {"host": {"created": "h"}}),
        _set({"created": "h"}, "note", "Checked", based_on=["r#host"]),
    )
    good_preview = write(seeded, *by_label, dry_run=True)
    assert good_preview.status == "would_accept", good_preview.model_dump(mode="json")
    assert good_preview.changes[0].subject is None
    assert good_preview.changes[1].subject is None

    good = write(seeded, *by_label)
    assert good.status == "accepted", good.model_dump(mode="json")
    run_ref = good.changes[1].subject
    assert run_ref is not None and run_ref != bogus_run_ref
    (key_claim,) = _live_claims(seeded, run_ref, f"{run_kind}.host")
    _assert_pins_sibling(seeded, _claim_of(good, "note"), key_claim)


def test_a_succession_re_authored_claim_pins_a_revised_ordinary_sibling(
    instance: PlaybillInstance,
) -> None:
    import base64

    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        ChangeSetAuthoringPayload,
        ClaimAuthoringPayload,
        ClaimTypeSuccessionDependent,
        ClaimTypeSuccessionMember,
        SelfSourceBody,
        authoring_member_identity,
    )
    from cruxible_client.contracts.claim_types import (
        claim_type_digest,
        claim_type_path,
        parse_claim_type,
    )
    from cruxible_client.contracts.claims import LiteralClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_client.contracts.subjects import subject_path
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from tests.core_support._write_support import OWNER
    from tests.support.claim_types import claim_type_changes

    seeded = _write(instance, _set(WI1, "status", "ready"), _set(WI1, "title", "Original"))
    assert seeded.status == "accepted", seeded
    (status_claim,) = [item.claim for item in seeded.changes if item.field == "status"]
    (title_claim,) = [item.claim for item in seeded.changes if item.field == "title"]
    assert status_claim is not None and title_claim is not None
    status_id = status_claim.removeprefix("Claim:")
    title_id = title_claim.removeprefix("Claim:")

    type_path = claim_type_path(f"{KIND}.status")
    head = instance.accepted_coordinate()
    current = parse_claim_type(instance.tree_at(head.git_oid)[type_path], path=type_path)
    successor = current.model_copy(
        update=claim_type_changes(
            value_type={"enum": ["archived", "blocked", "done", "ready"], "type": "string"},
            lifecycle=ArtifactLifecycle(predecessor_digest=claim_type_digest(current).tagged),
        )
    )

    re_authored_status = ClaimAuthoringPayload(
        statement=AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_path(KIND, "wi-1")),
            predicate=f"{KIND}.status",
            object=LiteralClaimObject(value="archived"),
            role="observation",
        ),
        rationale="Re-authored status based on revised title.",
        source=SelfSourceBody(content_base64=base64.b64encode(b"status: archived\n").decode()),
        revises=status_id,
        based_on=(title_id,),
    )
    revised_title = ClaimAuthoringPayload(
        statement=AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_path(KIND, "wi-1")),
            predicate=f"{KIND}.title",
            object=LiteralClaimObject(value="Updated"),
            role="observation",
        ),
        rationale="Revised title in the same change set.",
        source=SelfSourceBody(content_base64=base64.b64encode(b"title: Updated\n").decode()),
        revises=title_id,
    )
    succession = ClaimTypeSuccessionMember(
        successor=successor,
        dependents=(
            ClaimTypeSuccessionDependent(
                identity=ArtifactIdentity(kind="Claim", name=status_id),
                disposition="re_author",
                successor_claim_id=status_id,
            ),
        ),
    )
    members = (re_authored_status, revised_title, succession)
    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(members, key=lambda item: authoring_member_identity(item).encode("utf-8"))
        )
    )
    submitted = AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=OWNER, payload=payload, canonical_timestamp="2026-09-29T12:30:00.000000Z"
    )
    assert submitted.status.proposal_id is not None, submitted.intent.last_preflight
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt
    _assert_pins_sibling(instance, status_id, title_id)


def test_a_cycle_between_a_re_authored_claim_and_an_ordinary_sibling_refuses(
    instance: PlaybillInstance,
) -> None:
    import base64

    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        ChangeSetAuthoringPayload,
        ClaimAuthoringPayload,
        ClaimTypeSuccessionDependent,
        ClaimTypeSuccessionMember,
        SelfSourceBody,
        authoring_member_identity,
    )
    from cruxible_client.contracts.claim_types import (
        claim_type_digest,
        claim_type_path,
        parse_claim_type,
    )
    from cruxible_client.contracts.claims import LiteralClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_client.contracts.subjects import subject_path
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from tests.core_support._write_support import OWNER
    from tests.support.claim_types import claim_type_changes

    seeded = _write(instance, _set(WI1, "status", "ready"), _set(WI1, "title", "Original"))
    assert seeded.status == "accepted", seeded
    (status_claim,) = [item.claim for item in seeded.changes if item.field == "status"]
    (title_claim,) = [item.claim for item in seeded.changes if item.field == "title"]
    assert status_claim is not None and title_claim is not None
    status_id = status_claim.removeprefix("Claim:")
    title_id = title_claim.removeprefix("Claim:")

    type_path = claim_type_path(f"{KIND}.status")
    head = instance.accepted_coordinate()
    current = parse_claim_type(instance.tree_at(head.git_oid)[type_path], path=type_path)
    successor = current.model_copy(
        update=claim_type_changes(
            value_type={"enum": ["archived", "blocked", "done", "ready"], "type": "string"},
            lifecycle=ArtifactLifecycle(predecessor_digest=claim_type_digest(current).tagged),
        )
    )
    members = (
        ClaimAuthoringPayload(
            statement=AuthoringClaimStatement(
                subject=SemanticAddress.whole_artifact(subject_path(KIND, "wi-1")),
                predicate=f"{KIND}.status",
                object=LiteralClaimObject(value="archived"),
                role="observation",
            ),
            rationale="Re-authored status.",
            source=SelfSourceBody(content_base64=base64.b64encode(b"status: archived\n").decode()),
            revises=status_id,
            based_on=(title_id,),
        ),
        ClaimAuthoringPayload(
            statement=AuthoringClaimStatement(
                subject=SemanticAddress.whole_artifact(subject_path(KIND, "wi-1")),
                predicate=f"{KIND}.title",
                object=LiteralClaimObject(value="Updated"),
                role="observation",
            ),
            rationale="Revised title.",
            source=SelfSourceBody(content_base64=base64.b64encode(b"title: Updated\n").decode()),
            revises=title_id,
            based_on=(status_id,),
        ),
        ClaimTypeSuccessionMember(
            successor=successor,
            dependents=(
                ClaimTypeSuccessionDependent(
                    identity=ArtifactIdentity(kind="Claim", name=status_id),
                    disposition="re_author",
                    successor_claim_id=status_id,
                ),
            ),
        ),
    )
    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(members, key=lambda item: authoring_member_identity(item).encode("utf-8"))
        )
    )
    submitted = AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=OWNER, payload=payload, canonical_timestamp="2026-09-29T12:30:00.000000Z"
    )
    assert submitted.status.proposal_id is None
    assert submitted.intent.last_preflight is not None
    assert any(
        d.code == "cruxible.authoring.based_on_cycle"
        for d in submitted.intent.last_preflight.frontier.diagnostics
    )
