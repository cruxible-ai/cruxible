"""Evolving a committed vocabulary is one signed generation, not two or three."""

from __future__ import annotations

import base64
import itertools
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ClaimRetirementMemberV1,
    ClaimTypeAuthoringPayloadV1,
    ClaimTypeSuccessionDependentV1,
    ClaimTypeSuccessionDisposition,
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
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicyV1
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.proposal_models import ProposalReceiveLimits
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.playbill.authoring import preflight as preflight_module
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.lowering import AuthoringLoweringError, lower_authoring
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeDependentDispositionV3,
    ClaimTypeMigrationRequestV3,
    ClaimTypeMigrationResultV3,
    MigrationInputDisposition,
    service_migrate_claim_type,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.runtime.playbill_api import _authoring_preflight_result
from cruxible_core.service.playbill_claims import service_explain_playbill_claim
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    service_playbill_next,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_change_set_intents import accepted_change_set_record
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _seed_claim_surface
from tests.test_playbill.test_claims import _claim_type
from tests.test_playbill.test_resolution_contracts import _accept_tree

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
    successor_claim_id: str | None = None,
    reason: str | None = None,
) -> ClaimTypeSuccessionDependentV1:
    return ClaimTypeSuccessionDependentV1(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        disposition=disposition,  # type: ignore[arg-type]
        successor_claim_id=successor_claim_id,
        claim_retirement_reason=reason,  # type: ignore[arg-type]
    )


