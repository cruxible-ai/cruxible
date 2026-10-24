"""A Line's `settle_change_set` terminal settles under one conditional mandate, or falls back."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.captures import capture_contract_digest
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.procedure_mandates import (
    MandateClaimScope,
    MandateCondition,
    ProcedureMandate,
    parse_procedure_mandate_any,
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.line_specs import (
    line_identity_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import SettleChangeSetNode
from cruxible_client.contracts.query.definitions import (
    CLAIM_TYPE_PIN_ROLE,
    QueryDefinition,
    QueryEvaluationPolicy,
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryEntry,
    QueryLiteralRef,
    QueryParameterDeclaration,
    QueryParameterRef,
    QueryProjection,
    QueryProjectionField,
    QuerySubjectFieldRef,
)
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.procedures.egress import TerminalEgressReceiptV4
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    service_get_playbill_procedure_run,
    service_run_playbill_line,
)
from tests.core_support._light_capture_support import record_contract as capture_contract
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import (
    PREDICATE,
    SUBJECT_ID,
    SUBJECT_KIND,
    _claim_type,
    _subject,
    item_template,
)


def _condition(*, only_subject: str | None, require_claim: bool = False) -> QueryDefinition:
    """The settle condition; `require_claim` also projects the not-yet-accepted Claim."""

    claim_type = _claim_type(capture_contract_digest(capture_contract()).tagged)

    return QueryDefinition(
        identity=ArtifactIdentity(kind="QueryDefinition", name="security.settle-condition"),
        entry=QueryEntry(
            binding="advisory",
            subject_kinds=(SUBJECT_KIND,),
            subject_id=QueryParameterRef(parameter="advisory_id"),
        ),
        where=(
            None
            if only_subject is None
            else QueryComparisonFilter(
                left=QuerySubjectFieldRef(binding="advisory", field="subject_id"),
                operator="eq",
                right=QueryLiteralRef(value=only_subject),
                value_type="string",
            )
        ),
        result_binding="advisory",
        result_shape="subject",
        result_cardinality="one",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="id",
                    value=QuerySubjectFieldRef(binding="advisory", field="subject_id"),
                ),
                *(
                    (
                        QueryProjectionField(
                            name="severity",
                            value=QueryClaimValueRef(binding="advisory", predicate=PREDICATE),
                        ),
                    )
                    if require_claim
                    else ()
                ),
            )
        ),
        parameters=(QueryParameterDeclaration(name="advisory_id", value_type="string"),),
        evaluation_policy=QueryEvaluationPolicy(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="refuse_on_conflict",
        ),
        default_budgets=QueryBudgets(max_results=1, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=1, max_traversal_depth=0),
        pins=(
            (
                ArtifactPin(
                    role=CLAIM_TYPE_PIN_ROLE,
                    target=claim_type.identity,
                    artifact_digest=claim_type_digest(claim_type).tagged,
                ),
            )
            if require_claim
            else ()
        ),
    )


def _predicate_claim_type(predicate: str) -> Any:
    """The fixture's severity ClaimType, under another predicate when asked."""

    claim_type = _claim_type(capture_contract_digest(capture_contract()).tagged)
    if predicate == PREDICATE:
        return claim_type
    return claim_type.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
            "predicate": predicate,
        }
    )


def _predicate_item(predicate: str) -> dict[str, object]:
    """One settle item stating the shaped severity under ``predicate``."""

    template = item_template()
    statement = dict(template["statement"])  # type: ignore[call-overload]
    statement["predicate"] = predicate
    return {**template, "statement": statement}


LEVEL_KIND = "security.severity_level"


def _level_kind_members(*, rules: bool = True) -> dict[str, bytes]:
    """Severity levels keyed by a slug whose key Claims cite the run's own Capture.

    ``rules=False`` gives the slug no evidence rule at all, as the shipped example
    spells a key ClaimType: the creation alone covers its key Claims.
    """

    from cruxible_client.contracts.artifacts import ArtifactRef
    from cruxible_client.contracts.policies import (
        CAPTURE_CONTRACT_REF_ROLE,
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionRule,
    )
    from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
    from tests.core_support._subject_kind_support import keyed_subject_kind, slug_claim_type

    slug = slug_claim_type(LEVEL_KIND).model_copy(  # type: ignore[attr-defined]
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="workspace-record",
                        claim_roles=("observation",),
                        capture_contracts=(
                            ArtifactRef(
                                role=CAPTURE_CONTRACT_REF_ROLE,
                                target=capture_contract().identity,
                            ),
                        ),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    if not rules:
        slug = slug.model_copy(update={"evidence_admission_policy": ClaimEvidenceAdmissionPolicy()})
    return {
        claim_type_path(slug.predicate): render_claim_type(slug),
        subject_kind_path(LEVEL_KIND): render_subject_kind(
            keyed_subject_kind(LEVEL_KIND, (slug.predicate,))
        ),
    }


def _level_item() -> dict[str, object]:
    """The severity level the run read, created by key: its key Claim cites the read."""

    return {
        "tag": "playbill-procedure-subject-proposal-item-v1",
        "subject_kind": LEVEL_KIND,
        "key": {"slug": "$steps.result.severity"},
        "rationale": "The advisory names this severity level.",
    }


#: The key that governs each settle world, for tests that approve ordinarily.
OWNERS: dict[Path, Any] = {}
#: The settling Procedure of each capture-triggered world, for landing trigger Captures.
PROCEDURES: dict[Path, Any] = {}
#: The propose grant's digest in each world that accepts one.
PROPOSE_GRANTS: dict[Path, str] = {}


def settle_world(  # type: ignore[no-untyped-def]  # noqa: PLR0913
    tmp_path: Path,
    *,
    only_subject: str | None = None,
    fallback: str = "propose",
    mandates: int = 1,
    capture_triggered: bool = False,
    max_authority: str = "settle",
    propose_mandate: bool = False,
    require_claim: bool = False,
    scope_predicates: tuple[str, ...] = (PREDICATE,),
    item_predicates: tuple[str, ...] = (PREDICATE,),
    subject_items: tuple[dict[str, object], ...] = (),
    subject_creation: tuple[str, ...] = (),
    candidate_templates: Any = None,
    change_kinds: tuple[str, ...] = ("create", "revise"),
    level_rules: bool = True,
):
    instance, owner, procedure, root, policy = fixtures._world(tmp_path, accept_procedure=False)
    source, shape = procedure.definition.nodes
    definition = procedure.definition.model_copy(
        update={
            "terminal_capability": 3,
            "nodes": (
                source,
                shape.model_copy(update={"next": "settle"}),
                SettleChangeSetNode(
                    node_id="settle",
                    candidate_templates=(
                        (
                            *subject_items,
                            *(_predicate_item(predicate) for predicate in item_predicates),
                        )
                        if candidate_templates is None
                        else candidate_templates
                    ),
                    result="$steps.result",
                ),
            ),
        }
    )
    with_terminal = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )
    line = fixtures._served_line(with_terminal, policy).model_copy(
        update={"max_authority": max_authority}
    )
    trigger_members: dict[str, bytes] = {}
    if capture_triggered:
        # Landing a Capture of the trigger contract makes an armed Line due.
        from cruxible_client.contracts.captures import (
            COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
            capture_contract_path,
            render_capture_contract,
        )
        from cruxible_client.contracts.triggers import CaptureLandingSchedule
        from tests.support.lines import line_trigger
        from tests.support.lines import trigger_members as trigger_files
        from tests.test_procedures.test_line_triggers import SELECTOR

        trigger_members.update(
            trigger_files(
                line_trigger(
                    "settle-on-landing",
                    line=line.identity.name,
                    schedule=CaptureLandingSchedule(event=SELECTOR),
                )
            )
        )
        trigger_members[
            capture_contract_path(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name)
        ] = render_capture_contract(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT)
        PROCEDURES[root] = with_terminal
    claim_types = {
        predicate: _predicate_claim_type(predicate)
        for predicate in dict.fromkeys((*scope_predicates, *item_predicates))
    }
    query = _condition(only_subject=only_subject, require_claim=require_claim)
    members: dict[str, bytes] = {
        **trigger_members,
        procedure_path(fixtures.PROCEDURE_NAME): render_procedure(with_terminal),
        line_spec_path(line.identity.name): render_line_spec(line),
        **{
            claim_type_path(item.predicate): render_claim_type(item)
            for item in claim_types.values()
        },
        subject_path(SUBJECT_KIND, SUBJECT_ID): render_subject(_subject()),
        query_definition_path(query.identity.name): render_query_definition(query),
        **(_level_kind_members(rules=level_rules) if subject_items else {}),
    }
    for index in range(mandates):
        mandate = ProcedureMandate(
            identity=ArtifactIdentity(kind="ProcedureMandate", name=f"settle-{index}"),
            procedure=ArtifactPin(
                role="procedure",
                target=with_terminal.identity,
                artifact_digest=procedure_artifact_digest(with_terminal).tagged,
            ),
            grants="settle",
            resource_ceiling=with_terminal.definition.hard_caps,
            namespace=("claims",),
            valid_from=datetime(2020, 1, 1, tzinfo=UTC),
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            scope=tuple(
                MandateClaimScope(
                    claim_type=ArtifactPin(
                        role="claim-type",
                        target=claim_types[predicate].identity,
                        artifact_digest=claim_type_digest(claim_types[predicate]).tagged,
                    ),
                    change_kinds=change_kinds,  # type: ignore[arg-type]
                )
                for predicate in sorted(scope_predicates)
            ),
            condition=MandateCondition(
                query=ArtifactPin(
                    role="condition-query",
                    target=query.identity,
                    artifact_digest=query_definition_digest(query).tagged,
                ),
                binding_parameter="advisory_id",
                required_fields=("id", "severity") if require_claim else ("id",),
                fallback=fallback,  # type: ignore[arg-type]
            ),
            subject_creation=subject_creation,
        )
        members[procedure_mandate_path(mandate.identity.name)] = render_procedure_mandate(mandate)
    if propose_mandate:
        settle_digests = [
            procedure_mandate_digest(parse_procedure_mandate_any(content, path=path)).tagged
            for path, content in members.items()
            if path.startswith("procedure-mandates/")
        ]
        # The propose grant's digest sorts after every settle grant's, so a
        # digest-order choice between them would pick the settle grant.
        for day in range(256):
            grant = ProcedureMandate(
                identity=ArtifactIdentity(kind="ProcedureMandate", name="propose-grant"),
                procedure=ArtifactPin(
                    role="procedure",
                    target=with_terminal.identity,
                    artifact_digest=procedure_artifact_digest(with_terminal).tagged,
                ),
                grants="propose",
                resource_ceiling=with_terminal.definition.hard_caps,
                namespace=("claims",),
                valid_from=datetime(2020, 1, 1, tzinfo=UTC),
                expires_at=datetime(2099, 1, 1, tzinfo=UTC) + timedelta(days=day),
            )
            if all(procedure_mandate_digest(grant).tagged > item for item in settle_digests):
                break
        members[procedure_mandate_path(grant.identity.name)] = render_procedure_mandate(grant)
        PROPOSE_GRANTS[instance.root] = procedure_mandate_digest(grant).tagged
    fixtures._accept_more(instance, owner, members, name="settle-world")
    OWNERS[instance.root] = owner
    return instance, root, line


