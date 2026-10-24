# ruff: noqa: F811 - the instance fixture is shared with test_subject_identity
"""The E-b review's probes, as regressions (identity D1/D4/D6; findings F-001..F-008).

Each test reproduces one reviewer probe against the shared identity rules: core
assigns every new Subject's id with a binding the candidate boundary re-derives
(F-001); a pending creation reference names its member, never a predicted path
(F-002); one resolver -- exact id, alias, key, retired -- serves every route and
alias names are reserved (F-003); a key ClaimType's change is judged against the
kinds it keys (F-004); an instance before subject identity creates nothing
(F-005); revision 33 is its own compiler coordinate (F-006); creation resolves
against the change's own overlay (F-007); typed keys compare by encoding (F-008).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import (
    ClaimTypeInput,
    ClaimTypeSuccessionInput,
    SubjectInput,
    SubjectKindInput,
    lower_authoring_input,
)
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatement,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDrafts,
    SelfSourceBody,
    SubjectCreationAuthoringPayload,
    authoring_member_identity,
)
from cruxible_client.contracts.claims import LiteralClaimObject, SubjectClaimObject, parse_claim
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subject_kinds import SubjectKeyPart
from cruxible_client.contracts.subjects import parse_subject, render_subject, subject_path
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._identity_support import OWNER, shell
from tests.test_service.test_subject_identity import (  # noqa: F401
    DECISION,
    NOTE,
    PAIR,
    TICKET,
    _key,
    accept,
    change_set,
    compile_,
    instance,
    tree,
)

_SELF = SelfSourceBody(content_base64="b3du")  # "own"


def _submit_raw(instance: PlaybillInstance, updates: dict[str, bytes], name: str) -> Any:
    base = instance.accepted_coordinate()
    return instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree={**tree(instance), **updates},
        timestamp="2026-10-09T12:00:00.000000Z",
    )


def _preview(instance: PlaybillInstance, *members: Any) -> Any:
    return _preview_intent(instance, *members)[1]


def _preview_intent(instance: PlaybillInstance, *members: Any) -> tuple[Any, Any]:
    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(members, key=lambda item: authoring_member_identity(item).encode("utf-8"))
        )
    )
    return AuthoringIntentCoordinator.for_instance(instance).preview(
        actor=OWNER, payload=payload, canonical_timestamp="2026-10-09T12:00:00.000000Z"
    )


def _own(intent: Any, computed: Any) -> Any:
    """The assignment authority of one lowering core made (its own recorded creations)."""

    from cruxible_core.authoring.lowering import lowered_authority

    return lowered_authority(intent.intent_id, computed.lowered)


def _subjects(computed: Any) -> list[str]:
    assert computed.lowered is not None, computed.result.frontier
    return sorted(
        path for path, _content in computed.lowered.changed_members if path.startswith("subjects/")
    )


def _claim_about(subject: SemanticAddress, predicate: str, value: object) -> Any:
    return ClaimAuthoringPayloadV2(
        statement=AuthoringClaimStatement(
            subject=subject,
            predicate=predicate,
            object=(
                value if isinstance(value, SubjectClaimObject) else LiteralClaimObject(value=value)
            ),
            role="observation",
        ),
        rationale="Stated about the Subject the change creates.",
        source=_SELF,
        dependency_drafts=ClaimDependencyDrafts(),
    )


# -- F-001: every new Subject carries core's assignment binding ------------------------


def test_a_raw_minted_subject_with_a_chosen_id_refuses_at_receive(
    instance: PlaybillInstance,
) -> None:
    raw = _submit_raw(
        instance,
        {subject_path(TICKET, "author-chosen"): render_subject(shell("author-chosen", TICKET))},
        "raw-minted",
    )
    assert raw.candidate is None
    assert {item.code for item in raw.evaluation.diagnostics} == {
        "cruxible.subject.id_not_assigned"
    }


def test_a_minted_subject_records_the_binding_the_boundary_recomputes(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.subjects import SubjectAssignment

    created = accept(
        instance, lower_authoring_input(SubjectInput(kind="subject", subject_kind=TICKET))
    )
    (path,) = [
        item
        for item in tree(instance)
        if item.startswith(f"subjects/{TICKET}/") and item.endswith(".json")
    ]
    minted = parse_subject(tree(instance)[path], path=path)
    assert minted.assignment is not None and minted.assignment.rule == "minted"
    assert minted.assignment.operation == created.intent.intent_id
    # The same shell under another (operation, member) is not core's id: refused.
    forged = minted.model_copy(
        update={
            "subject_id": "sub-" + "0" * 32,
            "identity": minted.identity.model_copy(update={"name": f"{TICKET}/sub-{'0' * 32}"}),
            "assignment": SubjectAssignment(
                rule="minted", operation=minted.assignment.operation, member="another"
            ),
        }
    )
    raw = _submit_raw(
        instance,
        {subject_path(TICKET, forged.subject_id): render_subject(forged)},
        "forged-minted",
    )
    assert raw.candidate is None
    assert {item.code for item in raw.evaluation.diagnostics} == {
        "cruxible.subject.id_not_assigned"
    }


def test_a_further_hint_holder_with_an_unbound_suffix_refuses(instance: PlaybillInstance) -> None:
    """The boundary recomputes a further holder's suffix from its recorded binding."""

    from cruxible_client.contracts.subjects import SubjectAssignment
    from cruxible_core.indexes.subject_identity import candidate_identity_issues

    accept(
        instance, lower_authoring_input(SubjectInput(kind="subject", subject_kind=NOTE, key="twin"))
    )
    intent, computed = _preview_intent(
        instance,
        SubjectCreationAuthoringPayload(subject_kind=NOTE, key_value="Twin", create_new=True),
    )
    (path,) = _subjects(computed)
    changed = dict(computed.lowered.changed_members)
    held = parse_subject(changed[path], path=path)
    assert held.assignment is not None and held.assignment.rule == "further_holder"
    parent = tree(instance)
    honest = candidate_identity_issues(
        current_tree=parent,
        candidate_tree={**parent, **changed},
        scope=tuple(changed),
        assignment_authority=_own(intent, computed),
    )
    assert honest == ()
    # The binding recorded without its core record (no authority) refuses too.
    unrecorded = candidate_identity_issues(
        current_tree=parent, candidate_tree={**parent, **changed}, scope=tuple(changed)
    )
    assert [item.code for item in unrecorded] == ["cruxible.subject.id_not_assigned"]
    tampered = held.model_copy(
        update={
            "assignment": SubjectAssignment(rule="further_holder", operation="AIT-x", member="m")
        }
    )
    issues = candidate_identity_issues(
        current_tree=parent,
        candidate_tree={**parent, **changed, path: render_subject(tampered)},
        scope=tuple(changed),
        assignment_authority=_own(intent, computed),
    )
    assert [item.code for item in issues] == ["cruxible.subject.id_not_assigned"]


# -- F-002: a pending reference names its creation member ------------------------------


