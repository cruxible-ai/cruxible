"""SubjectKind: the authored definition of how Subjects of one kind are identified (D6).

A SubjectKind names its key ClaimTypes (single or composite, ``unique`` or
``hint``) or declares minted ids. It is authored, lowered, judged and read like
every other definition kind: a change-set member, its own law (key ClaimTypes
live, v8, literal, one-valued, about the kind only, accepted in the same change or
earlier), ``get SubjectKind:<kind>``, the kind's identity in ``orient`` and in the
query vocabulary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.authoring.examples import AUTHORING_EXAMPLE_FACTORIES
from cruxible_client.contracts.authoring.inputs import (
    ChangeSetInput,
    ClaimTypeInput,
    SubjectKindInput,
    lower_authoring_input,
)
from cruxible_client.contracts.authoring.models import AuthoringSubmitResult
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.subject_kinds import (
    AcceptedSubjectKind,
    KeyClaimTypeShape,
    SubjectKind,
    SubjectKindKeys,
    SubjectKindMinted,
    evaluate_subject_kind_law,
    parse_subject_kind,
    render_subject_kind,
    subject_kind_digest,
    subject_kind_path,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.discovery.query_vocabulary import load_query_vocabulary
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._identity_support import OWNER, identity_claim_type
from tests.core_support._support import initialize_local

KIND = "dev.decision"
SLUG = f"{KIND}.slug"
_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


def slug_claim_type(**changes: Any) -> Any:
    fields: dict[str, Any] = {
        "canonical_form": ("nfkc", "trim", "collapse_whitespace", "casefold"),
        "literal_schema": {"type": "string"},
        "predicate": SLUG,
        "subject_kinds": (KIND,),
    }
    fields.update(changes)
    return identity_claim_type("slug", **fields)


def submit(instance: PlaybillInstance, payload: Any) -> AuthoringSubmitResult:
    return AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=OWNER, payload=payload, canonical_timestamp="2026-10-09T12:00:00.000000Z"
    )


def accept(instance: PlaybillInstance, payload: Any) -> AuthoringSubmitResult:
    submitted = submit(instance, payload)
    assert submitted.status.proposal_id is not None, submitted
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt
    return submitted


def compile_(instance: PlaybillInstance, payload: Any) -> Any:
    return AuthoringIntentCoordinator.for_instance(instance).compile(
        actor=OWNER, payload=payload, canonical_timestamp="2026-10-09T12:00:00.000000Z"
    )


def declare(**fields: Any) -> SubjectKindInput:
    return SubjectKindInput(kind="subject_kind", name=KIND, **fields)


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    instance, _owner = initialize_local(tmp_path)
    return instance


def test_a_keyed_subject_kind_and_its_key_claim_type_land_in_one_change(
    instance: PlaybillInstance,
) -> None:
    accept(
        instance,
        lower_authoring_input(
            ChangeSetInput(
                kind="change_set",
                members=(
                    ClaimTypeInput(kind="claim_type", claim_type=slug_claim_type()),
                    declare(keys=(SLUG,), uniqueness="unique", description="A recorded decision."),
                ),
            )
        ),
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    declared = parse_subject_kind(tree[subject_kind_path(KIND)], path=subject_kind_path(KIND))
    assert declared.identity_rule == SubjectKindKeys(keys=(SLUG,), uniqueness="unique")
    assert declared.description == "A recorded decision."

    card = service_playbill_get(
        instance,
        request=GetRequest(
            ref=f"SubjectKind:{KIND}", evaluation_time=datetime(2026, 10, 9, tzinfo=UTC)
        ),
        access=_ACCESS,
    )
    assert card.kind == "subject_kind"
    assert card.card is not None
    dumped = card.card.model_dump(mode="json")
    assert (dumped["rule"], dumped["keys"], dumped["uniqueness"], dumped["live_subjects"]) == (
        "keys",
        [SLUG],
        "unique",
        0,
    )
    for detail in ("history", "proof"):
        read = service_playbill_get(
            instance, request=GetRequest(ref=f"SubjectKind:{KIND}", detail=detail), access=_ACCESS
        )
        assert (read.kind, read.detail) == ("subject_kind", detail)

    oriented = service_playbill_orient(instance)
    row = next(item for item in oriented.kinds if item.kind == KIND)
    assert row.identity == f"keys: {SLUG} (unique)"
    vocabulary = load_query_vocabulary(instance, instance.accepted_coordinate())
    assert KIND in vocabulary.kinds


def test_the_example_declares_a_single_key_unique_kind(instance: PlaybillInstance) -> None:
    payload = lower_authoring_input(AUTHORING_EXAMPLE_FACTORIES["subject-kind"]())
    accept(instance, payload)
    oriented = service_playbill_orient(instance)
    row = next(item for item in oriented.kinds if item.kind == "project.work_item")
    assert row.identity == "keys: project.work_item.slug (unique)"


def test_a_minted_kind_needs_no_key_and_an_undeclared_kind_says_so(
    instance: PlaybillInstance,
) -> None:
    accept(
        instance,
        lower_authoring_input(
            ChangeSetInput(
                kind="change_set",
                members=(
                    declare(minted=True),
                    ClaimTypeInput(
                        kind="claim_type",
                        claim_type=identity_claim_type(
                            "note",
                            canonical_form=(),
                            literal_schema={"type": "string"},
                            predicate="dev.other.note",
                            subject_kinds=("dev.other",),
                        ),
                    ),
                ),
            )
        ),
    )
    oriented = service_playbill_orient(instance)
    rows = {item.kind: item.identity for item in oriented.kinds}
    assert rows[KIND] == "minted"
    assert rows["dev.other"] == "undeclared"


@pytest.mark.parametrize(
    ("claim_type", "message"),
    [
        (None, "not a live accepted ClaimType"),
        (slug_claim_type(canonical_form=None), "must be a ClaimType v8"),
        (slug_claim_type(cardinality="many"), "must be one-valued"),
        (slug_claim_type(subject_kinds=(KIND, "dev.other")), f"must be about {KIND} only"),
    ],
)
def test_the_law_refuses_a_key_claim_type_that_cannot_name_a_subject(
    instance: PlaybillInstance, claim_type: Any, message: str
) -> None:
    members: list[Any] = [declare(keys=(SLUG,), uniqueness="unique")]
    if claim_type is not None:
        members.append(ClaimTypeInput(kind="claim_type", claim_type=claim_type))
    result = compile_(
        instance, lower_authoring_input(ChangeSetInput(kind="change_set", members=tuple(members)))
    )
    assert result.verdict == "refused", result
    found = [
        item
        for item in result.frontier.diagnostics
        if item.code == "cruxible.subject_kind.key_claim_type_invalid"
    ]
    assert found and message in found[0].message, result.frontier.diagnostics


def test_a_subject_kind_input_states_keys_or_minted_exactly_once() -> None:
    from cruxible_client.contracts.authoring.inputs import AuthoringInputError

    for fields in (
        {},
        {"keys": (SLUG,), "uniqueness": "unique", "minted": True},
        {"keys": (SLUG,)},
    ):
        with pytest.raises(AuthoringInputError) as refused:
            lower_authoring_input(declare(**fields))
        assert refused.value.code == "cruxible.authoring.subject_kind_invalid"


def _accepted(declared: SubjectKind) -> AcceptedSubjectKind:
    return AcceptedSubjectKind(
        path=subject_kind_path(declared.identity.name),
        subject_kind=declared,
        artifact_digest=subject_kind_digest(declared).tagged,
    )


def _shape(**changes: Any) -> KeyClaimTypeShape:
    values: dict[str, Any] = {
        "predicate": SLUG,
        "live": True,
        "artifact_format": "playbill-claim-type-v8",
        "object_kind": "literal",
        "cardinality": "one",
        "allowed_subject_kinds": (KIND,),
    }
    values.update(changes)
    return KeyClaimTypeShape(**values)


def test_the_law_holds_succession_never_revives_and_reserves_the_pseudo_kinds() -> None:
    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle

    keyed = SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name=KIND),
        identity_rule=SubjectKindKeys(keys=(SLUG,), uniqueness="unique"),
    )
    path = subject_kind_path(KIND)
    assert parse_subject_kind(render_subject_kind(keyed), path=path) == keyed
    assert (
        evaluate_subject_kind_law(
            keyed, path=path, predecessor=None, key_claim_types={SLUG: _shape()}
        ).verdict
        == "accepted"
    )
    # Changing the keys is a governed successor naming its exact predecessor.
    rekeyed = keyed.model_copy(
        update={
            "identity_rule": SubjectKindKeys(keys=(SLUG,), uniqueness="hint"),
            "lifecycle": ArtifactLifecycle(predecessor_digest=subject_kind_digest(keyed).tagged),
        }
    )
    assert (
        evaluate_subject_kind_law(
            rekeyed, path=path, predecessor=_accepted(keyed), key_claim_types={SLUG: _shape()}
        ).verdict
        == "accepted"
    )
    stale = rekeyed.model_copy(update={"lifecycle": ArtifactLifecycle(predecessor_digest=None)})
    refused = evaluate_subject_kind_law(
        stale, path=path, predecessor=_accepted(keyed), key_claim_types={SLUG: _shape()}
    )
    assert refused.diagnostics[0].code == "cruxible.subject_kind.predecessor_mismatch"
    retired = keyed.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_kind_digest(keyed).tagged
            )
        }
    )
    revived = keyed.model_copy(
        update={
            "identity_rule": SubjectKindMinted(),
            "lifecycle": ArtifactLifecycle(predecessor_digest=subject_kind_digest(retired).tagged),
        }
    )
    refused = evaluate_subject_kind_law(
        revived, path=path, predecessor=_accepted(retired), key_claim_types={}
    )
    assert refused.diagnostics[0].code == "cruxible.subject_kind.revival_refused"
    pseudo = SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name="semantic.subject"),
        identity_rule=SubjectKindMinted(),
    )
    refused = evaluate_subject_kind_law(
        pseudo, path=subject_kind_path("semantic.subject"), predecessor=None, key_claim_types={}
    )
    assert refused.diagnostics[0].code == "cruxible.subject_kind.reserved"