def run_settle(  # type: ignore[no-untyped-def]
    instance, root, line, *, caller_rung: int = 3, now: datetime = fixtures.NOW
):
    identity_digest = line_identity_digest(line.identity)
    return service_run_playbill_line(
        instance,
        path_identity_digest=identity_digest,
        request=LineRunRequest(line=identity_digest, occurrence_id=None, evaluation_time=None),
        actor_context=fixtures._actor(instance).model_copy(update={"timestamp": now}),
        caller_rung=caller_rung,
        provider_runtime_operator=fixtures._Operator(fixtures._WorkspaceInvoker()),  # type: ignore[arg-type]
        workspace_file_reader=fixtures._reader(instance, root),
        daemon_clock=fixtures._TestClock(now),
    )


def _egress(state: Any):  # type: ignore[no-untyped-def]
    assert len(state.terminal_egress) == 1, state
    return state.terminal_egress[0]


def test_a_true_condition_settles_the_claim_into_accepted_state(tmp_path: Path) -> None:
    instance, root, line = settle_world(tmp_path)
    base = instance.accepted_coordinate()
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert (egress.kind, egress.verdict, egress.settle_outcome) == (
        "settle_change_set",
        "delivered",
        "settled",
    )
    head = instance.accepted_coordinate()
    assert head != base and egress.accepted_git_oid == head.git_oid
    # An uncapped settle carries no cap, and its receipt keeps its bytes.
    assert egress.capped_by is None
    (delivered,) = [item for item in _egress_records(instance, state) if "receipt" in item]
    assert "capped_by" not in delivered["receipt"]
    (path,) = egress.target_paths
    claim = parse_claim(instance.tree_at(head.git_oid)[path], path=path)
    assert claim.statement.predicate == PREDICATE
    record = instance.accepted_history()[-1].record
    assert record is not None and record.approvals == ()
    assert record.mandate_digest == egress.procedure_mandate_digest


def test_a_settle_submits_its_lowered_fork_with_the_full_tree_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle terminal hands admission the lowered fork, not a whole-tree copy (perf B2).

    Receive validation then judges the fork from its edits and evaluation
    advances the accepted base over the changed paths. The verdicts are the
    whole-tree ones: the full receive pass accepts a plain copy of the same tree,
    and evaluating that copy yields the same candidate and diagnostics.
    """

    from cruxible_core.derived.derived_state import SnapshotTree
    from cruxible_core.proposals import proposals as service

    instance, root, line = settle_world(tmp_path)
    real_fork, real_validate = service._receives_as_fork, service.validate_proposal_tree
    real_evaluate = service.evaluate_proposal_tree
    received: list[bool] = []
    evaluated: list[str | None] = []

    def receives_as_fork(tree, *, limits, base_tree):  # type: ignore[no-untyped-def]
        verdict = real_fork(tree, limits=limits, base_tree=base_tree)
        if isinstance(tree, SnapshotTree):
            received.append(verdict)
            if verdict:
                # The whole-tree pass over plain copies accepts the same tree.
                real_validate(dict(tree), limits=limits, base_tree=dict(base_tree))
        return verdict

    def evaluate(**kwargs):  # type: ignore[no-untyped-def]
        outcome = real_evaluate(**kwargs)
        copied = real_evaluate(**{**kwargs, "proposed_tree": dict(kwargs["proposed_tree"])})
        assert copied.diagnostics == outcome.diagnostics
        assert (copied.candidate is None) == (outcome.candidate is None)
        digest = None if outcome.candidate is None else outcome.candidate.candidate_digest
        assert digest == (None if copied.candidate is None else copied.candidate.candidate_digest)
        evaluated.append(digest)
        return outcome

    monkeypatch.setattr(service, "_receives_as_fork", receives_as_fork)
    monkeypatch.setattr(service, "evaluate_proposal_tree", evaluate)
    state = run_settle(instance, root, line)
    egress = _egress(state)
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "settled")
    assert received == [True]
    assert len(evaluated) == 1 and evaluated[0] is not None


def test_a_false_condition_falls_back_to_an_ordinary_proposal(tmp_path: Path) -> None:
    instance, root, line = settle_world(tmp_path, only_subject="someone-else")
    base = instance.accepted_coordinate()
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert egress.settle_outcome == "proposed" and egress.accepted_git_oid is None
    assert "cruxible.settle.condition_false" in (egress.fallback_reason or "")
    assert egress.proposal_id is not None
    assert instance.accepted_coordinate() == base


def test_a_false_condition_without_fallback_refuses_before_any_proposal(tmp_path: Path) -> None:
    instance, root, line = settle_world(tmp_path, only_subject="someone-else", fallback="refuse")
    base = instance.accepted_coordinate()
    state = run_settle(instance, root, line)
    egress = _egress(state)
    assert (egress.verdict, egress.refusal_code) == ("refused", "settle_condition_refused")
    assert instance.accepted_coordinate() == base
    assert not [
        record
        for record in instance.proposal_evidence().list_admissions()
        if "/procedure-" in record.target_ref
    ]


def test_an_incomplete_condition_still_falls_back_for_its_own_reason(tmp_path: Path) -> None:
    """The authority cap is a new fallback reason; the condition's own stay as they were."""

    # The condition requires the very Claim being settled, absent at the parent.
    instance, root, line = settle_world(tmp_path, require_claim=True)
    base = instance.accepted_coordinate()
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert egress.settle_outcome == "proposed" and egress.accepted_git_oid is None
    assert egress.fallback_reason == "cruxible.settle.condition_incomplete"
    assert egress.effective_authority == "settle"
    assert instance.accepted_coordinate() == base


# --- a settle terminal the run's authority caps at propose proposes instead ----


def _egress_records(instance, state):  # type: ignore[no-untyped-def]
    """This run's terminal_egress journal payloads, in order."""

    import cruxible_core.service.procedures.procedure_runs as service
    from cruxible_core.exhaust.records import parse_journal_payload
    from cruxible_core.storage.cas import BodyAccessContext

    journal, _root = service._journal(instance)  # noqa: SLF001
    stream = service._stream(instance)  # noqa: SLF001
    access = BodyAccessContext(principal_id="test", can_read_body=True)
    return [
        parse_journal_payload(
            instance.body_store().read(stored.record.payload_digest, access=access)
        )
        for partition_id in journal.partition_ids(stream)
        for stored in journal.all_records(stream, partition_id)
        if stored.record.event_kind == "terminal_egress" and stored.record.run_id == state.run_id
    ]


def _settle_submission(instance, egress):  # type: ignore[no-untyped-def]
    return instance.proposal_evidence().read_admission(egress.proposal_id).settle_submission


@pytest.mark.parametrize(
    ("max_authority", "caller_rung", "term"),
    [("propose", 3, "line_max_authority"), ("settle", 2, "mandate_grant")],
    ids=["line-capped", "mandate-capped"],
)
def test_a_settle_terminal_capped_at_propose_proposes_for_the_cap(
    tmp_path: Path, max_authority: str, caller_rung: int, term: str
) -> None:
    """The same fallback a failing condition takes, for the cap, naming its term.

    Only a propose grant is accepted, so nothing here could settle: the run
    proposes under that grant and never asks a settle mandate for authority.
    """

    instance, root, line = settle_world(
        tmp_path, max_authority=max_authority, mandates=0, propose_mandate=True
    )
    base = instance.accepted_coordinate()

    state = run_settle(instance, root, line, caller_rung=caller_rung)

    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert (egress.kind, egress.verdict, egress.settle_outcome) == (
        "settle_change_set",
        "delivered",
        "proposed",
    )
    assert egress.fallback_reason == f"cruxible.settle.authority_capped_by_{term}"
    assert (egress.required_authority, egress.effective_authority, egress.limiting_term) == (
        "settle",
        "propose",
        term,
    )
    assert egress.proposal_id is not None and egress.accepted_git_oid is None
    assert instance.accepted_coordinate() == base
    submission = _settle_submission(instance, egress)
    assert submission is not None
    assert (submission.mode, submission.fallback_reason) == ("fallback", egress.fallback_reason)
    assert submission.mandate_digest == egress.procedure_mandate_digest
    # The cap is typed on the served egress and on the journaled receipt.
    assert egress.capped_by == term
    (delivered,) = [item for item in _egress_records(instance, state) if "receipt" in item]
    assert TerminalEgressReceiptV4.model_validate(delivered["receipt"]).capped_by == term