def _grandfathered(instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """A Subject accepted before Subject identity (an older build): it holds no key."""

    import cruxible_core.proposals.proposals as proposals_module
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    kind, _, subject_id = path.removeprefix("subjects/").removesuffix(".json").partition("/")
    with monkeypatch.context() as patched:
        patched.setattr(proposals_module, "boundary_identity_issues", lambda *_a, **_k: ())
        raw = _submit_raw(instance, {path: render_subject(shell(subject_id, kind))}, "pre-d1")
        assert raw.candidate is not None, raw.evaluation.diagnostics
        service_activate_playbill_proposal(
            instance, proposal_id=raw.admission.proposal_id, activated_by="owner"
        )


def test_a_creation_reference_lands_on_the_subject_core_assigns(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review F-002 probe: an occupied id means the reference reaches the suffixed Subject."""

    grand = "dev.grand"
    _grandfathered(instance, monkeypatch, subject_path(grand, "a-b"))
    accept(
        instance,
        change_set(
            ClaimTypeInput(kind="claim_type", claim_type=_key(grand, "slug")),
            ClaimTypeInput(kind="claim_type", claim_type=_key(grand, "note")),
            SubjectKindInput(
                kind="subject_kind", name=grand, keys=(f"{grand}.slug",), uniqueness="unique"
            ),
        ),
    )
    creation = SubjectCreationAuthoringPayload(subject_kind=grand, key_value="a_b")
    reference = SemanticAddress.subject_creation(grand, creation.member_identity.rpartition("@")[2])
    computed = _preview(instance, creation, _claim_about(reference, f"{grand}.note", "x"))
    (created,) = _subjects(computed)
    assert created != subject_path(grand, "a-b") and created.startswith(f"subjects/{grand}/a-b-")
    (note,) = [
        parse_claim(content, path=path)
        for path, content in computed.lowered.changed_members
        if path.startswith("claims/")
        and parse_claim(content, path=path).statement.predicate == f"{grand}.note"
    ]
    assert note.statement.subject.artifact_path == created


def test_a_creation_reference_reaches_composites_and_further_holders(
    instance: PlaybillInstance,
) -> None:
    """Reversed composite input as a Subject-valued object, and a hint create_new."""

    from tests.core_support._identity_support import identity_claim_type

    paired = identity_claim_type(
        "pair",
        canonical_form=None,
        object_kind="subject",
        cardinality="many",
        object_kinds=(PAIR,),
        predicate=f"{DECISION}.pair",
        subject_kinds=(DECISION,),
    )
    accept(
        instance,
        change_set(
            ClaimTypeInput(kind="claim_type", claim_type=paired),
            SubjectInput(kind="subject", subject_kind=DECISION, key="holder"),
            SubjectInput(kind="subject", subject_kind=NOTE, key="twin"),
        ),
    )
    reversed_pair = SubjectCreationAuthoringPayload(
        subject_kind=PAIR,
        key=(
            SubjectKeyPart(predicate=f"{PAIR}.right", value="R"),
            SubjectKeyPart(predicate=f"{PAIR}.left", value="L"),
        ),
    )
    pair_ref = SemanticAddress.subject_creation(
        PAIR, reversed_pair.member_identity.rpartition("@")[2]
    )
    further = SubjectCreationAuthoringPayload(subject_kind=NOTE, key_value="twin", create_new=True)
    computed = _preview(
        instance,
        reversed_pair,
        further,
        _claim_about(
            SemanticAddress.whole_artifact(subject_path(DECISION, "holder")),
            f"{DECISION}.pair",
            SubjectClaimObject(address=pair_ref),
        ),
    )
    subjects = _subjects(computed)
    assert subject_path(PAIR, "l--r") in subjects
    (note_path,) = [item for item in subjects if item.startswith(f"subjects/{NOTE}/")]
    assert note_path.startswith(f"subjects/{NOTE}/twin-")
    (relation,) = [
        parse_claim(content, path=path)
        for path, content in computed.lowered.changed_members
        if path.startswith("claims/")
        and parse_claim(content, path=path).statement.predicate == f"{DECISION}.pair"
    ]
    assert relation.statement.object.address.artifact_path == subject_path(PAIR, "l--r")


def test_an_unknown_creation_reference_refuses(instance: PlaybillInstance) -> None:
    stray = SemanticAddress.subject_creation(DECISION, "0" * 32)
    computed = _preview(instance, _claim_about(stray, f"{DECISION}.title", "x"))
    assert computed.lowered is None
    assert "cruxible.authoring.subject_creation_unresolved" in {
        item.code for item in computed.result.frontier.diagnostics
    }


# -- F-005 / F-006: a compiler before subject identity creates nothing -------------------


def test_revision_33_is_its_own_coordinate_and_revision_32_keeps_its_grammar() -> None:
    from cruxible_core.compiler.compiler import (
        GOVERNED_TRIGGERS_COMPILER,
        LIGHT_ARTIFACTS_COMPILER,
        SUBJECT_IDENTITY_COMPILER,
        artifact_kinds_for_compiler,
        current_compiler_coordinate,
        projection_registry_for_compiler,
    )

    assert SUBJECT_IDENTITY_COMPILER != GOVERNED_TRIGGERS_COMPILER
    assert current_compiler_coordinate() == LIGHT_ARTIFACTS_COMPILER
    old = {
        entry.kind for entry in artifact_kinds_for_compiler(GOVERNED_TRIGGERS_COMPILER).entries()
    }
    new = {entry.kind for entry in artifact_kinds_for_compiler(SUBJECT_IDENTITY_COMPILER).entries()}
    assert new - old == {"subject-kind"}
    assert projection_registry_for_compiler(
        GOVERNED_TRIGGERS_COMPILER
    ) != projection_registry_for_compiler(SUBJECT_IDENTITY_COMPILER)


def test_an_instance_before_subject_identity_creates_no_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.compiler.compiler import GOVERNED_TRIGGERS_COMPILER
    from tests.test_ledger.test_compiler_upgrade import old_instance

    old, _owner, _reviewer = old_instance(tmp_path, monkeypatch, GOVERNED_TRIGGERS_COMPILER)
    raw = _submit_raw(
        old, {subject_path(DECISION, "fresh"): render_subject(shell("fresh", DECISION))}, "old-raw"
    )
    assert raw.candidate is None
    assert {item.code for item in raw.evaluation.diagnostics} == {
        "cruxible.subject.compiler_upgrade_required"
    }
    authored = compile_(
        old, lower_authoring_input(SubjectInput(kind="subject", subject_kind=DECISION, key="x"))
    )
    assert authored.verdict == "refused"
    assert "cruxible.subject.compiler_upgrade_required" in {
        item.code for item in authored.frontier.diagnostics
    }


# -- F-004: a key ClaimType's change is judged against the kinds it keys --------------


def _slug_successor(instance: PlaybillInstance, **changes: Any) -> Any:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.claim_types import (
        claim_type_digest,
        claim_type_path,
        parse_claim_type,
    )

    path = claim_type_path(f"{DECISION}.slug")
    current = parse_claim_type(tree(instance)[path], path=path)
    return current.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(current).tagged),
            **changes,
        }
    )


def _codes(result: Any) -> set[str]:
    return {item.code for item in result.frontier.diagnostics}


def test_a_key_claim_type_made_many_valued_refuses_while_it_keys_a_kind(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.policies import ClaimResolutionPolicy

    many = _slug_successor(
        instance,
        cardinality="many",
        resolution_policy=ClaimResolutionPolicy(
            cardinality="many", eligible_verdicts=("supported",), selector="all"
        ),
    )
    result = compile_(
        instance,
        change_set(
            ClaimTypeSuccessionInput(kind="claim_type_succession", successor=many, carry_all=True)
        ),
    )
    assert result.verdict == "refused"
    assert "cruxible.subject_kind.key_claim_type_broken" in _codes(result), _codes(result)
    # Succeeding the kind to minted in the same change releases the key.
    released = compile_(
        instance,
        change_set(
            ClaimTypeSuccessionInput(kind="claim_type_succession", successor=many, carry_all=True),
            SubjectKindInput(kind="subject_kind", name=DECISION, minted=True),
        ),
    )
    assert "cruxible.subject_kind.key_claim_type_broken" not in _codes(released), _codes(released)


def test_a_key_canonical_form_change_is_a_rekeying(instance: PlaybillInstance) -> None:
    accept(
        instance,
        lower_authoring_input(SubjectInput(kind="subject", subject_kind=DECISION, key="Ship It")),
    )
    widened = _slug_successor(
        instance,
        canonical_form=("nfkc", "trim", "collapse_whitespace", "casefold", "strip_punctuation"),
    )
    result = compile_(
        instance,
        change_set(
            ClaimTypeSuccessionInput(
                kind="claim_type_succession", successor=widened, carry_all=True
            )
        ),
    )
    # "ship it" is already fixed under the wider form: no stale value, no new collision.
    assert result.verdict == "passed", _codes(result)
    accept(
        instance,
        lower_authoring_input(SubjectInput(kind="subject", subject_kind=DECISION, key="ship-it!")),
    )
    stale = compile_(
        instance,
        change_set(
            ClaimTypeSuccessionInput(
                kind="claim_type_succession", successor=widened, carry_all=True
            )
        ),
    )
    # The key check judges the re-keying itself, whatever the carry rules say.
    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_core.indexes.subject_identity import candidate_identity_issues

    slug_path = claim_type_path(f"{DECISION}.slug")
    parent = tree(instance)
    issues = candidate_identity_issues(
        current_tree=parent,
        candidate_tree={**parent, slug_path: render_claim_type(widened)},
        scope=(slug_path,),
    )
    assert [item.code for item in issues] == ["cruxible.subject_kind.rekey_unhandled"]
    # "ship-it!" renders to the id "Ship It" already holds, so it took the suffix.
    (stale_holder,) = issues[0].existing
    assert stale_holder.startswith(f"{DECISION}/ship-it-")
    # A key value outside the new form is a holder the change must re-author: the
    # carry refuses it (the ClaimType law), and so does the key check behind it.
    assert stale.verdict == "refused"
    assert _codes(stale) & {
        "cruxible.change_set.incomplete_closure",
        "cruxible.subject_kind.rekey_unhandled",
    }, _codes(stale)


# -- F-007: creation resolves against the change's own overlay -------------------------


def test_a_key_moved_in_the_same_change_is_free_for_a_creation(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.claims import claim_path

    created = accept(
        instance,
        lower_authoring_input(SubjectInput(kind="subject", subject_kind=DECISION, key="original")),
    )
    del created
    (slug_claim,) = [
        parse_claim(content, path=path)
        for path, content in tree(instance).items()
        if path.startswith("claims/")
        and parse_claim(content, path=path).statement.predicate == f"{DECISION}.slug"
    ]
    moved = _claim_about(
        SemanticAddress.whole_artifact(subject_path(DECISION, "original")),
        f"{DECISION}.slug",
        "renamed",
    ).model_copy(update={"revises": slug_claim.identity.name})
    again = SubjectCreationAuthoringPayload(subject_kind=DECISION, key_value="original")
    # The holder takes another key in the same change: the key is free, so a new
    # Subject is created (its rendering is taken by the holder's id).
    subjects = _subjects(_preview(instance, moved, again))
    assert len(subjects) == 1 and subjects[0].startswith(f"subjects/{DECISION}/original-")
    del claim_path


def test_a_subject_retired_in_the_same_change_holds_no_key(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subjects import subject_digest
    from cruxible_core.authoring.lowering import subject_creation_paths

    accept(
        instance,
        lower_authoring_input(SubjectInput(kind="subject", subject_kind=DECISION, key="original")),
    )
    path = subject_path(DECISION, "original")
    current = parse_subject(tree(instance)[path], path=path)
    retired = current.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_digest(current).tagged
            )
        }
    )
    from cruxible_client.contracts.authoring.models import SubjectAuthoringPayload

    creation = SubjectCreationAuthoringPayload(subject_kind=DECISION, key_value="original")
    head = instance.accepted_coordinate()
    landed = subject_creation_paths(
        instance,
        operation="AIT-" + "1" * 32,
        base=head,
        base_tree=instance.immutable_tree_at(head.git_oid),
        members=(SubjectAuthoringPayload(subject=retired), creation),
    )
    ((created, reused),) = landed.values()
    assert not reused and created.startswith(f"subjects/{DECISION}/original-")


def test_a_composite_key_partly_revised_in_the_change_is_free(
    instance: PlaybillInstance,
) -> None:
    accept(
        instance,
        lower_authoring_input(
            SubjectInput(kind="subject", subject_kind=PAIR, key={"left": "a", "right": "b"})
        ),
    )
    (right,) = [
        parse_claim(content, path=path)
        for path, content in tree(instance).items()
        if path.startswith("claims/")
        and parse_claim(content, path=path).statement.predicate == f"{PAIR}.right"
    ]
    revised = _claim_about(
        SemanticAddress.whole_artifact(subject_path(PAIR, "a--b")), f"{PAIR}.right", "c"
    ).model_copy(update={"revises": right.identity.name})
    again = SubjectCreationAuthoringPayload(
        subject_kind=PAIR,
        key=(
            SubjectKeyPart(predicate=f"{PAIR}.left", value="a"),
            SubjectKeyPart(predicate=f"{PAIR}.right", value="b"),
        ),
    )
    subjects = _subjects(_preview(instance, revised, again))
    assert len(subjects) == 1 and subjects[0].startswith(f"subjects/{PAIR}/a--b-")


# == E-b delta review ==================================================================
#
# F-001 (delta): a recorded assignment names an authoritative core record.


def _forged(kind: str, operation: str, member: str) -> tuple[str, bytes]:
    from cruxible_client.contracts.subject_kinds import minted_subject_id
    from cruxible_client.contracts.subjects import SubjectAssignment

    assignment = SubjectAssignment(rule="minted", operation=operation, member=member)
    subject_id = minted_subject_id(operation=operation, member=member)
    return subject_path(kind, subject_id), render_subject(
        shell(subject_id, kind).model_copy(update={"assignment": assignment})
    )


def _accepted_minted(instance: PlaybillInstance) -> Any:
    before = set(tree(instance))
    accept(instance, lower_authoring_input(SubjectInput(kind="subject", subject_kind=TICKET)))
    (path,) = [
        item
        for item in tree(instance)
        if item.startswith(f"subjects/{TICKET}/") and item not in before
    ]
    held = parse_subject(tree(instance)[path], path=path)
    assert held.assignment is not None
    return held.assignment


def _refusals(result: Any) -> set[str]:
    return {item.code for item in result.evaluation.diagnostics}


def test_an_invented_binding_whose_id_matches_refuses_at_admission(
    instance: PlaybillInstance,
) -> None:
    """Review probe ``forge.py``: id = digest(invented operation, invented member)."""

    path, content = _forged(TICKET, "AIT-never-created-by-core", "copied-or-invented-member")
    raw = _submit_raw(instance, {path: content}, "invented")
    assert raw.candidate is None
    assert _refusals(raw) == {"cruxible.subject.id_not_assigned"}
    message = raw.evaluation.diagnostics[0].message
    assert "core recorded no such creation" in message and "through authoring" in message


def test_a_real_binding_copied_to_another_kind_or_operation_refuses(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.authoring.inputs import SubjectKindInput

    real = _accepted_minted(instance)
    accept(
        instance,
        lower_authoring_input(SubjectKindInput(kind="subject_kind", name="dev.other", minted=True)),
    )
    # The same operation and member, another minted kind: that record creates a ticket.
    path, content = _forged("dev.other", real.operation, real.member)
    assert _refusals(_submit_raw(instance, {path: content}, "cross-kind")) == {
        "cruxible.subject.id_not_assigned"
    }
    # The member of one real operation under another real operation (a stored
    # intent that creates something else).
    other, _changed = _stored(
        instance, SubjectCreationAuthoringPayload(subject_kind=NOTE, key_value="elsewhere")
    )
    path, content = _forged(TICKET, other.intent_id, real.member)
    assert _refusals(_submit_raw(instance, {path: content}, "cross-operation")) == {
        "cruxible.subject.id_not_assigned"
    }


def test_a_preview_binding_never_stored_refuses_when_replayed_raw(
    instance: PlaybillInstance,
) -> None:
    """A preview's intent is not a durable record: its lowered Subject cannot be replayed."""

    _intent, computed = _preview_intent(
        instance, SubjectCreationAuthoringPayload(subject_kind=TICKET)
    )
    assert computed.result.verdict == "passed"
    replay = _submit_raw(instance, dict(computed.lowered.changed_members), "preview-replay")
    assert replay.candidate is None
    assert _refusals(replay) == {"cruxible.subject.id_not_assigned"}


def _stored(instance: PlaybillInstance, *members: Any) -> tuple[Any, dict[str, bytes]]:
    """A stored authoring intent of ``members``, and the tree its lowering writes."""

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    created = coordinator.create(
        actor=OWNER,
        payload=ChangeSetAuthoringPayload(
            members=tuple(
                sorted(members, key=lambda item: authoring_member_identity(item).encode("utf-8"))
            )
        ),
        canonical_timestamp="2026-10-09T12:00:00.000000Z",
    )
    # Preflighted as a stored intent is: core records the lowering's creations.
    computed, _bound = coordinator._compute_and_bind_preflight(  # noqa: SLF001
        created.intent.intent_id, actor=OWNER
    )
    assert computed.lowered is not None, computed.result.frontier
    return created.intent, dict(computed.lowered.changed_members)


def _stored_minted(instance: PlaybillInstance) -> tuple[Any, dict[str, bytes]]:
    return _stored(instance, SubjectCreationAuthoringPayload(subject_kind=TICKET))


def test_a_stored_intents_binding_holds_through_readmission_and_settlement(
    instance: PlaybillInstance,
) -> None:
    """The record is the intent: unchanged by a rebase, read again at settlement."""

    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    intent, changed = _stored_minted(instance)
    base = instance.accepted_coordinate()
    stale_tree = tree(instance)
    # The head moves on; the proposal is evaluated against the new head (readmitted).
    accept(
        instance, lower_authoring_input(SubjectInput(kind="subject", subject_kind=NOTE, key="x"))
    )
    readmitted = instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/readmitted", proposed_base_oid=base.git_oid
        ),
        candidate_tree={**stale_tree, **changed},
        timestamp="2026-10-09T12:00:00.000000Z",
    )
    assert readmitted.candidate is not None, readmitted.evaluation.diagnostics
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=readmitted.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    (path,) = [item for item in changed if item.startswith(f"subjects/{TICKET}/")]
    held = parse_subject(tree(instance)[path], path=path)
    assert held.assignment is not None and held.assignment.operation == intent.intent_id


