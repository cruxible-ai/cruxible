"""Subject references resolve through semantic.alias at every entry point (D3).

A reference ``kind/id`` that names no Subject resolves to the one live Subject
of its kind an alias names; an alias naming several refuses with each; none
leaves the entry point's own rule in force.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.builtin_claim_types import (
    SEMANTIC_ALIAS_CLAIM_TYPE,
    builtin_claim_type_files,
)
from cruxible_client.contracts.claim_types import (
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_client.contracts.line_inbox import InboxLinkSubject
from cruxible_client.contracts.subjects import subject_path
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.indexes.subject_aliases import resolve_subject_ref
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.query_values import read_live_values
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.core_support._identity_support import KIND, OWNER, seed_identity_surface

ALPHA = f"{KIND}/alpha"
BETA = f"{KIND}/beta"


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return seed_identity_surface(tmp_path)


def write(instance: PlaybillInstance, *changes: dict[str, Any]) -> WriteOutcome:
    request = WriteRequest.model_validate({"because": "Known as.", "changes": list(changes)})
    return service_playbill_write(
        instance,
        request=request,
        caller=WriteCaller(actor=OWNER, may_activate=True, may_approve=True),
    )


def alias(instance: PlaybillInstance, subject: str, name: str) -> None:
    outcome = write(
        instance, {"op": "add", "subject": subject, "field": "semantic.alias", "value": name}
    )
    assert outcome.status == "accepted", outcome


def values(instance: PlaybillInstance, subject: str, predicate: str) -> list[str]:
    kind, subject_id = subject.split("/", 1)
    return sorted(
        str(item.value)
        for item in read_live_values(
            instance,
            instance.accepted_coordinate(),
            subject_paths=(subject_path(kind, subject_id),),
            predicates=(predicate,),
        )
    )


def test_a_new_instance_seeds_the_builtin_claim_types(instance: PlaybillInstance) -> None:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    # The genesis seeds are the builtin ClaimTypes' exact bytes.
    current = builtin_claim_type_files()
    assert current == {
        path: tree[path]
        for path in ("claim-types/semantic/alias.json", "claim-types/semantic/distinct_from.json")
    }
    assert tree[claim_type_path("semantic.alias")] == render_claim_type(SEMANTIC_ALIAS_CLAIM_TYPE)


def test_an_alias_is_stored_canonical_and_resolves_a_write_subject(
    instance: PlaybillInstance,
) -> None:
    alias(instance, ALPHA, "  Lodash-JS ")
    assert values(instance, ALPHA, "semantic.alias") == ["lodash-js"]
    outcome = write(
        instance, {"op": "set", "subject": f"{KIND}/lodash-js", "field": "code", "value": "x"}
    )
    assert outcome.status == "accepted", outcome
    assert outcome.subjects_added == ()
    assert values(instance, ALPHA, f"{KIND}.code") == ["x"]
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        resolution = resolve_subject_ref(projection.typed.connection, KIND, "lodash-js")
    assert (resolution.via, resolution.ref) == ("alias", ALPHA)


def test_a_subject_valued_object_and_its_expectation_resolve_through_an_alias(
    instance: PlaybillInstance,
) -> None:
    alias(instance, BETA, "b-pkg")
    outcome = write(
        instance, {"op": "add", "subject": ALPHA, "field": "depends_on", "value": f"{KIND}/b-pkg"}
    )
    assert outcome.status == "accepted", outcome
    assert values(instance, ALPHA, f"{KIND}.depends_on") == [BETA]


def test_an_alias_naming_several_subjects_refuses_with_each(instance: PlaybillInstance) -> None:
    alias(instance, ALPHA, "shared")
    alias(instance, BETA, "shared")
    refused = write(
        instance, {"op": "set", "subject": f"{KIND}/shared", "field": "code", "value": "x"}
    )
    assert refused.refusal is not None
    assert refused.refusal.code == "cruxible.write.alias_ambiguous"
    assert set(refused.refusal.candidates) == {ALPHA, BETA}
    with pytest.raises(ReadRefusalError) as query_refused:
        service_playbill_query(
            instance,
            request=QueryRequest.model_validate(
                {"kind": KIND, "where": [{"field": "subject_id", "eq": "shared"}]}
            ),
        )
    assert query_refused.value.error_code == "cruxible.query.alias_ambiguous"


def test_a_compact_query_reference_resolves_through_an_alias(instance: PlaybillInstance) -> None:
    alias(instance, BETA, "b-pkg")
    assert (
        write(
            instance, {"op": "add", "subject": ALPHA, "field": "depends_on", "value": BETA}
        ).status
        == "accepted"
    )

    def ids(where: list[dict[str, object]]) -> list[str]:
        result = service_playbill_query(
            instance, request=QueryRequest.model_validate({"kind": KIND, "where": where})
        )
        return [row["subject_id"] for row in result.rows]

    assert ids([{"field": "subject_id", "eq": "b-pkg"}]) == ["beta"]
    assert ids([{"field": "depends_on", "eq": f"{KIND}/b-pkg"}]) == ["alpha"]


def test_an_inbox_link_names_its_subject_through_an_alias(instance: PlaybillInstance) -> None:
    from cruxible_core.service.procedures.line_inbox import _existing_target

    alias(instance, BETA, "b-pkg")
    assert _existing_target(instance, InboxLinkSubject(subject=f"{KIND}/b-pkg")) == (
        InboxLinkSubject(subject=BETA)
    )


def test_vocabulary_terms_follow_alias_claims_incrementally(instance: PlaybillInstance) -> None:
    alias(instance, ALPHA, "a-one")
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        rows = projection.typed.connection.execute(
            "SELECT target_path, term FROM vocabulary_terms WHERE basis='alias'"
        ).fetchall()
    assert [tuple(row) for row in rows] == [(subject_path(KIND, "alpha"), "a-one")]


def test_a_change_set_claim_names_its_subject_through_an_alias(
    instance: PlaybillInstance,
) -> None:
    import base64

    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        ChangeSetAuthoringPayload,
        ClaimAuthoringPayload,
        ClaimDependencyDrafts,
        SelfSourceBody,
    )
    from cruxible_client.contracts.claims import LiteralClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    alias(instance, ALPHA, "a-one")
    member = ClaimAuthoringPayload(
        statement=AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_path(KIND, "a-one")),
            predicate=f"{KIND}.plain",
            object=LiteralClaimObject(value="by alias"),
            role="observation",
        ),
        rationale="Authored by hand.",
        source=SelfSourceBody(content_base64=base64.b64encode(b"by alias").decode()),
        dependency_drafts=ClaimDependencyDrafts(),
    )
    submitted = AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=OWNER,
        payload=ChangeSetAuthoringPayload(members=(member,)),
        canonical_timestamp="2026-10-09T12:00:00.000000Z",
    )
    assert submitted.status.proposal_id is not None, submitted
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    assert values(instance, ALPHA, f"{KIND}.plain") == ["by alias"]


def test_a_query_parameter_names_its_subject_through_an_alias(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.query.definitions import (
        AcceptedQueryDefinition,
        QueryDefinition,
        QueryEvaluationPolicy,
    )
    from cruxible_client.contracts.query.grammar import (
        QueryBudgets,
        QueryEntry,
        QueryParameterDeclaration,
        QueryParameterRef,
    )
    from cruxible_core.service.discovery.query import parameters_through_aliases

    alias(instance, BETA, "b-pkg")
    definition = QueryDefinition(
        identity=ArtifactIdentity(kind="QueryDefinition", name="inv.one_package"),
        entry=QueryEntry(
            binding="item", subject_kinds=(KIND,), subject_id=QueryParameterRef(parameter="id")
        ),
        parameters=(
            QueryParameterDeclaration(name="id", value_type="string"),
            QueryParameterDeclaration(name="other", value_type="subject_reference"),
        ),
        result_binding="item",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        evaluation_policy=QueryEvaluationPolicy(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        ),
        default_budgets=QueryBudgets(max_results=10, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=50, max_traversal_depth=0),
    )
    accepted = AcceptedQueryDefinition.model_construct(
        path="queries/inv/one_package.json", query=definition, artifact_digest="sha256:" + "0" * 64
    )
    resolved = parameters_through_aliases(
        instance,
        accepted,
        coordinate=instance.accepted_coordinate(),
        parameters={"id": "b-pkg", "other": f"Subject:{KIND}/b-pkg"},
    )
    assert resolved == {"id": "beta", "other": f"Subject:{BETA}"}


def test_the_builtin_distinct_from_relates_subjects_of_any_kind(
    instance: PlaybillInstance,
) -> None:
    """The descriptor relation admits semantic.subject only, and lowering agrees with the law."""

    from cruxible_client.contracts.builtin_claim_types import SEMANTIC_DISTINCT_FROM_CLAIM_TYPE

    assert SEMANTIC_DISTINCT_FROM_CLAIM_TYPE.allowed_object_subject_kinds == ("semantic.subject",)
    write(
        instance,
        {"op": "add", "subject": ALPHA, "field": "semantic.distinct_from", "value": BETA},
    )
    assert values(instance, ALPHA, "semantic.distinct_from") == [BETA]
    claim_path_value, _bytes = _lowered_claim(instance, _distinct_member("beta", "alpha"))
    assert claim_path_value.startswith("claims/")


# -- creation collision (item 4) -------------------------------------------------


def _subject_member(subject_id: str) -> Any:
    from cruxible_client.contracts.authoring.models import SubjectAuthoringPayload
    from tests.core_support._identity_support import shell

    return SubjectAuthoringPayload(subject=shell(subject_id))


def _compile(instance: PlaybillInstance, *members: Any) -> Any:
    from cruxible_client.contracts.authoring.models import (
        ChangeSetAuthoringPayload,
        authoring_member_identity,
    )
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator

    payload = ChangeSetAuthoringPayload.model_validate(
        {
            "members": sorted(
                members, key=lambda item: authoring_member_identity(item).encode("utf-8")
            )
        }
    )
    return AuthoringIntentCoordinator.for_instance(instance).compile(
        actor=OWNER, payload=payload, canonical_timestamp="2026-10-09T12:00:00.000000Z"
    )


def test_an_authored_subject_never_carries_a_chosen_id(
    instance: PlaybillInstance,
) -> None:
    """Identity D1 supersedes the authoring-side collision probe: core names new Subjects.

    An authored shell for a Subject that does not exist refuses before any
    collision could be judged; the token and alias collision law still holds at
    the boundary for minted and grandfathered kinds (raw proposals below).
    """

    alias(instance, ALPHA, "a-one")
    for name in ("a-one", "fresh"):
        assert _codes(_compile(instance, _subject_member(name))) == [
            "cruxible.authoring.subject_id_not_assigned"
        ]


def _lowered_claim(instance: PlaybillInstance, member: Any) -> tuple[str, bytes]:
    """One Claim member's lowered path and bytes at the head (for a boundary probe)."""

    from cruxible_client.contracts.authoring.models import ChangeSetAuthoringPayload
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import lower_authoring

    intent = (
        AuthoringIntentCoordinator.for_instance(instance)
        .create(
            actor=OWNER,
            payload=ChangeSetAuthoringPayload(members=(member,)),
            canonical_timestamp="2026-10-09T12:00:00.000000Z",
        )
        .intent
    )
    lowered = lower_authoring(instance, intent=intent, actor_id="owner")
    (found,) = [item for item in lowered.changed_members if item[0].startswith("claims/")]
    return found