def test_a_capped_settle_binds_the_propose_grant_over_a_settle_grant(tmp_path: Path) -> None:
    """A capped settle is a proposal: it binds the propose grant, not the settle grant.

    The propose grant's digest sorts last here, so a digest-order choice would
    bind the settle grant, and retiring it would refuse a proposal the propose
    grant still covers.
    """

    instance, root, line = settle_world(tmp_path, max_authority="propose", propose_mandate=True)

    state = run_settle(instance, root, line)

    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert egress.settle_outcome == "proposed"
    assert egress.procedure_mandate_digest == PROPOSE_GRANTS[instance.root]
    submission = _settle_submission(instance, egress)
    assert submission is not None and submission.mandate_digest == PROPOSE_GRANTS[instance.root]


def test_a_tier_lifted_settle_with_only_a_propose_grant_proposes_for_the_grant(
    tmp_path: Path,
) -> None:
    """The caller's tier reaches settle, but no mandate grants it: the grant caps it."""

    instance, root, line = settle_world(tmp_path, mandates=0, propose_mandate=True)
    base = instance.accepted_coordinate()

    state = run_settle(instance, root, line, caller_rung=3)

    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "proposed")
    assert egress.effective_authority == "settle"
    assert egress.fallback_reason == "cruxible.settle.authority_capped_by_mandate_grant"
    assert egress.capped_by == "mandate_grant"
    assert egress.proposal_id is not None and egress.accepted_git_oid is None
    assert instance.accepted_coordinate() == base


def test_an_observe_capped_settle_terminal_is_still_refused(tmp_path: Path) -> None:
    instance, root, line = settle_world(tmp_path, max_authority="observe")
    base = instance.accepted_coordinate()

    state = run_settle(instance, root, line)

    assert state.status == "node_refused", state.terminal
    assert state.terminal.code == "terminal_authority_capped_by_line_max_authority"
    egress = _egress(state)
    assert (egress.verdict, egress.effective_authority, egress.limiting_term) == (
        "refused_effective_authority",
        "observe",
        "line_max_authority",
    )
    assert egress.proposal_id is None
    assert instance.accepted_coordinate() == base


def test_graduating_the_line_to_settle_keeps_the_procedure_and_settles(tmp_path: Path) -> None:
    """One Procedure across graduation: its digest, the track record's key, never changes.

    Before graduation the propose-capped Line proposes; its successor capped at
    settle settles under the covering settle mandate, through the same artifact.
    """

    from datetime import timedelta

    from cruxible_client.contracts.procedures.line_specs import line_spec_digest
    from tests.core_support._candidate_support import submit_member_candidate
    from tests.core_support._knowledge_loop_support import accept_proposal

    instance, root, line = settle_world(tmp_path, max_authority="propose")
    capped = run_settle(instance, root, line)
    assert capped.status == "succeeded", capped.terminal
    assert _egress(capped).settle_outcome == "proposed"
    assert _egress(capped).fallback_reason == (
        "cruxible.settle.authority_capped_by_line_max_authority"
    )

    graduated = line.model_copy(
        update={
            "max_authority": "settle",
            "lifecycle": line.lifecycle.model_copy(
                update={"predecessor_digest": line_spec_digest(line).tagged}
            ),
        }
    )
    inspection = submit_member_candidate(
        instance,
        members={line_spec_path(graduated.identity.name): render_line_spec(graduated)},
        actor_id="owner",
        proposal_name="graduate-line",
        proposal_family="procedure",
        timestamp="2026-09-12T11:50:00.000000Z",
    )
    accept_proposal(instance, OWNERS[instance.root], inspection)

    settled = run_settle(instance, root, graduated, now=fixtures.NOW + timedelta(minutes=5))

    assert settled.status == "succeeded", settled.terminal
    egress = _egress(settled)
    assert egress.settle_outcome == "settled"
    assert egress.accepted_git_oid == instance.accepted_coordinate().git_oid
    assert settled.procedure_artifact_digest == capped.procedure_artifact_digest


def test_two_covering_mandates_refuse_as_ambiguous(tmp_path: Path) -> None:
    instance, root, line = settle_world(tmp_path, mandates=2)
    state = run_settle(instance, root, line)
    egress = _egress(state)
    assert (egress.verdict, egress.refusal_code) == ("refused", "settle_mandate_ambiguous")


class _Crash(BaseException):
    """A process death: not an exception any code path may catch and continue."""


def test_a_duplicate_settle_delivery_returns_the_same_settled_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    from cruxible_core.procedures import proposal_delivery as delivery_module

    receipts = []
    original = delivery_module.ProposalTerminalEgressSink.deliver_terminal_egress

    def twice(self, **kwargs):  # type: ignore[no-untyped-def]
        first = original(self, **kwargs)
        second = original(self, **kwargs)
        receipts.append((first, second))
        return second

    monkeypatch.setattr(
        delivery_module.ProposalTerminalEgressSink, "deliver_terminal_egress", twice
    )
    instance, root, line = settle_world(tmp_path)
    generations = len(instance.accepted_history())
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    ((first, second),) = receipts
    assert first == second and first.outcome == "settled"  # type: ignore[attr-defined]
    assert len(instance.accepted_history()) == generations + 1


def _crash_settle(monkeypatch, *, after_activation: bool) -> None:  # type: ignore[no-untyped-def]
    from cruxible_core.procedures import proposal_delivery as delivery_module
    from cruxible_core.procedures.execution import ProcedureExecutor

    crashed = {"value": False}
    original_activate = delivery_module.ProposalTerminalEgressSink._activate
    original_append = ProcedureExecutor._append_event

    def crashing_activate(self, result, **kwargs):  # type: ignore[no-untyped-def]
        if after_activation:
            original_activate(self, result, **kwargs)
        crashed["value"] = True
        raise _Crash()

    def dead_append(self, admission, records, event_kind, payload):  # type: ignore[no-untyped-def]
        if crashed["value"]:
            raise _Crash()
        return original_append(self, admission, records, event_kind, payload)

    monkeypatch.setattr(delivery_module.ProposalTerminalEgressSink, "_activate", crashing_activate)
    monkeypatch.setattr(ProcedureExecutor, "_append_event", dead_append)


@pytest.mark.parametrize(
    "after_activation", [True, False], ids=["after-acceptance", "before-activation"]
)
def test_a_crashed_settlement_recovers_one_accepted_result(
    tmp_path: Path, monkeypatch, after_activation: bool
) -> None:
    from datetime import timedelta

    from cruxible_core.service.procedures.procedure_runs import service_get_playbill_procedure_run
    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress

    instance, root, line = settle_world(tmp_path)
    generations = len(instance.accepted_history())
    _crash_settle(monkeypatch, after_activation=after_activation)
    with pytest.raises(_Crash):
        run_settle(instance, root, line)
    monkeypatch.undo()
    assert len(instance.accepted_history()) == generations + (1 if after_activation else 0)

    recovered = service_recover_proposal_egress(
        instance, recorded_at=fixtures.NOW + timedelta(minutes=1)
    )
    ((run_id, disposition),) = recovered.items()
    assert disposition == "delivered"
    # Exactly one accepted settlement, whichever side of activation crashed.
    assert len(instance.accepted_history()) == generations + 1
    (egress,) = service_get_playbill_procedure_run(instance, run_id=run_id).terminal_egress
    assert egress.settle_outcome == "settled"
    assert egress.accepted_git_oid == instance.accepted_coordinate().git_oid
    assert (
        service_recover_proposal_egress(instance, recorded_at=fixtures.NOW + timedelta(minutes=2))
        == {}
    )


def test_a_crashed_settlement_whose_head_moved_recompiles_and_settles(
    tmp_path: Path, monkeypatch
) -> None:
    """A moved head re-lowers the retained request and rechecks settle authority."""

    from cruxible_core.service.procedures.procedure_runs import service_get_playbill_procedure_run
    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress

    instance, root, line = settle_world(tmp_path)
    _crash_settle(monkeypatch, after_activation=False)
    with pytest.raises(_Crash):
        run_settle(instance, root, line)
    monkeypatch.undo()
    moved = _subject().model_copy(
        update={
            "identity": ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/moved-on"),
            "subject_id": "moved-on",
        }
    )
    fixtures._accept_more(
        instance,
        OWNERS[instance.root],
        {subject_path(SUBJECT_KIND, "moved-on"): render_subject(moved)},
        name="head-moves-on",
    )
    generations = len(instance.accepted_history())

    recovered = service_recover_proposal_egress(
        instance, recorded_at=fixtures.NOW + timedelta(minutes=1)
    )
    ((run_id, disposition),) = recovered.items()
    assert disposition == "delivered"
    assert len(instance.accepted_history()) == generations + 1
    (egress,) = service_get_playbill_procedure_run(instance, run_id=run_id).terminal_egress
    assert egress.verdict == "delivered"
    assert egress.accepted_git_oid == instance.accepted_coordinate().git_oid
    # Finalized: recovery has nothing left to do.
    assert (
        service_recover_proposal_egress(instance, recorded_at=fixtures.NOW + timedelta(minutes=2))
        == {}
    )


def _fallback_codes(egress: Any) -> set[str]:
    return set((egress.fallback_reason or "").split(", "))