def test_settlement_reads_the_record_again(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admitted with its record; the record gone by settlement, activation refuses."""

    from cruxible_core.authoring import assignment_records
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    _intent, changed = _stored_minted(instance)
    admitted = _submit_raw(instance, changed, "settled-later")
    assert admitted.candidate is not None, admitted.evaluation.diagnostics
    monkeypatch.setattr(
        assignment_records.SubjectAssignmentRecords, "__call__", lambda *_args: False
    )
    base = instance.accepted_coordinate()
    with pytest.raises(Exception):  # noqa: B017 - settlement refuses before activating
        service_activate_playbill_proposal(
            instance, proposal_id=admitted.admission.proposal_id, activated_by="owner"
        )
    assert instance.accepted_coordinate() == base


def test_a_raw_proposal_still_creates_a_keyed_first_holder(instance: PlaybillInstance) -> None:
    """No core record needed: a first holder's id is recomputed from its key."""

    import shutil

    from cruxible_core.authoring.assignment_records import SUBJECT_CREATION_RECORDS

    _intent, changed = _stored(
        instance, SubjectCreationAuthoringPayload(subject_kind=NOTE, key_value="Raw First")
    )
    # The proposal stands alone: no intent and no creation record remain.
    exhaust = instance.root / instance.descriptor.storage.exhaust
    shutil.rmtree(exhaust / "authoring-intents")
    shutil.rmtree(exhaust / SUBJECT_CREATION_RECORDS)
    raw = _submit_raw(instance, changed, "raw-first-holder")
    assert raw.candidate is not None, raw.evaluation.diagnostics


# F-001 (delta 2): the record holds each creation's actual assignment outcome.


def _rebound(
    changed: dict[str, bytes], *, assignment: Any, key: tuple[str, ...], value: str | None = None
) -> tuple[str, dict[str, bytes]]:
    """``changed`` with its further holder re-pointed at ``assignment`` and ``key``."""

    from cruxible_client.contracts.claims import render_claim
    from cruxible_client.contracts.subject_kinds import distinct_holder_id
    from cruxible_client.contracts.subjects import subject_digest

    (path,) = [item for item in changed if item.startswith("subjects/")]
    old = parse_subject(changed[path], path=path)
    subject_id = distinct_holder_id(key, operation=assignment.operation, member=assignment.member)
    new = shell(subject_id, old.subject_kind).model_copy(update={"assignment": assignment})
    rebound = {
        (subject_path(old.subject_kind, subject_id) if item == path else item): content.replace(
            old.subject_id.encode(), subject_id.encode()
        ).replace(subject_digest(old).tagged.encode(), subject_digest(new).tagged.encode())
        for item, content in changed.items()
    }
    rebound[subject_path(old.subject_kind, subject_id)] = render_subject(new)
    if value is not None:
        for item, content in list(rebound.items()):
            if item.startswith("claims/"):
                claim = parse_claim(content, path=item)
                rebound[item] = render_claim(
                    claim.model_copy(
                        update={
                            "statement": claim.statement.model_copy(
                                update={"object": LiteralClaimObject(value=value)}
                            )
                        }
                    )
                )
    return subject_path(old.subject_kind, subject_id), rebound


def test_a_reused_members_record_authorizes_no_further_holder(
    instance: PlaybillInstance,
) -> None:
    """Review probe ``reuse_binding_delta2b``: the record says the member reused twin."""

    from cruxible_client.contracts.subjects import SubjectAssignment
    from cruxible_core.authoring.assignment_records import SubjectAssignmentRecords

    accept(
        instance, lower_authoring_input(SubjectInput(kind="subject", subject_kind=NOTE, key="Twin"))
    )
    reusing = SubjectCreationAuthoringPayload(subject_kind=NOTE, key_value="Twin")
    real, own = _stored(instance, reusing)
    assert own == {}  # its own lowering reused the existing twin
    record = SubjectAssignmentRecords(instance.root / instance.descriptor.storage.exhaust).record(
        real.intent_id
    )
    assert record is not None and [item.outcome for item in record.creations] == ["reused"]
    _other, further = _stored(
        instance,
        SubjectCreationAuthoringPayload(subject_kind=NOTE, key_value="Twin", create_new=True),
    )
    binding = SubjectAssignment(
        rule="further_holder", operation=real.intent_id, member=reusing.member_identity
    )
    _path, forged = _rebound(further, assignment=binding, key=("twin",))
    raw = _submit_raw(instance, forged, "reuse-to-further")
    assert raw.candidate is None
    assert _refusals(raw) == {"cruxible.subject.id_not_assigned"}


def test_a_hint_record_authorizes_only_its_own_key_and_id(instance: PlaybillInstance) -> None:
    """Review probe ``cross_key_binding_delta2``: a Twin further holder copied to key Other."""

    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    for key in ("Twin", "Other"):
        accept(
            instance,
            lower_authoring_input(SubjectInput(kind="subject", subject_kind=NOTE, key=key)),
        )
    _real, changed = _stored(
        instance,
        SubjectCreationAuthoringPayload(subject_kind=NOTE, key_value="Twin", create_new=True),
    )
    (path,) = [item for item in changed if item.startswith("subjects/")]
    held = parse_subject(changed[path], path=path)
    assert held.assignment is not None and held.assignment.rule == "further_holder"
    _other_path, forged = _rebound(
        changed, assignment=held.assignment, key=("other",), value="other"
    )
    raw = _submit_raw(instance, forged, "cross-key")
    assert raw.candidate is None
    assert _refusals(raw) == {"cruxible.subject.id_not_assigned"}
    # Its own exact creation is the one replay the record admits.
    own = _submit_raw(instance, changed, "own-further-holder")
    assert own.candidate is not None, own.evaluation.diagnostics
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=own.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted" and path in tree(instance)


# F-003 (delta): Procedure key references go through the common resolver.


def _procedure_address(
    instance: PlaybillInstance, key: Any, *, created: tuple[Any, ...] = ()
) -> Any:
    from cruxible_client.contracts.procedures.proposal_items import SubjectKeyReference
    from cruxible_core.indexes.subject_identity import IdentityView, tree_key_reader
    from cruxible_core.procedures.proposal_delivery import _SubjectKeys

    base = instance.accepted_coordinate()
    base_tree = instance.immutable_tree_at(base.git_oid)
    keys = _SubjectKeys(instance, base=base, base_tree=base_tree)
    keys.add_created(created)
    with instance.bind_accepted_projection(base) as projection:
        keys.bind(IdentityView(projection.typed.connection, key_reader=tree_key_reader(base_tree)))
        return keys.address(SubjectKeyReference(subject_kind=DECISION, key=key))


def test_a_procedure_key_reference_reads_an_alias_before_a_key(
    instance: PlaybillInstance,
) -> None:
    """Review probe ``alias_route.py``: alias ``other name`` and key ``other name`` differ."""

    from cruxible_core.indexes.subject_identity import IdentityView
    from tests.test_service.test_subject_identity import _alias_member, _create

    _create(instance, DECISION, "original")
    _create(instance, DECISION, "other name")
    accept(instance, change_set(_alias_member(DECISION, "original", "other name")))
    address = _procedure_address(instance, {"slug": "other name"})
    assert address.artifact_path == subject_path(DECISION, "original")
    # The creation resolver reads the same text the same way.
    base = instance.accepted_coordinate()
    with instance.bind_accepted_projection(base) as projection:
        creation = IdentityView(projection.typed.connection).resolve_creation(
            DECISION,
            _decision_key(instance, "other name"),
            single_text="other name",
            create_new=False,
        )
    assert creation.path == address.artifact_path


def _decision_key(instance: PlaybillInstance, value: str) -> Any:
    from cruxible_client.contracts.subject_kinds import live_subject_kind
    from cruxible_core.indexes.subject_identity import canonical_key

    base_tree = tree(instance)
    declared = live_subject_kind(base_tree, DECISION)
    assert declared is not None

    def claim_type(predicate: str) -> Any:
        from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type

        path = claim_type_path(predicate)
        return parse_claim_type(base_tree[path], path=path)

    return canonical_key(declared, {"slug": value}, claim_type=claim_type)


def test_a_procedure_key_reference_by_an_alias_alone_or_two_aliases(
    instance: PlaybillInstance,
) -> None:
    from cruxible_core.procedures.proposal_delivery import ProposalDeliveryRefused
    from tests.test_service.test_subject_identity import _alias_member, _create

    _create(instance, DECISION, "first")
    _create(instance, DECISION, "second")
    accept(instance, change_set(_alias_member(DECISION, "first", "nickname")))
    # Only an alias names it: the reference resolves (it no longer refuses key_missing).
    assert _procedure_address(instance, {"slug": "nickname"}).artifact_path == subject_path(
        DECISION, "first"
    )
    accept(instance, change_set(_alias_member(DECISION, "second", "nickname")))
    with pytest.raises(ProposalDeliveryRefused) as refused:
        _procedure_address(instance, {"slug": "nickname"})
    assert refused.value.details["code"] == "cruxible.authoring.alias_ambiguous"


def test_a_sibling_subject_item_keeps_the_reference_a_creation_member(
    instance: PlaybillInstance,
) -> None:
    """With a Subject item carrying the key, the reference names that item, unresolved."""

    from tests.test_service.test_subject_identity import _alias_member, _create

    _create(instance, DECISION, "original")
    accept(instance, change_set(_alias_member(DECISION, "original", "other name")))
    item = SubjectCreationAuthoringPayload(
        subject_kind=DECISION,
        key=(SubjectKeyPart(predicate=f"{DECISION}.slug", value="other name"),),
    )
    address = _procedure_address(instance, {"slug": "other name"}, created=(item,))
    assert address.selector.scheme == "subject-creation-v1"
    assert item.member_identity.endswith(address.selector.value)


# Complexity (delta follow-up): a batch overlay encodes each key once.


def test_a_batch_of_composite_creations_encodes_each_key_a_constant_number_of_times(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review probe ``count_overlay.py``: was N(N-1)/2 + 2N encodings for N creations."""

    import cruxible_core.indexes.subject_identity as identity
    from cruxible_core.authoring.lowering import subject_creation_paths

    base = instance.accepted_coordinate()
    base_tree = instance.immutable_tree_at(base.git_oid)
    original = identity.key_encoding
    counts: dict[int, int] = {}
    for size in (10, 100):
        seen = [0]

        def counted(key: Any, seen: list[int] = seen) -> str:
            seen[0] += 1
            return original(key)

        monkeypatch.setattr(identity, "key_encoding", counted)
        members = tuple(
            SubjectCreationAuthoringPayload(
                subject_kind=PAIR,
                key=(
                    SubjectKeyPart(predicate=f"{PAIR}.left", value="common"),
                    SubjectKeyPart(predicate=f"{PAIR}.right", value=f"r{number}"),
                ),
            )
            for number in range(size)
        )
        paths = subject_creation_paths(
            instance, operation="AIT-counted", base=base, base_tree=base_tree, members=members
        )
        monkeypatch.setattr(identity, "key_encoding", original)
        assert len(paths) == size
        counts[size] = seen[0]
    assert counts[100] <= 10 * counts[10]


# F-002 (delta 2): a Subject-valued object reference is held to the same rule.


def test_a_subject_valued_object_reference_is_checked_like_a_subject(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.authoring.models import ClaimAuthoringPayloadV1
    from cruxible_client.contracts.procedures.proposal_items import ProcedureKeyedClaimProposalItem
    from cruxible_core.indexes.subject_identity import IdentityView, tree_key_reader
    from cruxible_core.procedures.proposal_delivery import ProposalDeliveryRefused, _SubjectKeys
    from tests.test_service.test_subject_identity import _alias_member, _create

    _create(instance, DECISION, "original")
    _create(instance, DECISION, "other name")
    base = instance.accepted_coordinate()
    base_tree = instance.immutable_tree_at(base.git_oid)
    item = ProcedureKeyedClaimProposalItem.model_validate(
        {
            "tag": "playbill-procedure-claim-proposal-item-v3",
            "statement": {
                "tag": "playbill-procedure-claim-statement-v1",
                "subject": SemanticAddress.whole_artifact(
                    subject_path(DECISION, "original")
                ).model_dump(mode="json"),
                "predicate": f"{DECISION}.related",
                "object": {
                    "kind": "subject_key",
                    "subject": {"subject_kind": DECISION, "key": {"slug": "other name"}},
                },
                "role": "observation",
            },
            "rationale": "Related decisions.",
            "revises": None,
        }
    )
    (alias,) = [
        member
        for member in change_set(_alias_member(DECISION, "original", "other name")).members
        if isinstance(member, ClaimAuthoringPayloadV1)
    ]
    keys = _SubjectKeys(instance, base=base, base_tree=base_tree)
    with instance.bind_accepted_projection(base) as projection:
        connection = projection.typed.connection
        keys.bind(IdentityView(connection, key_reader=tree_key_reader(base_tree)))
        statement = keys.statement(item)
        assert isinstance(statement.object, SubjectClaimObject)
        # Frozen at the base: the key names other-name.
        assert statement.object.address.artifact_path == subject_path(DECISION, "other-name")
        keys.unstable(connection, members=(), subject_members=(), creations={})  # stable alone
        with pytest.raises(ProposalDeliveryRefused) as refused:
            # The same terminal adding the alias makes "other name" name original.
            keys.unstable(connection, members=(alias,), subject_members=(), creations={})
    assert refused.value.details["code"] == "cruxible.procedure.identity_reference_unstable"
    (changed,) = refused.value.details["references"]
    assert changed["final"] == subject_path(DECISION, "original")


# F-003 (delta 2): an intent event is published whole, never seen partial.


def test_a_none_of_a_subject_named_by_key_resolves_like_a_subject_value(
    instance: PlaybillInstance,
) -> None:
    """A Procedure item may state a none (inverse) whose ``of`` names its Subject by key."""

    from cruxible_client.contracts.authoring.models import AuthoringNoneClaimObject
    from cruxible_client.contracts.procedures.proposal_items import (
        ProcedureKeyedClaimProposalItem,
        ProcedureNoneClaimObject,
    )
    from cruxible_core.indexes.subject_identity import IdentityView, tree_key_reader
    from cruxible_core.procedures.proposal_delivery import _SubjectKeys
    from tests.test_service.test_subject_identity import _create

    _create(instance, DECISION, "original")
    _create(instance, DECISION, "other name")
    base = instance.accepted_coordinate()
    base_tree = instance.immutable_tree_at(base.git_oid)
    item = ProcedureKeyedClaimProposalItem.model_validate(
        {
            "tag": "playbill-procedure-claim-proposal-item-v3",
            "statement": {
                "tag": "playbill-procedure-claim-statement-v1",
                "subject": SemanticAddress.whole_artifact(
                    subject_path(DECISION, "original")
                ).model_dump(mode="json"),
                "predicate": f"{DECISION}.related",
                "object": {
                    "kind": "none",
                    "of": {
                        "kind": "subject_key",
                        "subject": {"subject_kind": DECISION, "key": {"slug": "other name"}},
                    },
                },
                "role": "observation",
            },
            "rationale": "Not related.",
            "revises": None,
        }
    )
    assert isinstance(item.statement.object, ProcedureNoneClaimObject)
    keys = _SubjectKeys(instance, base=base, base_tree=base_tree)
    with instance.bind_accepted_projection(base) as projection:
        keys.bind(IdentityView(projection.typed.connection, key_reader=tree_key_reader(base_tree)))
        statement = keys.statement(item)
    assert isinstance(statement.object, AuthoringNoneClaimObject)
    assert isinstance(statement.object.of, SubjectClaimObject)
    assert statement.object.of.address.artifact_path == subject_path(DECISION, "other-name")
    # A slot none (no ``of``) passes through as the plain none.
    slot = item.model_copy(
        update={
            "statement": item.statement.model_copy(update={"object": ProcedureNoneClaimObject()})
        }
    )
    assert keys.statement(slot).object == AuthoringNoneClaimObject()


def test_an_appended_intent_event_is_never_visible_partially(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review probe ``torn_intent_delta2b``: a reader during an append sees whole events.

    The writer is paused halfway through the bytes of a new event; a lock-free
    reader of the stream still validates every event (the new one is not under
    its final name yet), the operation's creations stay authorized, and the
    lowered Subject is admitted throughout.
    """

    import threading

    from cruxible_core.authoring import store as store_module

    intent, changed = _stored_minted(instance)
    (creation,) = [
        item for item in intent.payload.members if isinstance(item, SubjectCreationAuthoringPayload)
    ]
    authority = instance.subject_assignment_authority()
    (record,) = authority.record(intent.intent_id).recorded()  # type: ignore[union-attr]
    assert record.member == creation.member_identity and authority(intent.intent_id, record)
    store = store_module.AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust)
    events = store.root / intent.intent_id / "events"
    before = sorted(path.name for path in events.glob("*.json"))
    halfway, release, errors = threading.Event(), threading.Event(), []
    write = store_module._write_temporary

    def paused(path: Path, content: bytes) -> None:
        cut = len(content) // 2
        with path.open("xb") as handle:
            handle.write(content[:cut])
            handle.flush()
            halfway.set()
            assert release.wait(20)
            handle.write(content[cut:])

    def append() -> None:
        try:
            store.transition(
                intent.intent_id,
                actor_id="owner",
                operation_key="sha256:" + "f" * 64,
                transform=lambda value: value,
            )
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    monkeypatch.setattr(store_module, "_write_temporary", paused)
    writer = threading.Thread(target=append)
    writer.start()
    try:
        assert halfway.wait(20), errors
        assert sorted(path.name for path in events.glob("*.json")) == before
        assert len(store._validated_events(store.root / intent.intent_id)) == len(before)
        assert authority(intent.intent_id, record)
        during = _submit_raw(instance, changed, "during-append")
        assert during.candidate is not None, during.evaluation.diagnostics
    finally:
        release.set()
        writer.join(20)
        monkeypatch.setattr(store_module, "_write_temporary", write)
    assert not errors, errors
    assert len(sorted(events.glob("*.json"))) == len(before) + 1
    assert authority(intent.intent_id, record)


# E-b delta-3 F-001: the stability overlay reads aliases as accepted extraction does.


def test_a_qualified_alias_redirects_a_subject_valued_object_reference(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.authoring.models import ClaimAuthoringPayloadV1
    from cruxible_client.contracts.procedures.proposal_items import ProcedureKeyedClaimProposalItem
    from cruxible_core.indexes.subject_identity import IdentityView, tree_key_reader
    from cruxible_core.procedures.proposal_delivery import ProposalDeliveryRefused, _SubjectKeys
    from tests.test_service.test_subject_identity import _alias_member, _create

    _create(instance, DECISION, "original")
    _create(instance, DECISION, "other name")
    base = instance.accepted_coordinate()
    base_tree = instance.immutable_tree_at(base.git_oid)
    item = ProcedureKeyedClaimProposalItem.model_validate(
        {
            "tag": "playbill-procedure-claim-proposal-item-v3",
            "statement": {
                "tag": "playbill-procedure-claim-statement-v1",
                "subject": SemanticAddress.whole_artifact(
                    subject_path(DECISION, "original")
                ).model_dump(mode="json"),
                "predicate": f"{DECISION}.related",
                "object": {
                    "kind": "subject_key",
                    "subject": {"subject_kind": DECISION, "key": {"slug": "other name"}},
                },
                "role": "observation",
            },
            "rationale": "Related decisions.",
            "revises": None,
        }
    )
    (alias,) = [
        member
        for member in change_set(_alias_member(DECISION, "original", "other name")).members
        if isinstance(member, ClaimAuthoringPayloadV1)
    ]
    qualified = alias.model_copy(
        update={"statement": alias.statement.model_copy(update={"qualifier": "local-name"})}
    )
    keys = _SubjectKeys(instance, base=base, base_tree=base_tree)
    with instance.bind_accepted_projection(base) as projection:
        connection = projection.typed.connection
        keys.bind(IdentityView(connection, key_reader=tree_key_reader(base_tree)))
        keys.statement(item)
        with pytest.raises(ProposalDeliveryRefused) as refused:
            keys.unstable(connection, members=(qualified,), subject_members=(), creations={})
    assert refused.value.details["code"] == "cruxible.procedure.identity_reference_unstable"
    (changed,) = refused.value.details["references"]
    assert changed["final"] == subject_path(DECISION, "original")


def test_the_member_overlay_adds_exactly_the_aliases_accepted_extraction_indexes() -> None:
    """Parity: a pending Claim's alias rows are the rows its accepted version contributes."""

    import sqlite3

    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        ClaimAuthoringPayload,
        ClaimDependencyDrafts,
    )
    from cruxible_core.authoring.lowering import members_identity_overlay
    from cruxible_core.indexes.typed_state import schema_sql, statement_vocabulary_terms

    target = SemanticAddress.whole_artifact(subject_path(DECISION, "original"))
    statements = [
        AuthoringClaimStatement(
            subject=target,
            predicate="semantic.alias",
            object=LiteralClaimObject(value=value),
            qualifier=qualifier,
            role="normative",
        )
        for value, qualifier in (("Plain", None), ("Qualified Name", "local-name"))
    ]
    members = tuple(
        ClaimAuthoringPayload(
            statement=statement,
            rationale="Another name.",
            source=_SELF,
            dependency_drafts=ClaimDependencyDrafts(),
        )
        for statement in statements
    )
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(schema_sql())
        overlay = members_identity_overlay(connection, members=members, base_tree={})
    expected = {
        (row[1], row[3])
        for statement in statements
        for row in statement_vocabulary_terms("pending", statement)
        if row[2] == "alias"
    }
    assert overlay.aliases_added == expected and len(expected) == 2


# Maintainer ruling: a keyed Subject's key Claims follow its lifecycle.


def _key_claims_of(instance: PlaybillInstance, path: str) -> dict[str, str]:
    """Each Claim about ``path`` whose predicate is a key: its lifecycle, by Claim path."""

    found = {}
    for claim_path, content in tree(instance).items():
        if not claim_path.startswith("claims/"):
            continue
        claim = parse_claim(content, path=claim_path)
        if claim.statement.subject.artifact_path == path and claim.statement.predicate.endswith(
            ".slug"
        ):
            found[claim_path] = claim.lifecycle.state
    return found


def _succession(instance: PlaybillInstance, path: str, *, retire: bool) -> Any:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.authoring.inputs import SubjectSuccessionInput
    from cruxible_client.contracts.subjects import subject_digest

    held = parse_subject(tree(instance)[path], path=path)
    return SubjectSuccessionInput(
        kind="subject_succession",
        subject=held.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    state="retired" if retire else "live",
                    predecessor_digest=subject_digest(held).tagged,
                )
            }
        ),
    )