def _exemption_world(instance: PlaybillInstance) -> tuple[dict[str, bytes], str, str, bytes]:
    """A parent without ``a-one``, the tree with it, and its distinct_from alpha Claim.

    ``a-one`` lands first (no alias names it yet), so the Claim lowers against
    it; aliases naming ``a-one`` follow. Removing ``a-one`` from the head gives
    the parent a raw creation of it would meet.
    """

    from cruxible_client.contracts.subjects import render_subject
    from tests.core_support._identity_support import accept_tree, shell

    _accept_distinct_from(instance)
    accept_tree(instance, {subject_path(KIND, "a-one"): render_subject(shell("a-one"))}, name="a1")
    claim_path_value, claim_bytes = _lowered_claim(instance, _distinct_member("a-one", "alpha"))
    alias(instance, ALPHA, "a-one")
    tree = _undeclared(instance.tree_at(instance.accepted_coordinate().git_oid))
    created = subject_path(KIND, "a-one")
    parent = {path: content for path, content in tree.items() if path != created}
    return parent, created, claim_path_value, claim_bytes


def _undeclared(tree: Any) -> dict[str, bytes]:
    """``tree`` without its packages' SubjectKind: E-a's creation-collision rule judges
    a kind no key names (minted, or grandfathered); a keyed kind is held to its key."""

    from cruxible_client.contracts.subject_kinds import subject_kind_path

    return {path: content for path, content in tree.items() if path != subject_kind_path(KIND)}