def test_a_repeated_fallback_delivery_reports_the_same_proposed_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    from cruxible_core.procedures import proposal_delivery as delivery_module

    receipts = []
    original = delivery_module.ProposalTerminalEgressSink.deliver_terminal_egress

    def twice(self, **kwargs):  # type: ignore[no-untyped-def]
        first = original(self, **kwargs)
        second = original(self, **kwargs)
        receipts.append((first, second))
        return second

    monkeypatch.setattr(
        delivery_module.ProposalTerminalEgressSink, "deliver_terminal_egress", twice
    )
    instance, root, line = settle_world(tmp_path, only_subject="someone-else")
    base = instance.accepted_coordinate()
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    ((first, second),) = receipts
    assert first == second and first.outcome == "proposed"  # type: ignore[attr-defined]
    assert instance.accepted_coordinate() == base


def test_a_fallback_accepted_by_ordinary_review_is_still_reported_as_proposed(
    tmp_path: Path, monkeypatch
) -> None:
    from cruxible_core.procedures import proposal_delivery as delivery_module
    from cruxible_core.service.authoring.documents import (
        service_activate_playbill_proposal,
        service_submit_playbill_approval,
    )
    from tests.test_ledger.test_activation import _sign

    receipts = []
    original = delivery_module.ProposalTerminalEgressSink.deliver_terminal_egress

    def accept_between(self, **kwargs):  # type: ignore[no-untyped-def]
        first = original(self, **kwargs)
        approval = _sign(
            OWNERS[self.instance.root],
            first.candidate_digest,
            self.instance.accepted_coordinate().semantic_root,
        )
        service_submit_playbill_approval(
            self.instance,
            proposal_id=first.proposal_id,
            attestation=approval.attestation,
            authenticated_submitter="owner",
        )
        service_activate_playbill_proposal(
            self.instance, proposal_id=first.proposal_id, activated_by="owner"
        )
        second = original(self, **kwargs)
        receipts.append((first, second))
        return second

    monkeypatch.setattr(
        delivery_module.ProposalTerminalEgressSink, "deliver_terminal_egress", accept_between
    )
    instance, root, line = settle_world(tmp_path, only_subject="someone-else")
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    ((first, second),) = receipts
    # Ordinary review accepted the fallback; it never became a mandate settlement.
    assert instance.accepted_history()[-1].record.mandate_digest is None
    assert first == second and second.outcome == "proposed"  # type: ignore[attr-defined]
    assert second.accepted_git_oid is None  # type: ignore[attr-defined]
    assert "cruxible.settle.condition_false" in _fallback_codes(second)


@pytest.mark.parametrize(
    ("world", "reason"),
    [
        ({"only_subject": "someone-else"}, "cruxible.settle.condition_false"),
        (
            {"max_authority": "propose", "mandates": 0, "propose_mandate": True},
            "cruxible.settle.authority_capped_by_line_max_authority",
        ),
    ],
    ids=["condition", "authority-capped"],
)
def test_a_crashed_fallback_recovers_its_proposal_as_proposed(
    tmp_path: Path, monkeypatch, world: dict[str, Any], reason: str
) -> None:
    """Recovery re-drives the journaled request, the capped one included, to the same proposal."""

    from datetime import timedelta

    from cruxible_core.procedures import terminal_services
    from cruxible_core.procedures.execution import ProcedureExecutor
    from cruxible_core.service.procedures.procedure_runs import service_get_playbill_procedure_run
    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress

    instance, root, line = settle_world(tmp_path, **world)
    crashed = {"value": False}
    original_submit = terminal_services.ProposalTerminalAdapter.submit
    original_append = ProcedureExecutor._append_event

    def crashing_submit(self, **kwargs):  # type: ignore[no-untyped-def]
        original_submit(self, **kwargs)
        crashed["value"] = True
        raise _Crash()

    def dead_append(self, admission, records, event_kind, payload):  # type: ignore[no-untyped-def]
        if crashed["value"]:
            raise _Crash()
        return original_append(self, admission, records, event_kind, payload)

    monkeypatch.setattr(terminal_services.ProposalTerminalAdapter, "submit", crashing_submit)
    monkeypatch.setattr(ProcedureExecutor, "_append_event", dead_append)
    with pytest.raises(_Crash):
        run_settle(instance, root, line)
    monkeypatch.undo()
    (admitted,) = [
        record
        for record in instance.proposal_evidence().list_admissions()
        if "/procedure-" in record.target_ref
    ]
    assert admitted.settle_submission is not None
    assert admitted.settle_submission.mode == "fallback"

    recovered = service_recover_proposal_egress(
        instance, recorded_at=fixtures.NOW + timedelta(minutes=1)
    )
    ((run_id, disposition),) = recovered.items()
    assert disposition == "delivered"
    (egress,) = service_get_playbill_procedure_run(instance, run_id=run_id).terminal_egress
    assert egress.settle_outcome == "proposed" and egress.accepted_git_oid is None
    assert egress.proposal_id == admitted.proposal_id
    assert reason in _fallback_codes(egress)


def test_a_settled_delivery_finds_its_generation_without_walking_history(
    tmp_path: Path, monkeypatch
) -> None:
    from cruxible_core.runtime.instance import PlaybillInstance

    instance, root, line = settle_world(tmp_path)

    def no_walk(self):  # type: ignore[no-untyped-def]
        raise AssertionError("settlement enumerated accepted history")

    monkeypatch.setattr(PlaybillInstance, "accepted_history", no_walk)
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    assert _egress(state).settle_outcome == "settled"


def test_a_settle_reads_condition_facts_for_its_predicates_only(
    tmp_path: Path, monkeypatch
) -> None:
    """The condition is decided over the predicates its query reads, never every live Claim,
    and over lazily read trees, never two whole materialized ones."""

    from cruxible_core.runtime.instance import PlaybillInstance

    instance, root, line = settle_world(tmp_path)
    built = PlaybillInstance._accepted_query_facts
    asked: list[object] = []

    def scoped(source, coordinate, *, predicates=None, subject_kinds=None):  # type: ignore[no-untyped-def]
        asked.append(predicates)
        return built(source, coordinate, predicates=predicates, subject_kinds=subject_kinds)

    def no_materialized_tree(self, oid):  # type: ignore[no-untyped-def]
        raise AssertionError("settle delivery materialized a whole accepted tree")

    monkeypatch.setattr(PlaybillInstance, "_accepted_query_facts", staticmethod(scoped))
    monkeypatch.setattr(PlaybillInstance, "tree_at", no_materialized_tree)
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    assert _egress(state).settle_outcome == "settled"
    # Every build (delivery, then the proposal's own replay) asks for exactly the
    # predicates the condition query reads.
    expected = _condition(only_subject=None).referenced_predicates
    assert asked and all(predicates == expected for predicates in asked), asked


def test_a_settle_builds_its_condition_facts_once_over_the_condition_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivery, admission and activation share one facts build per request (perf B7).

    Admission now takes delivery's condition check and settlement the admission
    evaluation (perf E), so only delivery reads the facts; the facts must still
    be built over the condition kinds alone and answer as a fresh build does.

    Each condition read asks for the condition query's entry Subject kinds, not
    every Subject, and every target's condition result equals the one an
    unscoped, freshly built facts projection gives.
    """

    from cruxible_core.query import engine
    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.discovery import query as query_service

    instance, root, line = settle_world(tmp_path)
    kinds = _condition(only_subject=None).entry.subject_kinds
    built, build = PlaybillInstance._accepted_query_facts, query_service.build_accepted_query_facts
    asked: list[object] = []
    builds: list[object] = []
    compared: list[object] = []

    def scoped(source, coordinate, *, predicates=None, subject_kinds=None):  # type: ignore[no-untyped-def]
        asked.append(subject_kinds)
        return built(source, coordinate, predicates=predicates, subject_kinds=subject_kinds)

    def counted(source, *, coordinate, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("subject_kinds") == kinds:
            builds.append(coordinate.git_oid)
        return build(source, coordinate=coordinate, **kwargs)

    real_evaluate = engine.evaluate_claim_query

    def evaluate(definition, *, facts, coordinate, **kwargs):  # type: ignore[no-untyped-def]
        result = real_evaluate(definition, facts=facts, coordinate=coordinate, **kwargs)
        query = getattr(definition, "query", None)
        if getattr(getattr(query, "identity", None), "name", None) == "security.settle-condition":
            whole = build(instance, coordinate=coordinate)
            expected = real_evaluate(definition, facts=whole, coordinate=coordinate, **kwargs)
            assert result == expected
            compared.append(result.verdict)
        return result

    monkeypatch.setattr(PlaybillInstance, "_accepted_query_facts", staticmethod(scoped))
    monkeypatch.setattr(query_service, "build_accepted_query_facts", counted)
    monkeypatch.setattr(engine, "evaluate_claim_query", evaluate)
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    assert _egress(state).settle_outcome == "settled"
    assert asked and all(item == kinds for item in asked), asked
    assert len(builds) == 1, builds
    assert compared and set(compared) == {"completed"}


def test_every_settle_mandate_lookup_reads_its_one_path_never_the_tree(
    tmp_path: Path, monkeypatch
) -> None:
    """Delivery and the proposal's own evaluations find the mandate without listing paths."""

    from collections.abc import Iterator, Mapping

    from cruxible_core.proposals import delegated_authority

    instance, root, line = settle_world(tmp_path)
    lookup = delegated_authority._mandate_by_digest
    walked: list[tuple[str | None, int]] = []

    class CountingTree(Mapping[str, bytes]):
        def __init__(self, inner: Mapping[str, bytes]) -> None:
            self.inner = inner
            self.listed = 0

        def __getitem__(self, key: str) -> bytes:
            return self.inner[key]

        def __iter__(self) -> Iterator[str]:
            for key in self.inner:
                self.listed += 1
                yield key

        def __len__(self) -> int:
            return len(self.inner)

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self.inner, name)

    def counted(tree, digest, *, name=None):  # type: ignore[no-untyped-def]
        wrapped = CountingTree(tree)
        try:
            return lookup(wrapped, digest, name=name)
        finally:
            walked.append((name, wrapped.listed))

    monkeypatch.setattr(delegated_authority, "_mandate_by_digest", counted)
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    assert _egress(state).settle_outcome == "settled"
    # The delivery precheck reads the mandate by name; admission takes its
    # check and settlement admission's evaluation (perf E), so in this request
    # no evaluation looks it up again.
    assert walked and all(listed == 0 for _name, listed in walked), walked
    # A digest lookup, as an evaluation from stored records makes, lists no path either.
    head = instance.accepted_coordinate()
    tree = CountingTree(instance.immutable_tree_at(head.git_oid))
    assert lookup(tree, _egress(state).procedure_mandate_digest, name=None) is not None
    assert tree.listed == 0


