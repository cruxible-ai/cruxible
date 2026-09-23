"""A Line's `settle_change_set` terminal settles under one conditional mandate, or falls back."""

from __future__ import annotations

from datetime import UTC, datetime
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
    MandateClaimScopeV1,
    MandateConditionV1,
    ProcedureMandateV2,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v4
from cruxible_client.contracts.procedures.line_specs import (
    line_identity_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import SettleChangeSetNodeV3
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryComparisonFilterV1,
    QueryEntryV1,
    QueryLiteralRefV1,
    QueryParameterDeclarationV1,
    QueryParameterRefV1,
    QueryProjectionFieldV1,
    QueryProjectionV1,
    QuerySubjectFieldRefV1,
)
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequestV1,
    service_run_playbill_line,
)
from tests.core_support._pc_c_support import capture_contract
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import (
    PREDICATE,
    SUBJECT_ID,
    SUBJECT_KIND,
    _claim_type,
    _subject,
    item_template,
)


def _condition(*, only_subject: str | None) -> QueryDefinitionV1:
    return QueryDefinitionV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name="security.settle-condition"),
        entry=QueryEntryV1(
            binding="advisory",
            subject_kinds=(SUBJECT_KIND,),
            subject_id=QueryParameterRefV1(parameter="advisory_id"),
        ),
        where=(
            None
            if only_subject is None
            else QueryComparisonFilterV1(
                left=QuerySubjectFieldRefV1(binding="advisory", field="subject_id"),
                operator="eq",
                right=QueryLiteralRefV1(value=only_subject),
                value_type="string",
            )
        ),
        result_binding="advisory",
        result_shape="subject",
        result_cardinality="one",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="id",
                    value=QuerySubjectFieldRefV1(binding="advisory", field="subject_id"),
                ),
            )
        ),
        parameters=(QueryParameterDeclarationV1(name="advisory_id", value_type="string"),),
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="refuse_on_conflict",
        ),
        default_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
    )


def settle_world(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    only_subject: str | None = None,
    fallback: str = "propose",
    mandates: int = 1,
):
    instance, owner, procedure, root, policy = fixtures._world(tmp_path, accept_procedure=False)
    source, shape = procedure.definition.nodes
    definition = procedure.definition.model_copy(
        update={
            "terminal_capability": 3,
            "nodes": (
                source,
                shape.model_copy(update={"next": "settle"}),
                SettleChangeSetNodeV3(node_id="settle", candidate_templates=(item_template(),)),
            ),
        }
    )
    with_terminal = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v4(definition).tagged,
        }
    )
    line = fixtures._served_line(with_terminal, policy).model_copy(
        update={"requested_terminal_rung": 3}
    )
    claim_type = _claim_type(capture_contract_digest(capture_contract()).tagged)
    query = _condition(only_subject=only_subject)
    members: dict[str, bytes] = {
        procedure_path(fixtures.PROCEDURE_NAME): render_procedure(with_terminal),
        line_spec_path(line.identity.name): render_line_spec(line),
        claim_type_path(claim_type.predicate): render_claim_type(claim_type),
        subject_path(SUBJECT_KIND, SUBJECT_ID): render_subject(_subject()),
        query_definition_path(query.identity.name): render_query_definition(query),
    }
    for index in range(mandates):
        mandate = ProcedureMandateV2(
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
            scope=(
                MandateClaimScopeV1(
                    claim_type=ArtifactPin(
                        role="claim-type",
                        target=claim_type.identity,
                        artifact_digest=claim_type_digest(claim_type).tagged,
                    ),
                    change_kinds=("create", "revise"),
                ),
            ),
            condition=MandateConditionV1(
                query=ArtifactPin(
                    role="condition-query",
                    target=query.identity,
                    artifact_digest=query_definition_digest(query).tagged,
                ),
                binding_parameter="advisory_id",
                required_fields=("id",),
                fallback=fallback,  # type: ignore[arg-type]
            ),
        )
        members[procedure_mandate_path(mandate.identity.name)] = render_procedure_mandate(mandate)
    fixtures._accept_more(instance, owner, members, name="settle-world")
    return instance, root, line


def run_settle(instance, root, line, *, caller_rung: int = 3):  # type: ignore[no-untyped-def]
    identity_digest = line_identity_digest(line.identity)
    return service_run_playbill_line(
        instance,
        path_identity_digest=identity_digest,
        request=LineRunRequestV1(
            line_identity_digest=identity_digest, occurrence_id=None, evaluation_time=None
        ),
        actor_context=fixtures._actor(instance).model_copy(update={"timestamp": fixtures.NOW}),
        caller_rung=caller_rung,
        provider_runtime_operator=fixtures._Operator(fixtures._WorkspaceInvoker()),  # type: ignore[arg-type]
        workspace_file_reader=fixtures._reader(instance, root),
        daemon_clock=fixtures._TestClock(fixtures.NOW),
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
    (path,) = egress.target_paths
    claim = parse_claim(instance.tree_at(head.git_oid)[path], path=path)
    assert claim.statement.predicate == PREDICATE
    record = instance.accepted_history()[-1].record
    assert record is not None and record.approvals == ()
    assert record.mandate_digest == egress.procedure_mandate_digest


def test_a_false_condition_falls_back_to_an_ordinary_proposal(tmp_path: Path) -> None:
    instance, root, line = settle_world(tmp_path, only_subject="someone-else")
    base = instance.accepted_coordinate()
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    egress = _egress(state)
    assert egress.settle_outcome == "proposed" and egress.accepted_git_oid is None
    assert "playbill.settle.condition_false" in (egress.fallback_reason or "")
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