def _re_author(claim_id: str) -> ClaimTypeSuccessionDependentV1:
    """Re-author one dependent as the sibling Claim member that revises it."""

    return _dependent(claim_id, disposition="re_author", successor_claim_id=claim_id)


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

    # A re-authoring sibling revises the dependent it re-authors, so the whole
    # set is expressible before anything is compiled: no member index is named.
    succession = ClaimTypeSuccessionMemberV1(
        successor=successor,
        dependents=tuple(
            sorted(
                (
                    _re_author(claims["wi-42"]),
                    _re_author(claims["wi-2"]),
                    _dependent(claims["wi-3"], disposition="retire", reason="was-rescinded"),
                ),
                key=lambda item: item.identity.qualified.encode("utf-8"),
            )
        ),
    )
    intent = coordinator.create(
        actor=actor,
        payload=_change_set(succession, edge_a, edge_b, fresh),
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
    # dependent, no unregistered block. Nor any coverage debt: this world's
    # ClaimType names the coordinator self-source contract in its
    # evidence-admission policy, so its authored bodies are admitted by rule.
    assert {item.reason for item in outstanding.items} == set(), outstanding.model_dump(mode="json")


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
    # The repair must be applicable as written: a dependent is named by identity
    # alone, so the repair may not ask for a digest the member model has no
    # field to carry. The digest each required row reports is a read.
    assert not any("digest" in name for name in ClaimTypeSuccessionDependentV1.model_fields)
    assert "identity" in error.repairs[0].description
    assert "digest" not in error.repairs[0].description
    assert [
        ClaimTypeSuccessionDependentV1(
            identity=ArtifactIdentity.model_validate(item["identity"]),
            disposition="successor",
        )
        for item in required
    ]
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


def test_a_re_author_that_names_no_sibling_refuses_naming_the_claim_it_needs(
    tmp_path: Path,
) -> None:
    """The repair names the key the payload carries, at the only value it may hold."""

    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "demo-package"),),
    )
    actor = AuthenticatedActor(actor_id="owner")
    unknown = "CLM-" + "9" * 32
    payload = _change_set(
        ClaimTypeSuccessionMemberV1(
            successor=_subject_valued_successor(instance),
            dependents=(
                _dependent(
                    claims["wi-42"],
                    disposition="re_author",
                    successor_claim_id=unknown,
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
    assert error.offending_element == "members[0].dependents[0].successor_claim_id"
    replacement = error.repairs[0].replacement
    assert isinstance(replacement, dict)
    assert replacement["reason"] == "member_not_found"
    assert replacement["member"] == 0
    assert replacement["named_claim_id"] == unknown
    # The repaired value is a Claim ID, under the key the member really carries.
    assert replacement["successor_claim_id"] == claims["wi-42"]
    assert claim_path(str(replacement["successor_claim_id"]))


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
    succession = ClaimTypeSuccessionMemberV1(
        successor=_subject_valued_successor(instance),
        dependents=(_re_author(claims["wi-42"]),),
    )
    payload = _change_set(succession, other)
    positions = {
        authoring_member_identity(member): index for index, member in enumerate(payload.members)
    }
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    with pytest.raises(AuthoringLoweringError) as raised:
        lower_authoring(instance, intent=intent, actor_id="owner")
    error = raised.value
    assert error.code == "playbill.authoring.claim_type_succession_re_author_invalid"
    replacement = error.repairs[0].replacement
    assert isinstance(replacement, dict)
    assert replacement["reason"] == "predicate_mismatch"
    assert replacement["member"] == positions[authoring_member_identity(succession)]
    assert replacement["sibling_member"] == positions[authoring_member_identity(other)]


def test_a_sibling_claim_of_the_succeeded_type_is_not_a_dependent(tmp_path: Path) -> None:
    """Members lower in dependency order, so a same-set Claim speaks the successor."""

    instance, owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"),),
    )
    actor = AuthenticatedActor(actor_id="owner")
    successor = _enum_successor(instance, enum=["ready"])
    generations_before = len(instance.accepted_history())
    fresh = _claim(
        subject_id="wi-2",
        value=LiteralClaimObject(value="ready"),
        rationale="A second work item, stated in the same set that narrows the type.",
    )
    # The closure names the ACCEPTED Claim only; the sibling is not owed a
    # disposition, and a set that dispositioned it would not know its ID anyway.
    succession = ClaimTypeSuccessionMemberV1(
        successor=successor,
        dependents=(_dependent(claims["wi-42"], disposition="successor"),),
    )
    intent = coordinator.create(
        actor=actor,
        payload=_change_set(succession, fresh),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)

    assert len(instance.accepted_history()) == generations_before + 1
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    type_path = claim_type_path(PREDICATE)
    migrated = parse_claim_type(tree[type_path], path=type_path)
    minted = {item.member_identity: item.claim_id for item in intent.change_set_claim_identities}
    fresh_path = claim_path(minted[authoring_member_identity(fresh)])
    sibling = parse_claim(tree[fresh_path], path=fresh_path)
    assert sibling.statement.claim_type_digest == claim_type_digest(migrated).tagged
    assert sibling.lifecycle.predecessor_digest is None


def test_a_set_cannot_define_a_claim_type_and_succeed_it(tmp_path: Path) -> None:
    """One member per authored path: define-then-succeed is not a shape."""

    instance, _owner, coordinator, _claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"),),
    )
    actor = AuthenticatedActor(actor_id="owner")
    defined = _accepted_type(instance).model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="project.work_item.owner"),
            "predicate": "project.work_item.owner",
            "lifecycle": ArtifactLifecycle(),
        }
    )
    succeeded = defined.model_copy(
        update={
            "literal_schema": {"type": "string", "enum": ["ready"]},
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(defined).tagged),
        }
    )
    payload = _change_set(
        ClaimTypeAuthoringPayloadV1(claim_type=defined),
        ClaimTypeSuccessionMemberV1(successor=succeeded, dependents=()),
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    with pytest.raises(AuthoringLoweringError) as raised:
        lower_authoring(instance, intent=intent, actor_id="owner")
    error = raised.value
    assert error.code == "playbill.authoring.change_set_member_path_collision"
    replacement = error.repairs[0].replacement
    assert isinstance(replacement, dict)
    assert replacement["path"] == claim_type_path("project.work_item.owner")
    assert replacement["members"] == [0, 1]


def test_the_member_disposition_cannot_drift_from_the_migration_disposition() -> None:
    """Two distributions, one vocabulary: the member mirrors V3 field for field."""

    core = ClaimTypeDependentDispositionV3.model_fields
    member = ClaimTypeSuccessionDependentV1.model_fields
    # `successor` is the standalone route's hand-supplied artifact body;
    # `successor_claim_id` is the change set's sibling member. Everything the
    # two roads share is spelled identically.
    assert set(member) - set(core) == {"successor_claim_id"}
    assert set(core) - set(member) == {"successor"}
    for name in ("identity", "claim_retirement_reason", "claim_effective_until"):
        assert member[name].annotation == core[name].annotation, name

    assert set(get_args(ClaimTypeSuccessionDisposition)) == set(
        get_args(MigrationInputDisposition)
    ) | {"re_author"}

    identity = ArtifactIdentity(kind="Claim", name="CLM-" + "3" * 32)
    naive = datetime(2026, 9, 5, 12)  # noqa: DTZ001 - the point of the test
    for model in (ClaimTypeDependentDispositionV3, ClaimTypeSuccessionDependentV1):
        with pytest.raises(ValidationError, match="timezone-aware"):
            model(
                identity=identity,
                disposition="retire",
                claim_retirement_reason="was-wrong",
                claim_effective_until=naive,
            )
    aware = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert (
        ClaimTypeSuccessionDependentV1(
            identity=identity,
            disposition="retire",
            claim_retirement_reason="was-wrong",
            claim_effective_until=aware,
        ).claim_effective_until
        == ClaimTypeDependentDispositionV3(
            identity=identity,
            disposition="retire",
            claim_retirement_reason="was-wrong",
            claim_effective_until=aware,
        ).claim_effective_until
    )


def test_a_successor_admitting_no_accepted_contract_lints_on_both_roads(
    tmp_path: Path,
) -> None:
    """The operator road said it and the agent road said nothing; now both do."""

    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"),),
    )
    actor = AuthenticatedActor(actor_id="owner")
    successor = _enum_successor(instance, enum=["ready"]).model_copy(
        update={"evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1()}
    )

    operator = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV3(
            mode="preflight",
            successor=successor,
            dependents=(
                ClaimTypeDependentDispositionV3(
                    identity=ArtifactIdentity(kind="Claim", name=claims["wi-42"]),
                    disposition="successor",
                ),
            ),
        ),
        actor=actor,
    )
    assert operator.lint is not None
    expected = [warning.model_dump(mode="json") for warning in operator.lint.warnings]
    assert {str(warning["code"]) for warning in expected} == {
        "playbill.claim_type.evidence_policy_admits_no_accepted_contract"
    }

    intent = coordinator.create(
        actor=actor,
        payload=_change_set(
            ClaimTypeSuccessionMemberV1(
                successor=successor,
                dependents=(_dependent(claims["wi-42"], disposition="successor"),),
            )
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    served = _authoring_preflight_result(
        coordinator,
        actor=actor,
        result=coordinator.preflight(intent.intent_id, actor=actor),
    )
    assert served.lint is not None
    assert served.lint.warnings == expected


def test_the_deprecated_invalidation_word_refuses_typed(tmp_path: Path) -> None:
    """The standalone route's fourth word parses here and names the road that takes it."""

    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"),),
    )
    actor = AuthenticatedActor(actor_id="owner")
    payload = _change_set(
        ClaimTypeSuccessionMemberV1(
            successor=_enum_successor(instance, enum=["ready"]),
            dependents=(
                _dependent(
                    claims["wi-42"],
                    disposition="invalidation",
                    reason="was-rescinded",
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
    assert error.code == "playbill.authoring.claim_type_succession_disposition_deprecated"
    assert error.offending_element == "members[0].dependents[0].disposition"
    repair = error.repairs[0]
    assert repair.kind == "replace_disposition"
    assert "playbill claim-type migrate" in repair.description
    replacement = repair.replacement
    assert isinstance(replacement, dict)
    assert replacement["operator_route"] == "playbill claim-type migrate"
    assert replacement["permitted_dispositions"] == ["re_author", "retire", "successor"]
    # The standalone route still takes the word, with its deprecation warning.
    standalone = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV3(
            mode="preflight",
            successor=_enum_successor(instance, enum=["ready"]),
            dependents=(
                ClaimTypeDependentDispositionV3(
                    identity=ArtifactIdentity(kind="Claim", name=claims["wi-42"]),
                    disposition="invalidation",
                    claim_retirement_reason="was-rescinded",
                ),
            ),
        ),
        actor=actor,
    )
    assert {warning.code for warning in standalone.warnings} == {
        "playbill.claim_type.invalidation_deprecated"
    }


def test_a_dependent_this_set_also_retires_refuses_naming_both_members(
    tmp_path: Path,
) -> None:
    """One artifact, one disposition: the succession already settles its closure."""

    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"), ("wi-2", "ready")),
    )
    actor = AuthenticatedActor(actor_id="owner")
    succession = ClaimTypeSuccessionMemberV1(
        successor=_enum_successor(instance, enum=["ready"]),
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
    retirement = ClaimRetirementMemberV1(claim_ref=claims["wi-2"], reason="was-wrong")
    payload = _change_set(succession, retirement)
    positions = {
        authoring_member_identity(member): index for index, member in enumerate(payload.members)
    }
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    with pytest.raises(AuthoringLoweringError) as raised:
        lower_authoring(instance, intent=intent, actor_id="owner")
    error = raised.value
    assert error.code == "playbill.authoring.change_set_member_path_collision"
    succession_index = positions[authoring_member_identity(succession)]
    assert error.offending_element == f"members[{succession_index}].dependents"
    replacement = error.repairs[0].replacement
    assert isinstance(replacement, dict)
    assert replacement["members"] == sorted(
        (succession_index, positions[authoring_member_identity(retirement)])
    )
    assert replacement["path"] == claim_path(claims["wi-2"])
    assert error.repairs[0].kind == "drop_or_merge_member"


def test_a_re_author_sibling_that_moves_the_subject_refuses(tmp_path: Path) -> None:
    """A re-authoring says the same thing again, not something else."""

    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"), ("wi-2", "ready")),
    )
    actor = AuthenticatedActor(actor_id="owner")
    moved = _claim(
        subject_id="wi-3",
        value=LiteralClaimObject(value="ready"),
        claim_ref=claims["wi-42"],
        rationale="A revision that quietly changes which work item this is about.",
    )
    succession = ClaimTypeSuccessionMemberV1(
        successor=_enum_successor(instance, enum=["ready"]),
        dependents=tuple(
            sorted(
                (
                    _re_author(claims["wi-42"]),
                    _dependent(claims["wi-2"], disposition="successor"),
                ),
                key=lambda item: item.identity.qualified.encode("utf-8"),
            )
        ),
    )
    payload = _change_set(succession, moved)
    positions = {
        authoring_member_identity(member): index for index, member in enumerate(payload.members)
    }
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    with pytest.raises(AuthoringLoweringError) as raised:
        lower_authoring(instance, intent=intent, actor_id="owner")
    error = raised.value
    assert error.code == "playbill.authoring.claim_type_succession_re_author_invalid"
    sibling = positions[authoring_member_identity(moved)]
    assert error.offending_element == f"members[{sibling}].statement.subject"
    replacement = error.repairs[0].replacement
    assert isinstance(replacement, dict)
    assert replacement["reason"] == "subject_mismatch"
    assert replacement["member"] == positions[authoring_member_identity(succession)]
    assert replacement["sibling_member"] == sibling
    assert replacement["expected_subject"] != replacement["subject"]
    assert error.repairs[0].kind == "replace_subject"


def test_a_machine_applying_the_re_author_repair_lands_the_set(tmp_path: Path) -> None:
    """A repair is only a repair if writing it back produces a payload that lowers."""

    instance, _owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "demo-package"), ("wi-2", "demo-package")),
    )
    actor = AuthenticatedActor(actor_id="owner")
    successor = _subject_valued_successor(instance)
    package = SemanticAddress.whole_artifact(subject_path("package", "demo-package"))
    edges = [
        _claim(
            subject_id=subject_id,
            value=SubjectClaimObject(address=package),
            claim_ref=claims[subject_id],
            rationale="The advisory names the package, so say which package it is.",
        )
        for subject_id in ("wi-42", "wi-2")
    ]
    # Both pointers name the sibling that re-authors the OTHER dependent.
    pointers = {claims["wi-42"]: claims["wi-2"], claims["wi-2"]: claims["wi-42"]}
    element_re = re.compile(r"^members\[(\d+)\]\.dependents\[(\d+)\]\.successor_claim_id$")

    for _attempt in range(4):
        payload = _change_set(
            ClaimTypeSuccessionMemberV1(
                successor=successor,
                dependents=tuple(
                    sorted(
                        (
                            _dependent(
                                claim_id,
                                disposition="re_author",
                                successor_claim_id=named,
                            )
                            for claim_id, named in pointers.items()
                        ),
                        key=lambda item: item.identity.qualified.encode("utf-8"),
                    )
                ),
            ),
            *edges,
        )
        intent = coordinator.create(
            actor=actor,
            payload=payload,
            canonical_timestamp=TIMESTAMP,
        ).intent
        try:
            lowered = lower_authoring(instance, intent=intent, actor_id="owner")
        except AuthoringLoweringError as error:
            assert error.code == "playbill.authoring.claim_type_succession_re_author_invalid"
            replacement = error.repairs[0].replacement
            assert isinstance(replacement, dict)
            assert replacement["reason"] == "identity_mismatch"
            matched = element_re.match(error.offending_element)
            assert matched is not None, error.offending_element
            member = payload.members[int(matched.group(1))]
            assert isinstance(member, ClaimTypeSuccessionMemberV1)
            offending = member.dependents[int(matched.group(2))]
            # Exactly what a machine does: write the repair back under the key
            # the refusal named, at the value it carried.
            pointers[offending.identity.name] = str(replacement["successor_claim_id"])
            continue
        break
    else:  # pragma: no cover - the loop below asserts the repair converges
        raise AssertionError("the re_author repair did not converge")

    assert pointers == {claims["wi-42"]: claims["wi-42"], claims["wi-2"]: claims["wi-2"]}
    changed = dict(lowered.changed_members)
    for claim_id in claims.values():
        assert claim_path(claim_id) in changed


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