@pytest.mark.parametrize(
    ("only_subject", "outcome"), [(None, "settled"), ("someone-else", "proposed")]
)
def test_an_armed_capture_triggered_line_settles_or_falls_back_on_its_own(
    tmp_path: Path, monkeypatch, only_subject: str | None, outcome: str
) -> None:
    from datetime import timedelta
    from types import SimpleNamespace

    from cruxible_client.contracts.line_dispatch import LineEnablementPrincipal
    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
    from cruxible_core.runtime import line_arms
    from cruxible_core.runtime.line_arms import dispatch_armed_line
    from cruxible_core.service.procedures.line_dispatch import (
        armed_work,
        service_enable_line,
        service_match_listening_lines,
    )
    from cruxible_core.service.procedures.procedure_runs import (
        service_get_playbill_procedure_run,
    )
    from tests.test_procedures import test_line_arming as arming
    from tests.test_procedures.test_line_triggers import capture

    instance, root, line = settle_world(tmp_path, only_subject=only_subject, capture_triggered=True)
    procedure = PROCEDURES[root]
    base = instance.accepted_coordinate()
    actor = fixtures._actor(instance).model_copy(update={"timestamp": fixtures.NOW})
    armed_at = fixtures.NOW - timedelta(seconds=10)
    # Armed under a credential whose label is the instance's accepted Principal,
    # which the settle terminal's proposal is submitted as.
    monkeypatch.setattr(
        line_arms,
        "get_runtime_credential_store",
        lambda: SimpleNamespace(
            get=lambda _id: arming._credential(
                instance_id=instance.descriptor.instance_id, label="owner"
            )
        ),
    )
    service_enable_line(
        instance,
        line.identity.name,
        principal=LineEnablementPrincipal(
            kind="runtime_credential", credential_id="cred-arm", label="owner"
        ),
        actor=actor,
        now=armed_at,
        daemon_id="daemon",
    )
    capture(
        instance,
        SimpleNamespace(
            artifact_digest=procedure_artifact_digest(procedure).tagged,
            procedure=SimpleNamespace(definition_digest=procedure.definition_digest),
        ),
        at=armed_at + timedelta(seconds=1),
    )
    service_match_listening_lines(
        instance, actor=actor, now=armed_at + timedelta(seconds=2), daemon_id="daemon"
    )
    (arm,) = armed_work(instance, now=fixtures.NOW)
    manager = SimpleNamespace(
        get=lambda _id: instance,
        workspace_file_reader=lambda _id: fixtures._reader(instance, root),
        provider_runtime_operator=lambda: fixtures._Operator(fixtures._WorkspaceInvoker()),
    )

    result = dispatch_armed_line(manager, instance.descriptor.instance_id, arm, now=fixtures.NOW)

    assert result is not None and [item.status for item in result.items] == ["admitted"]
    (egress,) = service_get_playbill_procedure_run(
        instance, run_id=result.items[0].run_id
    ).terminal_egress
    assert egress.settle_outcome == outcome, (
        egress.verdict,
        egress.refusal_code,
        egress.effective_authority,
        egress.limiting_term,
    )
    if outcome == "settled":
        assert instance.accepted_coordinate().git_oid == egress.accepted_git_oid
    else:
        assert instance.accepted_coordinate() == base and egress.proposal_id is not None


def _stale_row(instance, proposal_id: str, monkeypatch):  # type: ignore[no-untyped-def]
    from cruxible_core.coverage.contracts import CoverageAccessProfile
    from cruxible_core.service.discovery.next import NextRequest, service_playbill_next
    from tests.core_support._support import every_moved_base_overlaps

    # The head moves on with an unrelated Subject: these rows are about what a
    # stale settle says, so any moved base counts as stale here.
    every_moved_base_overlaps(monkeypatch)

    # Head moves past the proposal's base, so it can no longer activate.
    moved = _subject().model_copy(
        update={
            "identity": ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/moved-on"),
            "subject_id": "moved-on",
        }
    )
    fixtures._accept_more(
        instance,
        OWNERS[instance.root],
        {subject_path(SUBJECT_KIND, "moved-on"): render_subject(moved)},
        name="head-moves-on",
    )
    result = service_playbill_next(
        instance,
        request=NextRequest(
            evaluation_time=fixtures.NOW,
            access_profile=CoverageAccessProfile(
                profile_id="settle-stale", permitted_access_classes=("instance",)
            ),
        ),
        # A stale-proposal row shows only to the proposal's author.
        caller_principal_id=instance.proposal_evidence().read_admission(proposal_id).actor_id,
    )
    (row,) = [
        item
        for item in result.items
        if item.reason == "proposal_stale" and item.subject_identity == proposal_id
    ]
    return row


def test_a_stale_fallback_settle_names_how_it_was_submitted(tmp_path: Path, monkeypatch) -> None:
    instance, root, line = settle_world(tmp_path, only_subject="someone-else")
    egress = _egress(run_settle(instance, root, line))
    assert egress.settle_outcome == "proposed" and egress.proposal_id is not None

    row = _stale_row(instance, egress.proposal_id, monkeypatch)

    assert row.detail["settle_submission"]["mode"] == "fallback"
    assert row.detail["settle_submission"]["mandate_digest"] == egress.procedure_mandate_digest
    assert row.repair.required_change == "readmit_as_its_author_or_withdraw_the_stale_proposal"


def test_a_delegated_settle_that_lost_its_race_says_recompile_reverifies_authority(
    tmp_path: Path, monkeypatch
) -> None:
    instance, root, line = settle_world(tmp_path)
    _crash_settle(monkeypatch, after_activation=False)
    with pytest.raises(_Crash):
        run_settle(instance, root, line)
    monkeypatch.undo()
    (pending,) = [
        admission
        for admission in instance.proposal_evidence().list_admissions()
        if admission.settle_submission is not None
    ]
    assert pending.settle_submission is not None and pending.settle_submission.mode == "delegated"

    row = _stale_row(instance, pending.proposal_id, monkeypatch)

    assert row.detail["settle_submission"]["mode"] == "delegated"
    assert row.repair.required_change == (
        "activate_or_readmit_to_recompile_and_reverify_the_settle_or_withdraw_it"
    )
    assert row.repair.command == f"cruxible proposal readmit {pending.proposal_id}"


# --- the capped request's own law (review F-006) -------------------------------


def _prepared_request(tmp_path: Path, **world: Any):  # type: ignore[no-untyped-def]
    from cruxible_core.procedures.egress import TerminalEgressRequestV2

    tmp_path.mkdir()
    instance, root, line = settle_world(tmp_path, **world)
    state = run_settle(instance, root, line)
    (prepared,) = [
        item for item in _egress_records(instance, state) if item.get("verdict") == "prepared"
    ]
    return TerminalEgressRequestV2.model_validate(prepared["request"])


def _rekeyed(request: Any, **update: Any):  # type: ignore[no-untyped-def]
    """The request with ``update`` applied and its operation key recomputed, validated."""

    from cruxible_core.procedures.egress import TerminalEgressRequestV2, terminal_operation_key

    changed = request.model_copy(update=update)
    changed = changed.model_copy(update={"operation_key": terminal_operation_key(changed)})
    return TerminalEgressRequestV2.model_validate(changed.model_dump(mode="python"))


def test_a_capped_settle_request_names_exactly_the_cap_its_rung_allows(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from cruxible_core.procedures.egress import TerminalEgressRequestV2

    capped = _prepared_request(
        tmp_path / "capped", max_authority="propose", mandates=0, propose_mandate=True
    )
    assert (capped.capped_by, capped.limiting_term, capped.effective_rung) == (
        "line_requested_rung",
        "line_requested_rung",
        2,
    )
    assert (capped.required_rung, capped.granted_operation) == (2, "propose_change_set")
    plain = _prepared_request(tmp_path / "plain")
    assert (plain.capped_by, plain.effective_rung, plain.required_rung) == (None, 3, 3)
    as_propose = {"required_rung": 2, "granted_operation": "propose_change_set"}

    # Below settle, only the limiting term caps it.
    with pytest.raises(ValidationError, match="names its limiting term"):
        _rekeyed(capped, capped_by="propagated_sensitivity")
    # At the settle rung, only the absent settle grant caps it.
    with pytest.raises(ValidationError, match="names its limiting term"):
        _rekeyed(plain, capped_by="line_requested_rung", **as_propose)
    assert _rekeyed(plain, capped_by="mandate_grant", **as_propose).capped_by == "mandate_grant"
    # A capped settle asks for propose authority, never settle.
    with pytest.raises(ValidationError, match="required rung disagrees"):
        _rekeyed(capped, required_rung=3, granted_operation="activate_change_set")
    # Only a settle terminal is capped.
    with pytest.raises(ValidationError, match="only a settle terminal is capped"):
        _rekeyed(capped, kind="propose_change_set")
    # The cap is part of the operation: dropping it without a new key refuses.
    stale = capped.model_dump(mode="python")
    stale.pop("capped_by")
    with pytest.raises(ValidationError, match="required rung disagrees"):
        TerminalEgressRequestV2.model_validate(stale)


# --- per-rule trust: a ClaimType outside the settle grant's scope proposes ------

SCOPED_B = "security.advisory.exploitability"
UNSCOPED_C = "security.advisory.summary"


def test_a_claim_type_outside_the_settle_scope_proposes_naming_it(tmp_path: Path) -> None:
    """A grant over A and B; a change with A and C proposes under it, naming C."""

    instance, root, line = settle_world(
        tmp_path,
        scope_predicates=(PREDICATE, SCOPED_B),
        item_predicates=(PREDICATE, UNSCOPED_C),
    )
    base = instance.accepted_coordinate()

    state = run_settle(instance, root, line)

    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "proposed")
    assert egress.fallback_reason == "cruxible.settle.outside_mandate_scope"
    assert egress.outside_scope_claim_types == (f"ClaimType:{UNSCOPED_C}",)
    assert egress.capped_by is None and egress.accepted_git_oid is None
    assert egress.proposal_id is not None and len(egress.target_paths) == 2
    assert instance.accepted_coordinate() == base
    submission = _settle_submission(instance, egress)
    assert submission is not None
    assert (submission.mode, submission.fallback_reason) == (
        "fallback",
        "cruxible.settle.outside_mandate_scope",
    )
    assert submission.outside_scope_claim_types == (f"ClaimType:{UNSCOPED_C}",)
    # The settle grant itself is bound, as for a failing condition.
    assert submission.mandate_digest == egress.procedure_mandate_digest
    (delivered,) = [item for item in _egress_records(instance, state) if "receipt" in item]
    receipt = TerminalEgressReceiptV4.model_validate(delivered["receipt"])
    assert receipt.outside_scope_claim_types == (f"ClaimType:{UNSCOPED_C}",)