def test_a_merge_retires_the_merged_subjects_key_claims_and_frees_its_key(
    instance: PlaybillInstance,
) -> None:
    """Merge = alias the survivor + retire the other: core retires the other's key Claim
    in the same change, so a later create-by-key of that key creates a new Subject."""

    from tests.test_service.test_subject_identity import _alias_member, _create

    _create(instance, DECISION, "alpha")
    _create(instance, DECISION, "beta")
    beta = subject_path(DECISION, "beta")
    assert set(_key_claims_of(instance, beta).values()) == {"live"}
    head = len(instance.accepted_history())

    # One change: the alias and the retirement; no key Claim named by the author.
    accept(
        instance,
        change_set(
            _alias_member(DECISION, "alpha", "Beta Incorporated"),
            _succession(instance, beta, retire=True),
        ),
    )

    assert len(instance.accepted_history()) == head + 1
    assert set(_key_claims_of(instance, beta).values()) == {"retired"}
    created = _create(instance, DECISION, "beta")
    paths = [item for item in tree(instance) if item.startswith(f"subjects/{DECISION}/beta")]
    assert beta in paths and len(paths) == 2, created
    (fresh,) = [item for item in paths if item != beta]
    assert set(_key_claims_of(instance, fresh).values()) == {"live"}


def test_a_raw_retirement_of_a_keyed_subject_retires_its_key_claims_too(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subjects import subject_digest
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from tests.test_service.test_subject_identity import _create

    _create(instance, DECISION, "gamma")
    gamma = subject_path(DECISION, "gamma")
    held = parse_subject(tree(instance)[gamma], path=gamma)
    retired = held.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_digest(held).tagged
            )
        }
    )
    raw = _submit_raw(instance, {gamma: render_subject(retired)}, "retire-gamma")
    assert raw.candidate is not None, raw.evaluation.diagnostics
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=raw.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    assert set(_key_claims_of(instance, gamma).values()) == {"retired"}