def _accept_distinct_from(instance: PlaybillInstance) -> None:
    """A new instance seeds the builtin semantic.distinct_from: nothing to accept."""

    assert claim_type_path("semantic.distinct_from") in instance.tree_at(
        instance.accepted_coordinate().git_oid
    )


def _distinct_member(subject_id: str, other: str) -> Any:
    import base64

    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        ClaimAuthoringPayload,
        ClaimDependencyDrafts,
        SelfSourceBody,
    )
    from cruxible_client.contracts.claims import SubjectClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress

    return ClaimAuthoringPayload(
        statement=AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_path(KIND, subject_id)),
            predicate="semantic.distinct_from",
            object=SubjectClaimObject(
                address=SemanticAddress.whole_artifact(subject_path(KIND, other))
            ),
            role="observation",
        ),
        rationale="A different package that shares the name.",
        source=SelfSourceBody(content_base64=base64.b64encode(b"different").decode()),
        dependency_drafts=ClaimDependencyDrafts(),
    )


def test_asserting_distinct_from_in_the_same_change_exempts_the_pair(
    instance: PlaybillInstance,
) -> None:
    from cruxible_core.indexes.subject_aliases import candidate_creation_collisions

    parent, created, claim_path_value, claim_bytes = _exemption_world(instance)
    candidate = {
        **parent,
        created: instance.tree_at(instance.accepted_coordinate().git_oid)[created],
    }
    assert candidate_creation_collisions(
        current_tree=parent, candidate_tree=candidate, scope=(created,)
    ) == ((created, (ALPHA,)),)
    assert (
        candidate_creation_collisions(
            current_tree=parent,
            candidate_tree={**candidate, claim_path_value: claim_bytes},
            scope=(claim_path_value, created),
        )
        == ()
    )