def test_a_change_inside_the_settle_scope_still_settles(tmp_path: Path) -> None:
    """The same grant over A and B settles a change carrying A and B alone."""

    instance, root, line = settle_world(
        tmp_path,
        scope_predicates=(PREDICATE, SCOPED_B),
        item_predicates=(PREDICATE, SCOPED_B),
    )
    base = instance.accepted_coordinate()

    state = run_settle(instance, root, line)

    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert egress.settle_outcome == "settled" and egress.fallback_reason is None
    assert egress.outside_scope_claim_types == ()
    assert egress.accepted_git_oid == instance.accepted_coordinate().git_oid != base.git_oid
    (delivered,) = [item for item in _egress_records(instance, state) if "receipt" in item]
    assert "outside_scope_claim_types" not in delivered["receipt"]


def test_a_change_with_no_claim_type_in_scope_still_proposes_naming_it(tmp_path: Path) -> None:
    """The grant applies to this Procedure; every ClaimType it changes is outside scope."""

    instance, root, line = settle_world(
        tmp_path, scope_predicates=(PREDICATE,), item_predicates=(UNSCOPED_C,)
    )

    state = run_settle(instance, root, line)

    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert egress.fallback_reason == "cruxible.settle.outside_mandate_scope"
    assert egress.outside_scope_claim_types == (f"ClaimType:{UNSCOPED_C}",)


# --- settled items that match earlier refusals surface per Line in `next` -----

RETIRED_AT = "2026-09-12T12:05:00.000000Z"


def _retire_claim(instance: Any, claim_id: str, *, at: str) -> None:
    from cruxible_client.contracts.authoring.models import ChangeSetAuthoringPayload
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._retirement_support import activate_submitted, retirement_member

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    created = coordinator.create(
        actor=actor,
        payload=ChangeSetAuthoringPayload(
            members=(retirement_member(instance, claim_id, reason="was-wrong"),),
            rationale="The advisory feed misreported this severity.",
        ),
        canonical_timestamp=at,
    )
    activate_submitted(
        instance, OWNERS[instance.root], coordinator.submit(created.intent.intent_id, actor=actor)
    )


def _settled_claim_id(state: Any) -> str:
    (child,) = _egress(state).children
    assert child.path is not None
    return str(child.path.rsplit("/", 1)[1].removesuffix(".json"))


def settled_refusal_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A Line that settled, had that Claim retired, then settled it again on the same bytes."""

    instance, root, line = settle_world(tmp_path)
    first = run_settle(instance, root, line)
    assert _egress(first).settle_outcome == "settled", first.terminal
    _retire_claim(instance, _settled_claim_id(first), at=RETIRED_AT)
    second = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=10))
    assert _egress(second).settle_outcome == "settled", second.terminal
    return instance, root, line, second


def _next_rows(instance: Any, reason: str) -> list[Any]:
    from cruxible_core.coverage.contracts import CoverageAccessProfile
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.service.discovery.next import NextRequest, service_playbill_next

    request = NextRequest(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=fixtures.NOW + timedelta(hours=1),
        access_profile=CoverageAccessProfile(
            profile_id="settled-refusals", permitted_access_classes=("instance", "public")
        ),
    )
    return [
        item
        for item in service_playbill_next(instance, request=request).items
        if item.reason == reason
    ]


def test_a_settled_item_lists_its_earlier_refusals_on_the_run_result(tmp_path: Path) -> None:
    """The settle terminal's run result names each settled item's receipts, as recorded."""

    instance, _root, _line, second = settled_refusal_world(tmp_path)
    (child,) = _egress(second).children
    (receipt,) = child.prior_refusals
    assert (receipt.kind, receipt.reason_code, receipt.decided_at) == (
        "claim_retired",
        "was-wrong",
        RETIRED_AT,
    )
    assert second.run_id is not None
    again = service_get_playbill_procedure_run(instance, run_id=second.run_id)
    assert again.terminal_egress[0].children[0].prior_refusals == (receipt,)


def test_next_names_a_line_that_settles_what_was_refused_before(tmp_path: Path) -> None:
    instance, _root, line, second = settled_refusal_world(tmp_path)

    (row,) = _next_rows(instance, "line_settled_prior_refusals")

    assert row.subject_identity == line.identity.qualified
    assert row.related_identities == (f"ProcedureRun:{second.run_id}",)
    assert row.detail["settled_items_matching_refusals"] == 1
    assert row.detail["settled_runs"] == 1
    assert row.repair.operation == "cruxible.authoring.submit"
    assert row.repair.command.startswith("cruxible authoring submit - <<< ")
    assert row.repair.arguments["payload"]["max_authority"] == "propose"


def test_the_line_row_reads_a_maintained_count_not_the_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One keyed probe per live Line and one Line record read, whatever the settled runs."""

    from cruxible_core.coverage.contracts import CoverageAccessProfile
    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.discovery.next import _line_settled_refusal_items
    from cruxible_core.storage.cas import ContentAddressedBodyStore

    instance, root, line, second = settled_refusal_world(tmp_path)
    profile = CoverageAccessProfile(
        profile_id="settled-refusals", permitted_access_classes=("instance", "public")
    )

    def counted() -> tuple[Any, ...]:
        index = instance.refusal_index()
        _line_settled_refusal_items(
            instance, coordinate=instance.accepted_coordinate(), access_profile=profile
        )  # warm: catch the index up to the head
        reads = {"blobs": 0, "cas": 0}
        blobs, read = PlaybillInstance.blobs_at, ContentAddressedBodyStore.read

        def counted_blobs(self: Any, oid: str, paths: Any) -> Any:
            reads["blobs"] += len(tuple(paths))
            return blobs(self, oid, paths)

        def counted_read(self: Any, digest: str, **kwargs: Any) -> bytes:
            reads["cas"] += 1
            return read(self, digest, **kwargs)

        before = index.line_probes
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(PlaybillInstance, "blobs_at", counted_blobs)
            patch.setattr(ContentAddressedBodyStore, "read", counted_read)
            (row,) = _line_settled_refusal_items(
                instance, coordinate=instance.accepted_coordinate(), access_profile=profile
            )
        return (index.line_probes - before, reads["blobs"], reads["cas"]), row

    small, row = counted()
    assert row.detail["settled_items_matching_refusals"] == 1
    # A third run settles the same statement again after its second retirement.
    _retire_claim(instance, _settled_claim_id(second), at="2026-09-12T12:15:00.000000Z")
    third = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=20))
    assert _egress(third).settle_outcome == "settled", third.terminal
    large, row = counted()
    assert row.detail["settled_items_matching_refusals"] == 2
    assert row.detail["settled_runs"] == 2
    # One read of the flagged Line's own record, for its repair payload.
    assert small == large == (1, 1, 0)


def submit_line_repair(instance: Any, row: Any) -> Any:
    """Submit a settled-prior-refusals row's repair payload exactly as emitted, and land it."""

    from pydantic import TypeAdapter

    from cruxible_client import contracts
    from cruxible_client.contracts.authoring.inputs import AuthoringInput
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.service.authoring.landing import service_land_authoring_submit

    payload = TypeAdapter(AuthoringInput).validate_python(row.repair.arguments["payload"])
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    submitted = coordinator.submit_input(
        actor=actor, input=payload, canonical_timestamp="2026-09-12T12:30:00.000000Z"
    )
    landed = service_land_authoring_submit(
        coordinator, actor=actor, submitted=submitted, accept="if_allowed", may_activate=True
    )
    assert isinstance(landed.outcome, contracts.AuthoringLanded), landed
    return landed


def test_demoting_the_line_closes_its_row(tmp_path: Path) -> None:
    """The repair payload is the Line successor: same Line, same pins, capped at propose."""

    from cruxible_client.contracts.procedures.line_specs import line_spec_digest, parse_line_spec

    instance, _root, line, _second = settled_refusal_world(tmp_path)
    (row,) = _next_rows(instance, "line_settled_prior_refusals")
    assert row.repair.target == line.identity.qualified
    submit_line_repair(instance, row)

    path = line_spec_path(line.identity.name)
    successor = parse_line_spec(
        instance.blob_at(instance.accepted_coordinate().git_oid, path), path=path
    )
    assert successor.identity == line.identity
    assert successor.procedure == line.procedure
    assert successor.acquisition_policy == line.acquisition_policy
    assert successor.max_authority == "propose"
    assert (successor.parameters, successor.budgets) == (line.parameters, line.budgets)
    assert successor.occurrence_epoch == line.occurrence_epoch
    assert successor.lifecycle.predecessor_digest == line_spec_digest(line).tagged
    assert _next_rows(instance, "line_settled_prior_refusals") == []


