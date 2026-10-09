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
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
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
        instance, request=request, caller=WriteCaller(actor=OWNER, may_activate=True)
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


def test_a_new_instance_seeds_the_builtin_alias_claim_type(instance: PlaybillInstance) -> None:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert builtin_claim_type_files() == {
        "claim-types/semantic/alias.json": tree["claim-types/semantic/alias.json"]
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
        ClaimAuthoringPayloadV2,
        ClaimDependencyDrafts,
        SelfSourceBody,
    )
    from cruxible_client.contracts.claims import LiteralClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    alias(instance, ALPHA, "a-one")
    member = ClaimAuthoringPayloadV2(
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


def test_an_instance_created_before_the_builtin_adopts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cruxible_core.runtime.instance as instance_module
    from cruxible_core.ledger.bootstrap import genesis_seed_set
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from cruxible_core.service.claims.claim_types import service_propose_playbill_claim_type
    from tests.core_support._support import initialize_local

    older = genesis_seed_set("triggers-3-workspace-file")
    monkeypatch.setattr(instance_module, "current_genesis_seed_set", lambda _kinds: older)
    instance, _owner = initialize_local(tmp_path)
    monkeypatch.undo()
    assert claim_type_path("semantic.alias") not in instance.tree_at(
        instance.accepted_coordinate().git_oid
    )
    inspection = service_propose_playbill_claim_type(
        instance,
        claim_type=SEMANTIC_ALIAS_CLAIM_TYPE,
        actor_id="owner",
        proposal_name="builtin-semantic-alias",
        timestamp="2026-10-09T12:00:00.000000Z",
    )
    proposal_id = inspection.proposal.admission.proposal_id  # type: ignore[union-attr]
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert tree[claim_type_path("semantic.alias")] == render_claim_type(SEMANTIC_ALIAS_CLAIM_TYPE)


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


def test_a_new_subject_colliding_with_an_alias_of_its_kind_refuses(
    instance: PlaybillInstance,
) -> None:
    alias(instance, ALPHA, "a-one")
    result = _compile(instance, _subject_member("a-one"))
    (diagnostic,) = [
        item
        for item in result.frontier.diagnostics
        if item.code == "cruxible.authoring.subject_collision"
    ]
    assert ALPHA in diagnostic.message
    # A name no Subject of the kind carries is created as before.
    assert not [
        item
        for item in _compile(instance, _subject_member("fresh")).frontier.diagnostics
        if item.code == "cruxible.authoring.subject_collision"
    ]


def _accept_distinct_from(instance: PlaybillInstance) -> None:
    from tests.core_support._identity_support import accept_tree, identity_claim_type

    distinct = identity_claim_type(
        "distinct_from",
        canonical_form=None,
        object_kind="subject",
        cardinality="many",
        # Lowering checks the object's own kind and the Claim law the descriptor
        # pseudo-kind, so a descriptor relation names both.
        object_kinds=(KIND, "semantic.subject"),
        predicate="semantic.distinct_from",
        subject_kinds=("semantic.subject",),
    )
    accept_tree(
        instance,
        {claim_type_path(distinct.predicate): render_claim_type(distinct)},
        name="distinct-from",
    )


def _distinct_member(subject_id: str, other: str) -> Any:
    import base64

    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        ClaimAuthoringPayloadV2,
        ClaimDependencyDrafts,
        SelfSourceBody,
    )
    from cruxible_client.contracts.claims import SubjectClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress

    return ClaimAuthoringPayloadV2(
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
    _accept_distinct_from(instance)
    alias(instance, ALPHA, "a-one")
    result = _compile(instance, _subject_member("a-one"), _distinct_member("a-one", "alpha"))
    codes = [item.code for item in result.frontier.diagnostics]
    assert "cruxible.authoring.subject_collision" not in codes, codes
    assert result.verdict == "passed", codes


# -- the authoritative creation boundary (review F-001, F-002) --------------------


def _alias_member(subject_id: str, name: str) -> Any:
    import base64

    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        ClaimAuthoringPayloadV2,
        ClaimDependencyDrafts,
        SelfSourceBody,
    )
    from cruxible_client.contracts.claims import LiteralClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress

    return ClaimAuthoringPayloadV2(
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


def test_same_change_alias_creation_collision_refuses(instance: PlaybillInstance) -> None:
    """Review F-002 probe: a new Subject and an alias naming it, in one change."""

    result = _compile(instance, _subject_member("fresh"), _alias_member("alpha", "fresh"))
    assert result.verdict == "refused", result
    assert "cruxible.authoring.subject_collision" in _codes(result)


def test_raw_proposal_creation_collision_refuses_at_receive(instance: PlaybillInstance) -> None:
    """Review F-001 probe: an ordinary proposal is held to the same boundary."""

    from cruxible_client.contracts.subjects import render_subject
    from tests.core_support._identity_support import shell

    alias(instance, ALPHA, "a-one")
    submitted = _submit_raw(
        instance, {subject_path(KIND, "a-one"): render_subject(shell("a-one"))}, "raw-collision"
    )
    assert submitted.candidate is None
    (diagnostic,) = submitted.evaluation.diagnostics
    assert diagnostic.code == "cruxible.subject.creation_collision"
    assert ALPHA in diagnostic.message


def test_a_moved_head_rechecks_creation_against_the_new_parent(
    instance: PlaybillInstance,
) -> None:
    """Admitted without collision, then the head names it: it never lands."""

    from cruxible_client.contracts.errors import ProposalStale
    from cruxible_client.contracts.subjects import render_subject
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from cruxible_core.service.proposals.proposals import service_readmit_playbill_proposal
    from tests.core_support._identity_support import shell

    submitted = _submit_raw(
        instance, {subject_path(KIND, "b-two"): render_subject(shell("b-two"))}, "pending-b-two"
    )
    assert submitted.candidate is not None, submitted.evaluation
    # The head moves: alpha is now also known as b-two.
    alias(instance, ALPHA, "b-two")
    with pytest.raises(ProposalStale):
        service_activate_playbill_proposal(
            instance, proposal_id=submitted.admission.proposal_id, activated_by="owner"
        )
    readmitted = service_readmit_playbill_proposal(
        instance, proposal_id=submitted.admission.proposal_id, actor_id="owner", dry_run=False
    )
    dumped = readmitted.model_dump_json()
    assert "cruxible.subject.creation_collision" in dumped, dumped
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert subject_path(KIND, "b-two") not in tree


def test_a_claim_subject_draft_is_held_to_the_boundary(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.authoring.models import ClaimDependencyDrafts
    from tests.core_support._identity_support import shell

    alias(instance, ALPHA, "a-one")
    drafted = _alias_member("a-one", "other").model_copy(
        update={"dependency_drafts": ClaimDependencyDrafts(subject=shell("a-one"))}
    )
    result = _compile(instance, drafted)
    assert "cruxible.authoring.subject_collision" in _codes(result), _codes(result)


def test_a_write_adding_an_alias_and_its_subject_together_refuses(
    instance: PlaybillInstance,
) -> None:
    outcome = write(
        instance,
        {"op": "add", "subject": ALPHA, "field": "semantic.alias", "value": "fresh"},
        {"op": "set", "subject": f"{KIND}/fresh", "field": "code", "value": "x"},
    )
    assert outcome.status != "accepted", outcome
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert subject_path(KIND, "fresh") not in tree


def test_a_distinct_from_exemption_covers_only_its_own_pair(instance: PlaybillInstance) -> None:
    _accept_distinct_from(instance)
    alias(instance, ALPHA, "a-one")
    alias(instance, BETA, "a-one")
    # a-one meets alpha and beta; asserting it distinct from alpha leaves beta.
    result = _compile(instance, _subject_member("a-one"), _distinct_member("a-one", "alpha"))
    (diagnostic,) = [
        item
        for item in result.frontier.diagnostics
        if item.code == "cruxible.authoring.subject_collision"
    ]
    assert BETA in diagnostic.message and ALPHA not in diagnostic.message


def test_a_retirement_in_the_same_change_lifts_the_collision(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subjects import parse_subject, render_subject, subject_digest
    from cruxible_core.indexes.subject_aliases import candidate_creation_collisions
    from tests.core_support._identity_support import shell

    alias(instance, ALPHA, "a-one")
    oid = instance.accepted_coordinate().git_oid
    base = instance.immutable_tree_at(oid)
    plain = instance.tree_at(oid)
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

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subjects import parse_subject, render_subject, subject_digest
    from tests.core_support._identity_support import accept_tree

    def resolved(subject_id: str) -> tuple[str | None, str | None]:
        with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
            found = resolve_subject_ref(projection.typed.connection, KIND, subject_id)
        return found.ref, found.via

    # Merge alpha into beta: alias beta with alpha's id, then retire alpha.
    alias(instance, BETA, "alpha")
    # While alpha is live, its exact path wins over beta's alias.
    assert resolved("alpha") == (ALPHA, "identity")
    alpha_path = subject_path(KIND, "alpha")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    alpha = parse_subject(tree[alpha_path], path=alpha_path)
    retired = alpha.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_digest(alpha).tagged
            )
        }
    )
    accept_tree(instance, {alpha_path: render_subject(retired)}, name="retire-alpha")

    assert resolved("alpha") == (BETA, "alias")
    outcome = write(instance, {"op": "set", "subject": ALPHA, "field": "code", "value": "x"})
    assert outcome.status == "accepted", outcome
    assert values(instance, BETA, f"{KIND}.code") == ["x"]
    # A retired Subject no alias supersedes is still the one it names.
    gamma_path = subject_path(KIND, "gamma")
    from tests.core_support._identity_support import shell

    accept_tree(instance, {gamma_path: render_subject(shell("gamma"))}, name="gamma")
    gamma = parse_subject(
        instance.tree_at(instance.accepted_coordinate().git_oid)[gamma_path], path=gamma_path
    )
    accept_tree(
        instance,
        {
            gamma_path: render_subject(
                gamma.model_copy(
                    update={
                        "lifecycle": ArtifactLifecycle(
                            state="retired", predecessor_digest=subject_digest(gamma).tagged
                        )
                    }
                )
            )
        },
        name="retire-gamma",
    )
    assert resolved("gamma") == (f"{KIND}/gamma", "identity")
