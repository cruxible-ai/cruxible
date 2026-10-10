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
    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(members, key=lambda item: authoring_member_identity(item).encode("utf-8"))
        )
    )
    _intent, computed = AuthoringIntentCoordinator.for_instance(instance).preview(
        actor=OWNER, payload=payload, canonical_timestamp="2026-10-09T12:00:00.000000Z"
    )
    return computed


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
    computed = _preview(
        instance,
        SubjectCreationAuthoringPayload(subject_kind=NOTE, key_value="Twin", create_new=True),
    )
    (path,) = _subjects(computed)
    changed = dict(computed.lowered.changed_members)
    held = parse_subject(changed[path], path=path)
    assert held.assignment is not None and held.assignment.rule == "further_holder"
    parent = tree(instance)
    honest = candidate_identity_issues(
        current_tree=parent, candidate_tree={**parent, **changed}, scope=tuple(changed)
    )
    assert honest == ()
    tampered = held.model_copy(
        update={
            "assignment": SubjectAssignment(rule="further_holder", operation="AIT-x", member="m")
        }
    )
    issues = candidate_identity_issues(
        current_tree=parent,
        candidate_tree={**parent, **changed, path: render_subject(tampered)},
        scope=tuple(changed),
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
        SUBJECT_IDENTITY_COMPILER,
        artifact_kinds_for_compiler,
        current_compiler_coordinate,
        projection_registry_for_compiler,
    )

    assert SUBJECT_IDENTITY_COMPILER != GOVERNED_TRIGGERS_COMPILER
    assert current_compiler_coordinate() == SUBJECT_IDENTITY_COMPILER
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