# --- settled counts are computed in post: delivery never touches them ----------


def _retired_then_resettle(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A settled Claim, retired: the next run on the same bytes settles a matching item."""

    instance, root, line = settle_world(tmp_path)
    first = run_settle(instance, root, line)
    assert _egress(first).settle_outcome == "settled", first.terminal
    _retire_claim(instance, _settled_claim_id(first), at=RETIRED_AT)
    return instance, root, line


def test_settle_delivery_never_touches_the_refusal_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivery does nothing for the row: no refusal-index call happens inside it."""

    from cruxible_core.procedures.proposal_delivery import ProposalTerminalEgressSink
    from cruxible_core.proposals.refusal_index import RefusalIndex
    from cruxible_core.runtime.instance import PlaybillInstance

    instance, root, line = _retired_then_resettle(tmp_path)
    delivering = {"active": False, "calls": 0}
    deliver = ProposalTerminalEgressSink.deliver_terminal_egress

    def watched_deliver(self: Any, *args: Any, **kwargs: Any) -> Any:
        delivering["active"] = True
        try:
            return deliver(self, *args, **kwargs)
        finally:
            delivering["active"] = False

    def guard(original: Any) -> Any:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            if delivering["active"]:
                delivering["calls"] += 1
                raise AssertionError("settle delivery called the refusal index")
            return original(*args, **kwargs)

        return guarded

    monkeypatch.setattr(ProposalTerminalEgressSink, "deliver_terminal_egress", watched_deliver)
    monkeypatch.setattr(PlaybillInstance, "refusal_index", guard(PlaybillInstance.refusal_index))
    monkeypatch.setattr(RefusalIndex, "current", guard(RefusalIndex.current))
    base = instance.accepted_coordinate()

    second = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=10))

    assert second.status == "succeeded", second.terminal
    assert _egress(second).settle_outcome == "settled"
    assert instance.accepted_coordinate() != base
    assert delivering["calls"] == 0
    # The count is computed afterwards, from the journal.
    (row,) = _next_rows(instance, "line_settled_prior_refusals")
    assert row.detail["settled_items_matching_refusals"] == 1


def test_a_restart_or_a_discarded_index_loses_no_count(tmp_path: Path) -> None:
    """Nothing is held in memory: a fresh index object, or a rebuilt file, recounts."""

    instance, root, line = _retired_then_resettle(tmp_path)
    second = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=10))
    assert _egress(second).settle_outcome == "settled", second.terminal

    # A restart before anything read the row: a new process has a new index object.
    instance.refusal_index().close()
    del instance.__dict__["_refusal_index"]
    (row,) = _next_rows(instance, "line_settled_prior_refusals")
    assert (row.detail["settled_items_matching_refusals"], row.detail["settled_runs"]) == (1, 1)

    # A discarded file: the rebuild resets the cursor and counts the journal again.
    index = instance.refusal_index()
    index.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(index.path) + suffix).unlink(missing_ok=True)
    (row,) = _next_rows(instance, "line_settled_prior_refusals")
    assert (row.detail["settled_items_matching_refusals"], row.detail["settled_runs"]) == (1, 1)
    assert row.detail["latest_run_id"] == second.run_id


def test_contention_only_delays_the_row(tmp_path: Path) -> None:
    """A busy index skips the post step at once; the next read counts what was left."""

    import sqlite3
    import time

    instance, root, line = _retired_then_resettle(tmp_path)
    instance.refusal_index().catch_up(instance)
    second = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=10))
    assert _egress(second).settle_outcome == "settled", second.terminal

    other = sqlite3.connect(instance.refusal_index().path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        from cruxible_core.service.procedures.settled_refusals import (
            count_settled_refusals_step,
        )

        started = time.monotonic()
        step = count_settled_refusals_step(instance)
        assert time.monotonic() - started < 1.0
        assert step.ran is False
    finally:
        other.execute("ROLLBACK")
        other.close()

    (row,) = _next_rows(instance, "line_settled_prior_refusals")
    assert row.detail["settled_items_matching_refusals"] == 1


def test_counting_is_idempotent_per_proposal(tmp_path: Path) -> None:
    """Counting the same journal again (a rebuilt journal index) changes nothing."""

    from cruxible_core.proposals.refusal_index import SettledCursor
    from cruxible_core.service.procedures.settled_refusals import catch_up_settled_refusals

    instance, root, line = _retired_then_resettle(tmp_path)
    second = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=10))
    assert _egress(second).settle_outcome == "settled", second.terminal
    (row,) = _next_rows(instance, "line_settled_prior_refusals")
    before = dict(row.detail)

    index = instance.refusal_index()
    cursor = index.settled_cursor(instance, wait=True)
    assert cursor is not None
    # Rewind the cursor as a journal-index rebuild would, and count again.
    assert index.advance_settled(
        instance,
        expected=cursor,
        through=SettledCursor(cursor.journal_identity, 0),
        deliveries=(),
        wait=True,
    )
    catch_up_settled_refusals(instance, steps=8, wait=True)

    (row,) = _next_rows(instance, "line_settled_prior_refusals")
    assert dict(row.detail) == before


def test_the_latest_run_is_the_latest_settlement_whatever_the_order(tmp_path: Path) -> None:
    """F-006: totals take every settlement; the latest moves only to a later instant."""

    from cruxible_core.proposals.refusal_index import SettledDelivery

    instance, root, line = _retired_then_resettle(tmp_path)
    older = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=10))
    _retire_claim(instance, _settled_claim_id(older), at="2026-09-12T12:15:00.000000Z")
    newer = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=20))
    assert _egress(newer).settle_outcome == "settled", newer.terminal
    (row,) = _next_rows(instance, "line_settled_prior_refusals")
    spec = row.detail["line_spec_digest"]

    # Rebuild, then count the newer delivery before the older one.
    index = instance.refusal_index()
    index.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(index.path) + suffix).unlink(missing_ok=True)
    index.catch_up(instance)
    for state, at in ((newer, "12:20"), (older, "12:10")):
        cursor = index.settled_cursor(instance, wait=True)
        assert cursor is not None
        assert index.advance_settled(
            instance,
            expected=cursor,
            through=cursor,
            deliveries=(
                SettledDelivery(
                    line_identity=line.identity.qualified,
                    line_spec_digest=spec,
                    run_id=str(state.run_id),
                    proposal_id=str(_egress(state).proposal_id),
                    settled_at=f"2026-09-12T{at}:00.000000Z",
                ),
            ),
            wait=True,
        )

    (row,) = _next_rows(instance, "line_settled_prior_refusals")
    assert (row.detail["settled_items_matching_refusals"], row.detail["settled_runs"]) == (2, 2)
    assert row.detail["latest_run_id"] == newer.run_id
    assert row.detail["latest_settled_at"] == "2026-09-12T12:20:00.000000Z"