def test_a_succession_rebases_and_replays_byte_identically(tmp_path: Path) -> None:
    """One vocabulary change advances over an unrelated acceptance, then replays."""

    instance, owner, coordinator, claims = _affects_package_world(
        tmp_path,
        values=(("wi-42", "ready"),),
    )
    actor = AuthenticatedActor(actor_id="owner")
    successor = _enum_successor(instance, enum=["ready"])
    payload = _change_set(
        ClaimTypeSuccessionMemberV1(
            successor=successor,
            dependents=(_dependent(claims["wi-42"], disposition="successor"),),
        ),
        # This Claim member speaks the successor vocabulary about a Subject the
        # instance has not accepted yet, so the whole set refuses until it has.
        _claim(subject_id="wi-late", value=LiteralClaimObject(value="ready")),
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    assert coordinator.preflight(intent.intent_id, actor=actor).verdict == "refused"

    # The missing Subject lands underneath the intent's base.
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[subject_path(SUBJECT_KIND, "wi-late")] = render_subject(_shell(SUBJECT_KIND, "wi-late"))
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-08-21T12:01:00.000000Z",
        proposal_name="advance-for-succession-rebase",
    )

    rebased = coordinator.rebase(intent.intent_id, actor=actor).intent
    assert rebased.base_coordinate == AcceptedCoordinate.from_internal(
        instance.accepted_coordinate()
    )
    _accept(instance, owner, coordinator, intent.intent_id, actor)

    accepted = instance.accepted_coordinate()
    before = instance.tree_at(accepted.git_oid)
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    replayed = reopened.accepted_coordinate()
    assert replayed == accepted
    assert reopened.tree_at(replayed.git_oid) == before


