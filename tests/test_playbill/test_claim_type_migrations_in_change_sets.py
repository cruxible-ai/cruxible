"""Evolving a committed vocabulary is one signed generation, not two or three."""

from __future__ import annotations

import base64
import itertools
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ClaimTypeSuccessionDependentV1,
    ClaimTypeSuccessionMemberV1,
    SelfSourceBodyV1,
    authoring_member_identity,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactV3,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, subject_path
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.lowering import AuthoringLoweringError, lower_authoring
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeDependentDispositionV3,
    ClaimTypeMigrationRequestV3,
    ClaimTypeMigrationResultV3,
    service_migrate_claim_type,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_claims import service_explain_playbill_claim
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    service_playbill_next,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _seed_claim_surface
from tests.test_playbill.test_claims import _claim_type

PREDICATE = "sec.vuln.affects_package"
SUBJECT_KIND = "project.work_item"


def _claim_ids() -> Iterator[str]:
    for index in itertools.count(1):
        yield f"CLM-{index:032x}"


def _coordinator(instance: PlaybillInstance) -> AuthoringIntentCoordinator:
    exhaust = instance.root / instance.descriptor.storage.exhaust
    identities = _claim_ids()
    tokens = (f"{index:032x}" for index in itertools.count(1))
    return AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(exhaust, token_factory=lambda: next(tokens)),
        claim_id_factory=lambda: next(identities),
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=UTC),
    )


def _shell(kind: str, subject_id: str) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{kind}/{subject_id}"),
        subject_kind=kind,
        subject_id=subject_id,
    )


def _literal_affects_package(enum: list[str] | None = None) -> ClaimType:
    values = _claim_type().model_dump(mode="python")
    values["identity"] = ArtifactIdentity(kind="ClaimType", name=PREDICATE)
    values["predicate"] = PREDICATE
    values["literal_schema"] = (
        {"type": "string"}
        if enum is None
        else {
            "type": "string",
            "enum": enum,
        }
    )
    return ClaimType.model_validate(values)


def _accepted_type(instance: PlaybillInstance) -> ClaimType:
    path = claim_type_path(PREDICATE)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return parse_claim_type(tree[path], path=path)


def _subject_valued_successor(instance: PlaybillInstance) -> ClaimType:
    current = _accepted_type(instance)
    return current.model_copy(
        update={
            "object_kind": "subject",
            "literal_schema": None,
            "allowed_object_subject_kinds": ("package",),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=claim_type_digest(current).tagged,
            ),
        }
    )


def _enum_successor(instance: PlaybillInstance, *, enum: list[str]) -> ClaimType:
    current = _accepted_type(instance)
    return current.model_copy(
        update={
            "literal_schema": {"type": "string", "enum": enum},
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=claim_type_digest(current).tagged,
            ),
        }
    )


def _claim(
    *,
    subject_id: str,
    value: object,
    claim_ref: str | None = None,
    rationale: str = "The scanner observed this package in the advisory.",
    body: str = "package: demo-package\n",
) -> ClaimAuthoringPayloadV1:
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=SemanticAddress.whole_artifact(subject_path(SUBJECT_KIND, subject_id)),
            predicate=PREDICATE,
            object=value,  # type: ignore[arg-type]
            role="observation",
        ),
        rationale=rationale,
        source=SelfSourceBodyV1(content_base64=base64.b64encode(body.encode("utf-8")).decode()),
        claim_ref=claim_ref,
    )


def _change_set(*members: object) -> ChangeSetAuthoringPayloadV1:
    return ChangeSetAuthoringPayloadV1(
        members=tuple(  # type: ignore[arg-type]
            sorted(
                members,  # type: ignore[type-var]
                key=lambda member: authoring_member_identity(member).encode("utf-8"),  # type: ignore[arg-type]
            )
        )
    )


def _refusals(intent: object) -> list[tuple[str, str, str]]:
    """Say why a preflight refused, so an assertion failure names the law."""

    preflight = getattr(intent, "last_preflight", None)
    if preflight is None:
        return []
    return [
        (item.code, item.offending_element, item.message) for item in preflight.frontier.diagnostics
    ]