def test_a_successor_keeps_its_key_claims_and_its_key(instance: PlaybillInstance) -> None:
    from cruxible_core.indexes.subject_identity import IdentityView, tree_key_reader
    from tests.test_service.test_subject_identity import _create

    _create(instance, DECISION, "delta")
    delta = subject_path(DECISION, "delta")
    before = {path: tree(instance)[path] for path in _key_claims_of(instance, delta)}

    accept(instance, change_set(_succession(instance, delta, retire=False)))

    assert parse_subject(tree(instance)[delta], path=delta).lifecycle.predecessor_digest
    after = {path: tree(instance)[path] for path in _key_claims_of(instance, delta)}
    assert after == before  # carried unchanged
    base = instance.accepted_coordinate()
    base_tree = instance.immutable_tree_at(base.git_oid)
    with instance.bind_accepted_projection(base) as projection:
        view = IdentityView(projection.typed.connection, key_reader=tree_key_reader(base_tree))
        assert view.resolve(DECISION, "delta").path == delta
        assert (
            view.resolve_creation(
                DECISION, _decision_key(instance, "delta"), single_text="delta", create_new=False
            ).path
            == delta
        )


# E-b delta-4 F-001: the retired-with-Subject exemption is Claim law v3 revision 11.


def _retirement_candidate(instance: PlaybillInstance, slug: str) -> tuple[Any, dict[str, Any]]:
    """A keyed Subject with a title, and a raw change retiring it and its title.

    Returns the candidate's evaluation inputs and the Claim paths by role.
    """

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.claims import render_claim
    from cruxible_client.contracts.subjects import subject_digest
    from cruxible_core.claims.key_claims import retired_key_claim
    from tests.test_service.test_subject_identity import _create, write

    _create(instance, DECISION, slug)
    path = subject_path(DECISION, slug)
    titled = write(
        instance, {"op": "set", "subject": f"{DECISION}/{slug}", "field": "title", "value": "T"}
    )
    assert titled.status == "accepted", titled
    parent = tree(instance)
    held = parse_subject(parent[path], path=path)
    retired = held.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_digest(held).tagged
            )
        }
    )
    claims = {
        parse_claim(content, path=claim_file).statement.predicate.rpartition(".")[2]: claim_file
        for claim_file, content in parent.items()
        if claim_file.startswith("claims/")
        and parse_claim(content, path=claim_file).statement.subject.artifact_path == path
    }
    title = parse_claim(parent[claims["title"]], path=claims["title"])
    candidate = {
        **parent,
        path: render_subject(retired),
        # An attributed retirement of a non-key Claim, alongside its Subject.
        claims["title"]: render_claim(retired_key_claim(title)),
    }
    return (parent, candidate), claims