def _retiring_succession_world(
    tmp_path: Path,
    *,
    dependents: int,
) -> tuple[PlaybillInstance, object, AuthoringIntentCoordinator, ClaimTypeSuccessionMemberV1]:
    """Accept `dependents` Claims of one predicate, and author the succession that retires them."""

    subjects = tuple(f"wi-fan-{index:03d}" for index in range(dependents))
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance,
        owner,
        claim_type_override=_literal_affects_package(),
        additional_subjects=tuple(_shell(SUBJECT_KIND, item) for item in subjects)
        + (_shell("package", "demo-package"),),
    )
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    def _authored(subject_id: str) -> ClaimAuthoringPayloadV1:
        return _claim(subject_id=subject_id, value=LiteralClaimObject(value="demo-package"))

    intent = coordinator.create(
        actor=actor,
        payload=_change_set(*(_authored(item) for item in subjects)),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)
    minted = {item.member_identity: item.claim_id for item in intent.change_set_claim_identities}
    succession = ClaimTypeSuccessionMemberV1(
        successor=_enum_successor(instance, enum=["other-package"]),
        dependents=tuple(
            sorted(
                (
                    _dependent(
                        minted[authoring_member_identity(_authored(item))],
                        disposition="retire",
                        reason="was-rescinded",
                    )
                    for item in subjects
                ),
                key=lambda item: item.identity.qualified.encode("utf-8"),
            )
        ),
    )
    return instance, owner, coordinator, succession