def _accept(
    instance: PlaybillInstance,
    owner: object,
    coordinator: AuthoringIntentCoordinator,
    intent_id: str,
    actor: AuthenticatedActor,
) -> None:
    submitted = coordinator.submit(intent_id, actor=actor)
    assert submitted.status.proposal_id is not None, _refusals(submitted.intent)
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        owner,
        submitted.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=submitted.status.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"


def _affects_package_world(
    tmp_path: Path,
    *,
    claim_type: ClaimType | None = None,
    values: tuple[tuple[str, str], ...] = (
        ("wi-42", "demo-package"),
        ("wi-2", "demo-package"),
        ("wi-3", "demo-package"),
    ),
) -> tuple[PlaybillInstance, object, AuthoringIntentCoordinator, dict[str, str]]:
    """Accept a literal-valued vocabulary and the Claims that already speak it."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance,
        owner,
        claim_type_override=claim_type or _literal_affects_package(),
        additional_subjects=(
            _shell(SUBJECT_KIND, "wi-2"),
            _shell(SUBJECT_KIND, "wi-3"),
            _shell(SUBJECT_KIND, "wi-4"),
            _shell("package", "demo-package"),
            _shell("package", "other-package"),
        ),
    )
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _change_set(
        *(
            _claim(subject_id=subject_id, value=LiteralClaimObject(value=value))
            for subject_id, value in values
        )
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)
    minted = {item.member_identity: item.claim_id for item in intent.change_set_claim_identities}
    by_subject = {
        subject_id: minted[
            authoring_member_identity(
                _claim(subject_id=subject_id, value=LiteralClaimObject(value=value))
            )
        ]
        for subject_id, value in values
    }
    return instance, owner, coordinator, by_subject


def _dependent(
    claim_id: str,
    *,
    disposition: str,
    successor_member: int | None = None,
    reason: str | None = None,
) -> ClaimTypeSuccessionDependentV1:
    return ClaimTypeSuccessionDependentV1(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        disposition=disposition,  # type: ignore[arg-type]
        successor_member=successor_member,
        claim_retirement_reason=reason,  # type: ignore[arg-type]
    )


def test_the_affects_package_migration_lands_as_one_generation(tmp_path: Path) -> None:
    """The dogfood migration: tombstones, re-authored edges and a new Claim, once."""

    instance, owner, coordinator, claims = _affects_package_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    successor = _subject_valued_successor(instance)
    generations_before = len(instance.accepted_history())

    edge_a = _claim(
        subject_id="wi-42",
        value=SubjectClaimObject(
            address=SemanticAddress.whole_artifact(subject_path("package", "demo-package"))
        ),
        claim_ref=claims["wi-42"],
        rationale="The advisory names the package, so say which package it is.",
    )
    edge_b = _claim(
        subject_id="wi-2",
        value=SubjectClaimObject(
            address=SemanticAddress.whole_artifact(subject_path("package", "demo-package"))
        ),
        claim_ref=claims["wi-2"],
        rationale="The advisory names the package, so say which package it is.",
    )
    fresh = _claim(
        subject_id="wi-4",
        value=SubjectClaimObject(
            address=SemanticAddress.whole_artifact(subject_path("package", "other-package"))
        ),
        rationale="A fourth work item is affected, stated under the new vocabulary.",
    )

    # The succession sorts among the Claim members by identity, so the sibling
    # indices are only knowable once the whole membership is ordered.
    def _payload(succession: ClaimTypeSuccessionMemberV1) -> ChangeSetAuthoringPayloadV1:
        return _change_set(succession, edge_a, edge_b, fresh)

    provisional = ClaimTypeSuccessionMemberV1(successor=successor, dependents=())
    ordered = _payload(provisional).members
    positions = {authoring_member_identity(member): index for index, member in enumerate(ordered)}
    succession = ClaimTypeSuccessionMemberV1(
        successor=successor,
        dependents=tuple(
            sorted(
                (
                    _dependent(
                        claims["wi-42"],
                        disposition="re_author",
                        successor_member=positions[authoring_member_identity(edge_a)],
                    ),
                    _dependent(
                        claims["wi-2"],
                        disposition="re_author",
                        successor_member=positions[authoring_member_identity(edge_b)],
                    ),
                    _dependent(claims["wi-3"], disposition="retire", reason="was-rescinded"),
                ),
                key=lambda item: item.identity.qualified.encode("utf-8"),
            )
        ),
    )
    intent = coordinator.create(
        actor=actor,
        payload=_payload(succession),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)

    assert len(instance.accepted_history()) == generations_before + 1
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    type_path = claim_type_path(PREDICATE)
    migrated_type = parse_claim_type(tree[type_path], path=type_path)
    assert migrated_type.object_kind == "subject"
    assert migrated_type.lifecycle.predecessor_digest is not None

    for key in ("wi-42", "wi-2"):
        path = claim_path(claims[key])
        edge = parse_claim(tree[path], path=path)
        assert edge.lifecycle.state == "live"
        assert edge.statement.object.kind == "subject"
        assert edge.statement.claim_type_digest == claim_type_digest(migrated_type).tagged
        assert edge.lifecycle.predecessor_digest is not None

    tombstone_path = claim_path(claims["wi-3"])
    tombstone = parse_claim(tree[tombstone_path], path=tombstone_path)
    assert tombstone.lifecycle.state == "retired"
    assert isinstance(tombstone, ClaimArtifactV3)
    assert tombstone.retirement.reason == "was-rescinded"
    # The exact-coordinate exemption: a tombstone keeps the literal it stated
    # under the vocabulary it was accepted under.
    assert tombstone.statement.object.kind == "literal"

    minted = {item.member_identity: item.claim_id for item in intent.change_set_claim_identities}
    fresh_id = minted[authoring_member_identity(fresh)]
    fresh_claim = parse_claim(tree[claim_path(fresh_id)], path=claim_path(fresh_id))
    assert fresh_claim.statement.object.kind == "subject"
    assert fresh_claim.lifecycle.predecessor_digest is None

    edge = parse_claim(tree[claim_path(claims["wi-42"])], path=claim_path(claims["wi-42"]))
    explained = service_explain_playbill_claim(instance, identity=claims["wi-42"])
    assert explained.claim.envelope["predecessor_digest"] == edge.lifecycle.predecessor_digest
    assert edge.lifecycle.predecessor_digest is not None

    outstanding = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=datetime(2026, 8, 23, 12, tzinfo=UTC),
            access_profile=CoverageAccessProfileV1(
                profile_id="claim-type-succession",
                permitted_access_classes=("instance", "public"),
            ),
        ),
    )
    # Nothing the succession left behind: no stale pin, no undispositioned
    # dependent, no unregistered block -- only the ordinary evidence coverage
    # this seeded world already owed before the vocabulary moved.
    assert {item.reason for item in outstanding.items} == {"claim_uncovered"}, (
        outstanding.model_dump(mode="json")
    )


def test_a_carry_of_an_out_of_enum_value_refuses(tmp_path: Path) -> None:
    """A narrowed literal schema is a real migration: carrying is not free."""

    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"), ("wi-2", "blocked")),
    )
    actor = AuthenticatedActor(actor_id="owner")
    successor = _enum_successor(instance, enum=["ready"])
    payload = _change_set(
        ClaimTypeSuccessionMemberV1(
            successor=successor,
            dependents=tuple(
                sorted(
                    (
                        _dependent(claims["wi-42"], disposition="successor"),
                        _dependent(claims["wi-2"], disposition="successor"),
                    ),
                    key=lambda item: item.identity.qualified.encode("utf-8"),
                )
            ),
        )
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    result = coordinator.preflight(intent.intent_id, actor=actor)
    assert result.verdict == "refused"
    codes = {item.code for item in result.frontier.diagnostics}
    assert codes == {"playbill.claim.literal_schema_invalid"}, codes


def test_an_enum_narrowing_carries_what_fits_and_re_authors_what_does_not(
    tmp_path: Path,
) -> None:
    """One generation: the value in the enum carries, the one outside is re-said."""

    instance, owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"), ("wi-2", "blocked"), ("wi-3", "done")),
    )
    actor = AuthenticatedActor(actor_id="owner")
    successor = _enum_successor(instance, enum=["ready"])
    repaired = _claim(
        subject_id="wi-2",
        value=LiteralClaimObject(value="ready"),
        claim_ref=claims["wi-2"],
        rationale="The narrowed vocabulary has one word for this state.",
    )
    provisional = ClaimTypeSuccessionMemberV1(successor=successor, dependents=())
    ordered = _change_set(provisional, repaired).members
    positions = {authoring_member_identity(member): index for index, member in enumerate(ordered)}
    succession = ClaimTypeSuccessionMemberV1(
        successor=successor,
        dependents=tuple(
            sorted(
                (
                    _dependent(claims["wi-42"], disposition="successor"),
                    _dependent(
                        claims["wi-2"],
                        disposition="re_author",
                        successor_member=positions[authoring_member_identity(repaired)],
                    ),
                    _dependent(claims["wi-3"], disposition="retire", reason="was-rescinded"),
                ),
                key=lambda item: item.identity.qualified.encode("utf-8"),
            )
        ),
    )
    intent = coordinator.create(
        actor=actor,
        payload=_change_set(succession, repaired),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    carried = parse_claim(tree[claim_path(claims["wi-42"])], path=claim_path(claims["wi-42"]))
    assert carried.statement.object.value == "ready"  # type: ignore[union-attr]
    re_authored = parse_claim(tree[claim_path(claims["wi-2"])], path=claim_path(claims["wi-2"]))
    assert re_authored.statement.object.value == "ready"  # type: ignore[union-attr]
    assert re_authored.lifecycle.state == "live"
    rescinded = parse_claim(tree[claim_path(claims["wi-3"])], path=claim_path(claims["wi-3"]))
    assert rescinded.lifecycle.state == "retired"


def test_an_incomplete_closure_refuses_with_its_exact_required_dependents(
    tmp_path: Path,
) -> None:
    """No ref, no accepted change: the repair names every digest it still owes."""

    instance, _owner, coordinator, claims = _affects_package_world(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    coordinate_before = instance.accepted_coordinate()
    payload = _change_set(
        ClaimTypeSuccessionMemberV1(
            successor=_enum_successor(instance, enum=["demo-package"]),
            dependents=(_dependent(claims["wi-42"], disposition="successor"),),
        )
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    with pytest.raises(AuthoringLoweringError) as raised:
        lower_authoring(instance, intent=intent, actor_id="owner")
    error = raised.value
    assert error.code == "playbill.authoring.claim_type_succession_closure_incomplete"
    replacement = error.repairs[0].replacement
    assert isinstance(replacement, dict)
    required = replacement["required_dependents"]
    assert isinstance(required, list)
    assert {str(item["identity"]["name"]) for item in required} == set(claims.values())
    assert all(str(item["current_artifact_digest"]).startswith("sha256:") for item in required)
    assert error.repairs[0].kind == "replace_dependents"
    assert instance.accepted_coordinate() == coordinate_before


def test_object_kind_change_refuses_a_carried_live_claim(tmp_path: Path) -> None:
    """The 2026-09-02 object-kind law, said at the member that broke it."""

    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "demo-package"),),
    )
    actor = AuthenticatedActor(actor_id="owner")
    payload = _change_set(
        ClaimTypeSuccessionMemberV1(
            successor=_subject_valued_successor(instance),
            dependents=(_dependent(claims["wi-42"], disposition="successor"),),
        )
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    with pytest.raises(AuthoringLoweringError) as raised:
        lower_authoring(instance, intent=intent, actor_id="owner")
    error = raised.value
    assert error.code == "playbill.authoring.claim_type_succession_object_kind_change"
    assert error.offending_element == "members[0].dependents[0].disposition"
    replacement = error.repairs[0].replacement
    assert isinstance(replacement, dict)
    assert replacement["permitted_dispositions"] == ["retire", "re_author"]


@pytest.mark.parametrize(
    ("successor_member", "reason"),
    [(9, "member_not_found"), (0, "not_a_claim_member")],
)
def test_a_re_author_that_names_no_claim_member_refuses_with_both_indices(
    tmp_path: Path,
    successor_member: int,
    reason: str,
) -> None:
    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "demo-package"),),
    )
    actor = AuthenticatedActor(actor_id="owner")
    payload = _change_set(
        ClaimTypeSuccessionMemberV1(
            successor=_subject_valued_successor(instance),
            dependents=(
                _dependent(
                    claims["wi-42"],
                    disposition="re_author",
                    successor_member=successor_member,
                ),
            ),
        )
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    with pytest.raises(AuthoringLoweringError) as raised:
        lower_authoring(instance, intent=intent, actor_id="owner")
    error = raised.value
    assert error.code == "playbill.authoring.claim_type_succession_re_author_invalid"
    assert error.offending_element == "members[0].dependents[0].successor_member"
    replacement = error.repairs[0].replacement
    assert isinstance(replacement, dict)
    assert replacement["reason"] == reason
    assert replacement["member"] == 0


def test_a_re_author_sibling_under_another_type_refuses_with_both_indices(
    tmp_path: Path,
) -> None:
    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "demo-package"),),
    )
    actor = AuthenticatedActor(actor_id="owner")
    other = ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=SemanticAddress.whole_artifact(subject_path(SUBJECT_KIND, "wi-2")),
            predicate="project.work_item.status",
            object=LiteralClaimObject(value="ready"),
            role="observation",
        ),
        rationale="A Claim of another vocabulary entirely.",
        source=SelfSourceBodyV1(
            content_base64=base64.b64encode(b"status: ready\n").decode(),
        ),
        claim_ref=claims["wi-42"],
    )
    provisional = ClaimTypeSuccessionMemberV1(
        successor=_subject_valued_successor(instance),
        dependents=(),
    )
    ordered = _change_set(provisional, other).members
    positions = {authoring_member_identity(member): index for index, member in enumerate(ordered)}
    succession = ClaimTypeSuccessionMemberV1(
        successor=_subject_valued_successor(instance),
        dependents=(
            _dependent(
                claims["wi-42"],
                disposition="re_author",
                successor_member=positions[authoring_member_identity(other)],
            ),
        ),
    )
    intent = coordinator.create(
        actor=actor,
        payload=_change_set(succession, other),
        canonical_timestamp=TIMESTAMP,
    ).intent
    with pytest.raises(AuthoringLoweringError) as raised:
        lower_authoring(instance, intent=intent, actor_id="owner")
    error = raised.value
    assert error.code == "playbill.authoring.claim_type_succession_re_author_invalid"
    replacement = error.repairs[0].replacement
    assert isinstance(replacement, dict)
    assert replacement["reason"] == "predicate_mismatch"
    assert replacement["successor_member"] == positions[authoring_member_identity(other)]


def test_two_successions_of_one_type_cannot_share_a_change_set(tmp_path: Path) -> None:
    instance, _owner, _coordinator, _claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "demo-package"),),
    )
    successor = _subject_valued_successor(instance)
    with pytest.raises(ValueError, match="member identities must be unique"):
        ChangeSetAuthoringPayloadV1(
            members=(  # type: ignore[arg-type]
                ClaimTypeSuccessionMemberV1(successor=successor, dependents=()),
                ClaimTypeSuccessionMemberV1(successor=successor, dependents=()),
            )
        )


def test_both_roads_build_the_same_succession_candidate(tmp_path: Path) -> None:
    """One law, one implementation: the standalone route and the member agree."""

    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"), ("wi-2", "ready")),
    )
    actor = AuthenticatedActor(actor_id="owner")
    successor = _enum_successor(instance, enum=["ready"])
    dependents = tuple(
        sorted(
            (claims["wi-42"], claims["wi-2"]),
            key=lambda item: item.encode("utf-8"),
        )
    )
    payload = _change_set(
        ClaimTypeSuccessionMemberV1(
            successor=successor,
            dependents=tuple(
                _dependent(claim_id, disposition="successor") for claim_id in dependents
            ),
        )
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    lowered = lower_authoring(instance, intent=intent, actor_id="owner")

    standalone = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV3(
            mode="submit",
            successor=successor,
            dependents=tuple(
                ClaimTypeDependentDispositionV3(
                    identity=ArtifactIdentity(kind="Claim", name=claim_id),
                    disposition="successor",
                )
                for claim_id in dependents
            ),
        ),
        actor=actor,
    )
    assert isinstance(standalone, ClaimTypeMigrationResultV3)
    tree_oid = standalone.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    standalone_tree = instance.proposal_tree(tree_oid)
    for changed_path, content in lowered.changed_members:
        assert standalone_tree[changed_path] == content, changed_path