# -- the authoritative creation boundary (review F-001, F-002) --------------------


def _alias_member(subject_id: str, name: str) -> Any:
    import base64

    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        ClaimAuthoringPayload,
        ClaimDependencyDrafts,
        SelfSourceBody,
    )
    from cruxible_client.contracts.claims import LiteralClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress

    return ClaimAuthoringPayload(
        statement=AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_path(KIND, subject_id)),
            predicate="semantic.alias",
            object=LiteralClaimObject(value=name),
            role="observation",
        ),
        rationale="Alias added in this change.",
        source=SelfSourceBody(content_base64=base64.b64encode(b"alias").decode()),
        dependency_drafts=ClaimDependencyDrafts(),
    )


def _submit_raw(instance: PlaybillInstance, updates: dict[str, bytes], name: str) -> Any:
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    base = instance.accepted_coordinate()
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree={**instance.tree_at(base.git_oid), **updates},
        timestamp="2026-10-09T12:00:00.000000Z",
    )


def _codes(result: Any) -> list[str]:
    return [item.code for item in result.frontier.diagnostics]


def _previewed_subjects(instance: PlaybillInstance, *members: Any) -> list[str]:
    """The Subject paths a change would write, lowered as submit would (nothing stored)."""

    from cruxible_client.contracts.authoring.models import (
        ChangeSetAuthoringPayload,
        authoring_member_identity,
    )
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator

    payload = ChangeSetAuthoringPayload.model_validate(
        {
            "members": sorted(
                members, key=lambda item: authoring_member_identity(item).encode("utf-8")
            )
        }
    )
    _intent, computed = AuthoringIntentCoordinator.for_instance(instance).preview(
        actor=OWNER, payload=payload, canonical_timestamp="2026-10-09T12:00:00.000000Z"
    )
    assert computed.lowered is not None, computed.result.frontier
    return sorted(
        path for path, _content in computed.lowered.changed_members if path.startswith("subjects/")
    )