def test_a_succession_dependent_entry_costs_no_more_than_advertised(tmp_path: Path) -> None:
    """The most expensive entry in any change-set record is still under the bound.

    A succession's dependents are the costliest entries the ledger writes: each
    one carries the retirement laws AND the succession's, so a dependent entry
    measures around a fifth more than a plain Claim retirement and six times a
    Subject. The advertised per-entry cost has to bound THIS, or the ceiling
    computed from it admits a set the ledger cannot record -- which is card 110.
    """

    instance, owner, coordinator, succession = _retiring_succession_world(tmp_path, dependents=8)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=_change_set(succession),
        canonical_timestamp=TIMESTAMP,
    ).intent

    _accept(instance, owner, coordinator, intent.intent_id, actor)

    entries, size = accepted_change_set_record(instance)
    # One authored member; nine record entries -- the successor and its eight
    # dependents. The member count never bounded this.
    assert entries == 9
    assert ProposalReceiveLimits().projected_change_set_record_bytes(entries) >= size


def test_a_succession_over_the_record_ceiling_refuses_before_it_is_lowered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One authored member, twenty-one record entries, refused before the compile.

    The bound that counted authored members read this set as ONE member and let
    it through however low the ceiling was set. A succession names its exact
    reverse-pin closure on the member itself, so the entries it will write are
    readable before anything is staged, and the refusal costs one read rather
    than the whole compile. `lower_authoring` is replaced with a function that
    fails, so "never lowered" is proved rather than inferred.
    """

    instance, _owner, coordinator, succession = _retiring_succession_world(tmp_path, dependents=20)
    actor = AuthenticatedActor(actor_id="owner")
    per_member = ProposalReceiveLimits().change_set_record_bytes_per_member
    instance.bind_receive_limits(ProposalReceiveLimits(max_change_set_record_bytes=3 * per_member))
    assert instance.proposal_service().receive_limits.max_change_set_members == 3
    intent = coordinator.create(
        actor=actor,
        payload=_change_set(succession),
        canonical_timestamp=TIMESTAMP,
    ).intent

    def _never(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a succession over the record ceiling was lowered anyway")

    monkeypatch.setattr(preflight_module, "lower_authoring", _never)

    result = coordinator.preflight(intent.intent_id, actor=actor)

    assert result.verdict == "refused"
    diagnostic = next(
        item
        for item in result.frontier.diagnostics
        if item.code == "playbill.authoring.change_set_record_too_large"
    )
    assert "projects to at least 21 record entries" in diagnostic.message
    assert diagnostic.repairs[0].replacement == {
        "record_entries": 21,
        "record_entries_measured": False,
        "max_change_set_members": 3,
        "max_change_set_record_bytes": 3 * per_member,
        "projected_change_set_record_bytes": 21 * per_member,
    }