def _judged(
    instance: PlaybillInstance, trees: Any, coordinates: dict[str, tuple[str, str]] | None
) -> Any:
    from cruxible_core.proposals.proposals import evaluate_proposal_tree

    parent, candidate = trees
    return evaluate_proposal_tree(
        base_tree=parent,
        current_tree=parent,
        proposed_tree=candidate,
        current=instance.accepted_coordinate(),
        bodies=instance.body_store(),
        timestamp="2026-10-09T12:00:00.000000Z",
        actor_id="owner",
        rebased=False,
        historical_law_coordinates=coordinates,
    )


@pytest.mark.parametrize("role", ["slug", "title"])
def test_only_claim_law_revision_11_admits_a_claim_retired_with_its_subject(
    instance: PlaybillInstance, role: str
) -> None:
    """Differential: identical inputs; revisions 9 and 10 refuse, revision 11 admits.

    ``slug`` is the key Claim core retires with the Subject; ``title`` an ordinary
    Claim the change retires itself. Earlier revisions keep their meaning exactly.
    """

    from cruxible_client.contracts.laws import (
        CLAIM_LAW_V3_REVISION_9,
        CLAIM_LAW_V3_REVISION_10,
        CLAIM_LAW_V3_REVISION_11,
    )

    trees, claims = _retirement_candidate(instance, "epsilon")
    current = _judged(instance, trees, None)
    assert current.candidate is not None, current.diagnostics
    # Every member judged at the law it was admitted under; one Claim's law varied.
    recorded = {
        member.path: (
            member.law_identifier,
            current.candidate.law_digests[member.law_identifier],
        )
        for member in current.candidate.members
    }
    assert recorded[claims[role]][1] == CLAIM_LAW_V3_REVISION_11.digest

    def at(revision: Any) -> dict[str, tuple[str, str]]:
        return {**recorded, claims[role]: (revision.identifier, revision.digest)}

    assert _judged(instance, trees, at(CLAIM_LAW_V3_REVISION_11)).candidate is not None
    for old in (CLAIM_LAW_V3_REVISION_9, CLAIM_LAW_V3_REVISION_10):
        refused = _judged(instance, trees, at(old))
        assert refused.candidate is None
        assert {item.code for item in refused.diagnostics} == {
            "cruxible.claim.subject_context_mismatch"
        }