def test_a_creation_by_a_name_an_alias_holds_names_that_subject(
    instance: PlaybillInstance,
) -> None:
    """Review F-003/F-007: an alias, added in the same change or before, names its Subject.

    Creating by key a text an alias of the kind holds resolves to the aliased
    Subject (the shared resolver: alias before key), so no second Subject
    shadows the name -- whether the alias is accepted or added by the change.
    """

    from cruxible_client.contracts.authoring.models import SubjectCreationAuthoringPayload

    fresh = SubjectCreationAuthoringPayload(subject_kind=KIND, key_value="fresh")
    assert _previewed_subjects(instance, _alias_member("alpha", "fresh"), fresh) == []
    alias(instance, ALPHA, "other")
    other = SubjectCreationAuthoringPayload(subject_kind=KIND, key_value="other")
    assert _previewed_subjects(instance, other) == []
    unrelated = SubjectCreationAuthoringPayload(subject_kind=KIND, key_value="unrelated")
    assert _previewed_subjects(instance, unrelated) == [subject_path(KIND, "unrelated")]


def test_a_new_id_never_shadows_an_alias(instance: PlaybillInstance) -> None:
    """Review F-003: id assignment reserves alias names; the boundary refuses one that does not.

    A different key whose rendering is a live alias of the kind takes the
    key-digest suffix; a raw creation built before the alias landed (or at a
    head that moved) carries the plain rendering and is refused at admission
    and on readmission.
    """

    from cruxible_client.contracts.errors import ProposalStale
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from cruxible_core.service.proposals.proposals import service_readmit_playbill_proposal
    from tests.core_support._subject_kind_support import core_assigned

    built = core_assigned(instance, {subject_path(KIND, "b-two"): _shell_bytes("b-two")})
    pending = _submit_raw(instance, dict(built), "pending-b-two")
    assert pending.candidate is not None, pending.evaluation
    alias(instance, ALPHA, "b-two")
    with pytest.raises(ProposalStale):
        service_activate_playbill_proposal(
            instance, proposal_id=pending.admission.proposal_id, activated_by="owner"
        )
    readmitted = service_readmit_playbill_proposal(
        instance, proposal_id=pending.admission.proposal_id, actor_id="owner", dry_run=False
    )
    assert "cruxible.subject.id_not_assigned" in readmitted.model_dump_json()
    raw = _submit_raw(instance, dict(built), "raw-b-two")
    assert raw.candidate is None
    assert {item.code for item in raw.evaluation.diagnostics} == {
        "cruxible.subject.id_not_assigned"
    }
    # A write naming kind/b-two reaches alpha through its alias.
    write(instance, {"op": "set", "subject": f"{KIND}/b-two", "field": "code", "value": "z"})
    assert values(instance, ALPHA, f"{KIND}.code") == ["z"]


def _shell_bytes(subject_id: str) -> bytes:
    from cruxible_client.contracts.subjects import render_subject
    from tests.core_support._identity_support import shell

    return render_subject(shell(subject_id))