def test_a_counting_step_is_bounded_and_flat_with_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caught-up step reads nothing; a one-record step reads the same at any size."""

    from cruxible_core.proposals.refusal_index import SettledCursor
    from cruxible_core.service.procedures.settled_refusals import count_settled_refusals_step
    from cruxible_core.storage.cas import ContentAddressedBodyStore

    def counted(instance: Any, *, rewind: bool) -> int:
        index = instance.refusal_index()
        if rewind:
            cursor = index.settled_cursor(instance, wait=True)
            assert cursor is not None
            index.advance_settled(
                instance,
                expected=cursor,
                through=SettledCursor(cursor.journal_identity, 0),
                deliveries=(),
                wait=True,
            )
        reads = {"cas": 0}
        read = ContentAddressedBodyStore.read

        def counted_read(self: Any, digest: str, **kwargs: Any) -> bytes:
            reads["cas"] += 1
            return read(self, digest, **kwargs)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(ContentAddressedBodyStore, "read", counted_read)
            step = count_settled_refusals_step(instance, wait=True, limit=1)
        assert step.ran and step.records <= 1
        return reads["cas"]

    sizes = []
    for runs in (1, 3):
        (tmp_path / f"runs-{runs}").mkdir()
        instance, root, line = _retired_then_resettle(tmp_path / f"runs-{runs}")
        previous = None
        for offset in range(runs):
            if previous is not None:
                _retire_claim(
                    instance,
                    _settled_claim_id(previous),
                    at=f"2026-09-12T12:{15 + 10 * offset:02d}:00.000000Z",
                )
            previous = run_settle(
                instance, root, line, now=fixtures.NOW + timedelta(minutes=10 + 10 * offset)
            )
            assert _egress(previous).settle_outcome == "settled", previous.terminal
        assert _next_rows(instance, "line_settled_prior_refusals")  # caught up
        sizes.append((counted(instance, rewind=False), counted(instance, rewind=True)))
    assert sizes[0] == sizes[1]
    assert sizes[0][0] == 0


def test_the_next_consumer_counts_settlements_in_the_background(tmp_path: Path) -> None:
    """The consumer tick counts from the cursor; no read of `next` is needed."""

    from types import SimpleNamespace

    from cruxible_core.consumers.next import settled_refusals as part

    instance, root, line = _retired_then_resettle(tmp_path)
    second = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=10))
    assert _egress(second).settle_outcome == "settled", second.terminal
    index = instance.refusal_index()
    (row,) = _next_rows(instance, "line_settled_prior_refusals")
    spec = row.detail["line_spec_digest"]

    # Discard the counts; the tick (not a `next` read) recounts them.
    index.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(index.path) + suffix).unlink(missing_ok=True)
    index.catch_up(instance)
    key = (line.identity.qualified, spec)
    assert index.settled_refusals(instance, (key,)) == {}
    (work,) = part._PART.due(instance, now=fixtures.NOW)
    part._PART.run(SimpleNamespace(get=lambda _id: instance), "instance", work, now=fixtures.NOW)

    counted = index.settled_refusals(instance, (key,))[key]
    assert (counted.items, counted.runs, counted.latest_run_id) == (1, 1, second.run_id)
    assert part._PART.due(instance, now=fixtures.NOW) == ()
    (health,) = part._PART.health(instance, now=fixtures.NOW)
    assert health.state == "running"


# --- next reports retirement coverage from its final snapshot (review F-011, F-012)


def _next_result(instance: Any) -> Any:
    from cruxible_core.coverage.contracts import CoverageAccessProfile
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.service.discovery.next import NextRequest, service_playbill_next

    return service_playbill_next(
        instance,
        request=NextRequest(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=fixtures.NOW + timedelta(hours=1),
            access_profile=CoverageAccessProfile(
                profile_id="settled-refusals", permitted_access_classes=("instance", "public")
            ),
        ),
    )


def _unrelated_generations(instance: Any, count: int) -> None:
    from cruxible_client.contracts.subjects import SubjectShell

    for number in range(count):
        shell = SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/backlog-{number}"),
            subject_kind=SUBJECT_KIND,
            subject_id=f"backlog-{number}",
        )
        fixtures._accept_more(
            instance,
            OWNERS[instance.root],
            {subject_path(SUBJECT_KIND, f"backlog-{number}"): render_subject(shell)},
            name=f"backlog-{number}",
        )


def test_next_reports_coverage_while_a_first_matching_count_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-011: with no count row yet, the answer still says how far retirements are covered."""

    from cruxible_core.proposals import refusal_index as module

    instance, root, line = _retired_then_resettle(tmp_path)
    instance.refusal_index().catch_up(instance)
    monkeypatch.setattr(module, "RETIREMENT_PAGE", 1)
    _unrelated_generations(instance, 12)
    second = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=10))
    assert _egress(second).settle_outcome == "settled", second.terminal
    head = instance.accepted_history()[-1].sequence

    result = _next_result(instance)

    coverage = result.status.prior_refusal_coverage
    assert coverage is not None
    assert coverage.accepted_generation == head
    assert coverage.indexed_through_generation < head
    assert [item for item in result.items if item.reason == "line_settled_prior_refusals"] == []
    # Later answers resume until current; then the row appears, without the note.
    for _attempt in range(10):
        result = _next_result(instance)
        if result.status.prior_refusal_coverage is None:
            break
    assert result.status.prior_refusal_coverage is None
    (row,) = [item for item in result.items if item.reason == "line_settled_prior_refusals"]
    assert row.detail["settled_items_matching_refusals"] == 1
    assert "counted_through_generation" not in row.detail


def test_next_coverage_is_read_after_its_own_catch_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-012: a backlog this answer's own catch-up clears is not reported as lagging."""

    from cruxible_core.proposals import refusal_index as module

    instance, _root, _line, _second = settled_refusal_world(tmp_path)
    assert _next_rows(instance, "line_settled_prior_refusals")  # a maintained count
    monkeypatch.setattr(module, "RETIREMENT_PAGE", 1)
    _unrelated_generations(instance, 2)

    result = _next_result(instance)

    (row,) = [item for item in result.items if item.reason == "line_settled_prior_refusals"]
    assert "counted_through_generation" not in row.detail
    assert result.status.prior_refusal_coverage is None
    peeked = instance.refusal_index().peek(instance)
    assert peeked is not None and peeked[0] == instance.accepted_history()[-1].sequence


# --- a settle grant creates Subjects only of the kinds it names (identity D1) ---


def test_a_settle_grant_naming_a_kind_settles_its_new_subjects(tmp_path: Path) -> None:
    instance, root, line = settle_world(
        tmp_path, subject_items=(_level_item(),), subject_creation=(LEVEL_KIND,)
    )
    base = instance.accepted_coordinate()
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "settled"), egress
    head = instance.accepted_coordinate()
    assert head != base
    tree = instance.tree_at(head.git_oid)
    created = f"subjects/{LEVEL_KIND}/high.json"
    assert created in egress.target_paths and created in tree
    # The level's key Claim settled with it; only the advisory's Claim was a
    # condition target.
    predicates = sorted(
        parse_claim(tree[path], path=path).statement.predicate
        for path in egress.target_paths
        if path.startswith("claims/")
    )
    assert predicates == sorted((PREDICATE, f"{LEVEL_KIND}.slug"))


def test_a_settled_creation_covers_its_key_claim_with_no_evidence_rule(tmp_path: Path) -> None:
    """A settle creation's key Claim is supported though its key ClaimType has no rule."""

    from cruxible_client.contracts.claim_reads import ClaimValuesRequest
    from cruxible_core.service.claims.claim_reads import service_read_claim_values

    instance, root, line = settle_world(
        tmp_path,
        subject_items=(_level_item(),),
        subject_creation=(LEVEL_KIND,),
        level_rules=False,
    )
    egress = _egress(run_settle(instance, root, line))
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "settled"), egress

    (row,) = service_read_claim_values(
        instance,
        request=ClaimValuesRequest(
            subject_paths=(f"subjects/{LEVEL_KIND}/high.json",),
            predicates=(f"{LEVEL_KIND}.slug",),
            evaluation_time=fixtures.NOW,
        ),
    ).values
    assert row.verdict == "supported"


def test_a_settle_grant_naming_no_kind_never_creates_a_subject(tmp_path: Path) -> None:
    instance, root, line = settle_world(tmp_path, subject_items=(_level_item(),))
    base = instance.accepted_coordinate()
    state = run_settle(instance, root, line)
    egress = _egress(state)
    assert (egress.verdict, egress.refusal_code) == ("refused", "settle_mandate_missing"), egress
    assert instance.accepted_coordinate() == base


def test_a_moved_head_with_suspended_mandate_leaves_delegated_proposal_stale(
    tmp_path: Path, monkeypatch
) -> None:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress
    from cruxible_core.service.proposals.proposals import service_playbill_proposal_status

    instance, root, line = settle_world(tmp_path)
    _crash_settle(monkeypatch, after_activation=False)
    with pytest.raises(_Crash):
        run_settle(instance, root, line)
    monkeypatch.undo()
    (pending,) = [
        item
        for item in instance.proposal_evidence().list_admissions()
        if item.settle_submission is not None
    ]
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    # Locate only the bound grant, rather than relying on the fixture's name.
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        (identity,) = projection.typed.connection.execute(
            "SELECT identity FROM procedure_mandates WHERE artifact_digest=?",
            (pending.settle_submission.mandate_digest,),
        ).fetchone()
        mandate = projection.typed.source(identity)
    assert isinstance(mandate, ProcedureMandate)
    path = procedure_mandate_path(mandate.identity.name)
    suspended = mandate.model_copy(
        update={
            "suspended": True,
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(mandate).tagged
            ),
        }
    )
    assert path in tree
    fixtures._accept_more(
        instance,
        OWNERS[instance.root],
        {path: render_procedure_mandate(suspended)},
        name="suspend-before-publication",
    )
    head = instance.accepted_coordinate()
    recovered = service_recover_proposal_egress(
        instance, recorded_at=fixtures.NOW + timedelta(minutes=1)
    )
    assert set(recovered.values()) == {"refused"}
    assert instance.accepted_coordinate() == head
    assert (
        service_playbill_proposal_status(instance, proposal_id=pending.proposal_id).status
        == "awaiting_readmit"
    )
    assert instance.proposal_evidence().read_withdrawal(pending.proposal_id) is None


def test_recovery_follows_a_recompiled_settle_accepted_before_its_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress

    instance, root, line = settle_world(tmp_path)
    _crash_settle(monkeypatch, after_activation=False)
    with pytest.raises(_Crash):
        run_settle(instance, root, line)
    monkeypatch.undo()
    moved = _subject().model_copy(
        update={
            "identity": ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/recovery-head"),
            "subject_id": "recovery-head",
        }
    )
    fixtures._accept_more(
        instance,
        OWNERS[instance.root],
        {subject_path(SUBJECT_KIND, "recovery-head"): render_subject(moved)},
        name="move-before-recovery",
    )
    before = len(instance.accepted_history())
    _crash_settle(monkeypatch, after_activation=True)
    with pytest.raises(_Crash):
        service_recover_proposal_egress(instance, recorded_at=fixtures.NOW + timedelta(minutes=1))
    monkeypatch.undo()
    assert len(instance.accepted_history()) == before + 1
    recovered = service_recover_proposal_egress(
        instance, recorded_at=fixtures.NOW + timedelta(minutes=2)
    )
    assert set(recovered.values()) == {"delivered"}
    assert len(instance.accepted_history()) == before + 1


def test_a_settle_rerun_whose_items_restate_accepted_state_changes_nothing(
    tmp_path: Path,
) -> None:
    """All-unchanged settle: the second run proposes and accepts nothing (design row 22)."""

    instance, root, line = settle_world(tmp_path)
    first = _egress(run_settle(instance, root, line))
    assert first.settle_outcome == "settled"
    head = instance.accepted_coordinate()

    again = run_settle(instance, root, line, now=fixtures.NOW + timedelta(minutes=5))
    egress = _egress(again)
    assert egress.proposal_id is None and egress.accepted_git_oid is None
    assert egress.children and all(child.unchanged for child in egress.children)
    assert instance.accepted_coordinate() == head