def test_claim_law_revision_11_is_adopted_with_subject_identity_only(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Claim judged on a compiler before revision 33 is judged by revision 10."""

    import cruxible_core.proposals.proposals as proposals_module
    from cruxible_client.contracts.claims import render_claim
    from cruxible_client.contracts.laws import CLAIM_LAW_V3_REVISION_10, CLAIM_LAW_V3_REVISION_11
    from cruxible_core.claims.key_claims import retired_key_claim

    (parent, _candidate), claims = _retirement_candidate(instance, "zeta")
    title = parse_claim(parent[claims["title"]], path=claims["title"])
    only_title = {**parent, claims["title"]: render_claim(retired_key_claim(title))}

    def law_of(trees: Any) -> str:
        judged = _judged(instance, trees, None)
        assert judged.candidate is not None, judged.diagnostics
        (member,) = [item for item in judged.candidate.members if item.path == claims["title"]]
        return str(judged.candidate.law_digests[member.law_identifier])

    assert law_of((parent, only_title)) == CLAIM_LAW_V3_REVISION_11.digest
    monkeypatch.setattr(proposals_module, "admits_subject_kinds", lambda _compiler: False)
    assert law_of((parent, only_title)) == CLAIM_LAW_V3_REVISION_10.digest


# E-b delta-4 F-002: a carried key Claim composes with later lifecycle changes.


def _reopened(instance: PlaybillInstance) -> PlaybillInstance:
    """The instance opened afresh: accepted history replays and verifies on open."""

    instance.settled_workspace_advertisement()
    return PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


@pytest.mark.parametrize("successions", [1, 2])
def test_a_carried_key_claim_retires_with_its_subject_after_successions(
    instance: PlaybillInstance, successions: int
) -> None:
    """create -> successor(s) carrying the key Claim -> retirement (authoring route)."""

    from tests.test_service.test_subject_identity import _create

    _create(instance, DECISION, "carry")
    carry = subject_path(DECISION, "carry")
    (key_path,) = _key_claims_of(instance, carry)
    accepted_key = tree(instance)[key_path]
    for _ in range(successions):
        accept(instance, change_set(_succession(instance, carry, retire=False)))
    assert tree(instance)[key_path] == accepted_key  # carried, never rewritten

    accept(instance, change_set(_succession(instance, carry, retire=True)))

    retired = parse_claim(tree(instance)[key_path], path=key_path)
    assert retired.lifecycle.state == "retired"
    original = parse_claim(accepted_key, path=key_path)
    assert retired.backing == original.backing and retired.pins == original.pins
    created = _create(instance, DECISION, "carry")  # the key is free again
    paths = [item for item in tree(instance) if item.startswith(f"subjects/{DECISION}/carry")]
    assert len(paths) == 2, created
    head = instance.accepted_coordinate()
    assert _reopened(instance).accepted_coordinate() == head


def test_a_merge_after_a_carry_retires_the_key_claim_on_the_raw_route(
    instance: PlaybillInstance,
) -> None:
    """Raw admission, readmission at a moved head, settlement and replay."""

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subjects import subject_digest
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from tests.test_service.test_subject_identity import _create

    _create(instance, DECISION, "keeper")
    _create(instance, DECISION, "merged")
    merged = subject_path(DECISION, "merged")
    (key_path,) = _key_claims_of(instance, merged)
    accept(instance, change_set(_succession(instance, merged, retire=False)))
    base = instance.accepted_coordinate()
    parent = tree(instance)
    held = parse_subject(parent[merged], path=merged)
    retired = held.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_digest(held).tagged
            )
        }
    )
    # The head moves on before the raw merge arrives: it is readmitted there.
    accept(instance, change_set(_alias_member_of("keeper", "Merged Inc")))
    raw = instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/merge-after-carry", proposed_base_oid=base.git_oid
        ),
        candidate_tree={**parent, merged: render_subject(retired)},
        timestamp="2026-10-09T12:00:00.000000Z",
    )
    assert raw.candidate is not None, raw.evaluation.diagnostics
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=raw.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    assert _key_claims_of(instance, merged) == {key_path: "retired"}
    head = instance.accepted_coordinate()
    assert _reopened(instance).accepted_coordinate() == head


def _alias_member_of(subject_id: str, value: str) -> Any:
    from tests.test_service.test_subject_identity import _alias_member

    return _alias_member(DECISION, subject_id, value)


def test_an_explicit_key_claim_retirement_after_a_carry_is_admitted(
    instance: PlaybillInstance,
) -> None:
    from tests.test_service.test_subject_identity import _create, write

    _create(instance, DECISION, "explicit")
    explicit = subject_path(DECISION, "explicit")
    (key_path,) = _key_claims_of(instance, explicit)
    accept(instance, change_set(_succession(instance, explicit, retire=False)))
    claim_id = key_path.rsplit("/", 1)[1].removesuffix(".json")

    outcome = write(instance, {"op": "retire", "target": claim_id})

    assert outcome.status == "accepted", outcome
    assert _key_claims_of(instance, explicit) == {key_path: "retired"}
    head = instance.accepted_coordinate()
    assert _reopened(instance).accepted_coordinate() == head


# E-b delta-4 F-003: only Claim dependents are exempt; others close as usual.


@pytest.mark.parametrize("retire", [False, True])
def test_a_non_claim_subject_pin_still_closes_over_a_succession(
    instance: PlaybillInstance, retire: bool
) -> None:
    """A v7 ClaimType pinning a keyed Subject (role ``subject``) is dispositioned by the
    change, as before: an ordinary incomplete-closure refusal, never a parse error."""

    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.authoring.inputs import ClaimTypeInput
    from cruxible_client.contracts.subjects import subject_digest
    from tests.test_service.test_subject_identity import _create, _field

    _create(instance, DECISION, "pinned")
    pinned = subject_path(DECISION, "pinned")
    held = parse_subject(tree(instance)[pinned], path=pinned)
    notes = _field(DECISION, "notes").model_copy(
        update={
            "pins": (
                ArtifactPin(
                    role="subject",
                    target=held.identity,
                    artifact_digest=subject_digest(held).tagged,
                ),
            )
        }
    )
    assert notes.artifact_format == "playbill-claim-type-v7"
    accept(instance, change_set(ClaimTypeInput(kind="claim_type", claim_type=notes)))

    refused = compile_(instance, change_set(_succession(instance, pinned, retire=retire)))

    assert refused.verdict == "refused"
    assert "cruxible.change_set.incomplete_closure" in _codes(refused)