def test_a_claim_subject_draft_is_held_to_the_boundary(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.authoring.models import ClaimDependencyDrafts
    from tests.core_support._identity_support import shell

    alias(instance, ALPHA, "a-one")
    drafted = _alias_member("a-one", "other").model_copy(
        update={"dependency_drafts": ClaimDependencyDrafts(subject=shell("a-one"))}
    )
    result = _compile(instance, drafted)
    # A draft never creates its Subject (D1), so it never reaches a collision.
    assert _codes(result) == ["cruxible.authoring.subject_id_not_assigned"], _codes(result)


@pytest.mark.parametrize("alias_first", [True, False])
def test_a_write_adding_and_using_an_alias_together_reaches_its_live_subject(
    instance: PlaybillInstance,
    alias_first: bool,
) -> None:
    """Identity D3 and set-history-build-1015: a held alias lands on its live Subject."""

    changes = [
        {"op": "add", "subject": ALPHA, "field": "semantic.alias", "value": "fresh"},
        {"op": "set", "subject": f"{KIND}/fresh", "field": "code", "value": "x"},
    ]
    outcome = write(instance, *(changes if alias_first else reversed(changes)))
    assert outcome.status == "accepted", outcome
    assert outcome.subjects_added == ()
    assert [change.subject for change in outcome.changes] == [ALPHA, ALPHA]
    assert values(instance, ALPHA, "semantic.alias") == ["fresh"]
    assert values(instance, ALPHA, f"{KIND}.code") == ["x"]
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert subject_path(KIND, "fresh") not in tree


def test_a_distinct_from_exemption_covers_only_its_own_pair(instance: PlaybillInstance) -> None:
    from cruxible_core.indexes.subject_aliases import candidate_creation_collisions

    parent, created, claim_path_value, claim_bytes = _exemption_world(instance)
    alias(instance, BETA, "a-one")
    tree = _undeclared(instance.tree_at(instance.accepted_coordinate().git_oid))
    parent = {path: content for path, content in tree.items() if path != created}
    # a-one meets alpha and beta; asserting it distinct from alpha leaves beta.
    assert candidate_creation_collisions(
        current_tree=parent,
        candidate_tree={**tree, claim_path_value: claim_bytes},
        scope=(claim_path_value, created),
    ) == ((created, (BETA,)),)


def test_a_retirement_in_the_same_change_lifts_the_collision(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subjects import parse_subject, render_subject, subject_digest
    from cruxible_core.indexes.subject_aliases import candidate_creation_collisions
    from tests.core_support._identity_support import shell

    alias(instance, ALPHA, "a-one")
    oid = instance.accepted_coordinate().git_oid
    plain = _undeclared(instance.tree_at(oid))
    base = plain
    created = subject_path(KIND, "a-one")
    alpha_path = subject_path(KIND, "alpha")
    alpha = parse_subject(plain[alpha_path], path=alpha_path)
    retired = alpha.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_digest(alpha).tagged
            )
        }
    )
    candidate = {
        **plain,
        created: render_subject(shell("a-one")),
        alpha_path: render_subject(retired),
    }
    for parent in (base, plain):  # the projection and the cold derivation agree
        assert (
            candidate_creation_collisions(
                current_tree=parent, candidate_tree=candidate, scope=(alpha_path, created)
            )
            == ()
        )
        assert candidate_creation_collisions(
            current_tree=parent,
            candidate_tree={**plain, created: render_subject(shell("a-one"))},
            scope=(created,),
        ) == ((created, (ALPHA,)),)


def test_a_reference_to_a_merged_subject_reaches_the_survivor(
    instance: PlaybillInstance,
) -> None:
    """D5: exact live path, then alias (live target), then exact retired path."""

    from cruxible_client.contracts.subjects import render_subject
    from tests.core_support._identity_support import accept_tree, retire_subject

    def resolved(subject_id: str) -> tuple[str | None, str | None]:
        with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
            found = resolve_subject_ref(projection.typed.connection, KIND, subject_id)
        return found.ref, found.via

    # Merge alpha into beta: alias beta with alpha's id, then retire alpha.
    alias(instance, BETA, "alpha")
    # While alpha is live, its exact path wins over beta's alias.
    assert resolved("alpha") == (ALPHA, "identity")
    alpha_path = subject_path(KIND, "alpha")
    retire_subject(instance, alpha_path, name="retire-alpha")

    assert resolved("alpha") == (BETA, "alias")
    outcome = write(instance, {"op": "set", "subject": ALPHA, "field": "code", "value": "x"})
    assert outcome.status == "accepted", outcome
    assert values(instance, BETA, f"{KIND}.code") == ["x"]
    # A retired Subject no alias supersedes is still the one it names.
    gamma_path = subject_path(KIND, "gamma")
    from tests.core_support._identity_support import shell

    accept_tree(instance, {gamma_path: render_subject(shell("gamma"))}, name="gamma")
    retire_subject(instance, gamma_path, name="retire-gamma")
    assert resolved("gamma") == (f"{KIND}/gamma", "identity")
