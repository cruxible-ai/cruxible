"""Review regressions: core-only receipts, conservative bases and bounded diagnostics."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
)
from cruxible_core.exhaust.line_inbox import LineInboxStore
from tests.core_support._identity_support import accept_tree
from tests.support.operation_world import NOW, Invoker, Operator, actor, author_procedure
from tests.support.procedures import procedure_spec
from tests.test_procedures.test_claims_terminal import (
    KIND,
    PREDICATE,
    activate,
    candidate_claims,
    claim_type,
    claims_spec,
    claims_world,
    install,
    run,
)


class _MixedInvoker(Invoker):
    def invoke_provider(self, **kwargs: Any) -> Any:
        from tests.test_procedures.test_procedure_source_runs import _WorkspaceInvoker

        if "bytes" in kwargs["context"].input:
            return _WorkspaceInvoker().invoke_provider(**kwargs)
        return super().invoke_provider(**kwargs)


def _reader(instance: Any, root: Path) -> Any:
    from cruxible_core.documents.workspace_file import WorkspaceFileReader

    return WorkspaceFileReader(
        instance_id=instance.descriptor.instance_id,
        operating_profile="local",
        attached_roots=(root,),
        managed_roots=(instance.root,),
    )


def _direct(instance: Any, root: Path, spec: Any, payload: object) -> Any:
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequest,
        service_run_playbill_procedure,
    )

    author_procedure(instance, spec)
    return service_run_playbill_procedure(
        instance,
        name=spec.procedure,
        request=ProcedureRunRequest(input=payload, evaluation_time=NOW),
        actor_context=actor(),
        caller_rung=2,
        provider_runtime_operator=Operator(_MixedInvoker()),
        workspace_file_reader=_reader(instance, root),
    )


def _inbox(instance: Any) -> tuple[Any, ...]:
    store = LineInboxStore(instance)
    page, _ = store.page(line=None, status=None, limit=1000, cursor=None)
    return tuple(store.get(item.envelope.item_id) for item in page)


def _grant_settle(
    instance: Any,
    line_name: str,
    predicate: str,
    *,
    changes: Any = None,
    value: Any = None,
    subjects: str | None = None,
    with_condition: bool = True,
) -> str:
    """Accept a settle grant over one ClaimType; return its digest."""
    from cruxible_client.contracts.procedure_mandates import (
        MandateClaimScope,
        MandateCondition,
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.query.definitions import (
        query_definition_digest,
        query_definition_path,
        render_query_definition,
    )
    from tests.support.mandates import procedure_mandate, scope_changes
    from tests.test_procedures.test_procedure_settlement_delivery import _condition

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    line = parse_line_spec(tree[line_spec_path(line_name)], path=line_spec_path(line_name))
    ct = parse_claim_type(tree[claim_type_path(predicate)], path=claim_type_path(predicate))
    query = _condition(only_subject=None)
    query = query.model_copy(
        update={"entry": query.entry.model_copy(update={"subject_kinds": (KIND,)})}
    )
    mandate = procedure_mandate(
        line_name + "-settle",
        grants="settle",
        namespace=("claims",),
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
        scope=(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type",
                    target=ct.identity,
                    artifact_digest=claim_type_digest(ct).tagged,
                ),
                changes=changes or scope_changes("create", "revise"),
                value=value,
            ),
        ),
        subjects=subjects,
        condition=MandateCondition(
            query=ArtifactPin(
                role="condition-query",
                target=query.identity,
                artifact_digest=query_definition_digest(query).tagged,
            ),
            binding_parameter="advisory_id",
            required_fields=("id",),
            fallback="propose",
        )
        if with_condition
        else None,
    )
    accept_tree(
        instance,
        {
            query_definition_path(query.identity.name): render_query_definition(query),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="settle-grant",
    )
    from cruxible_client.contracts.procedure_mandates import procedure_mandate_digest

    return procedure_mandate_digest(mandate).tagged


@pytest.mark.parametrize("kind", ["propose_change_set", "settle_change_set"])
def test_authored_command_terminals_are_retired(kind: str) -> None:
    from cruxible_client.contracts.procedures.nodes import RetiredNodeKind, refuse_retired_kinds

    with pytest.raises(RetiredNodeKind, match="claims"):
        refuse_retired_kinds(
            [
                {
                    "kind": kind,
                    "node_id": "write",
                    "candidate_templates": [{"procedure_evidence": {}}],
                }
            ]
        )


@pytest.mark.parametrize(
    "field",
    [
        "revises",
        "retires",
        "source",
        "citation_role",
        "existing_claim_dispositions",
        "procedure_evidence",
    ],
)
def test_provider_data_cannot_supply_claim_commands(tmp_path: Path, field: str) -> None:
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open", field: "forged"}]}
    )
    install(instance, claims_spec())
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (claim,) = candidate_claims(instance, state)
    assert claim.statement.object.value == "open"
    assert claim.lifecycle.predecessor_digest is None
    assert claim.backing.procedure_evidence.value.basis == "extracted"
    assert claim.backing.procedure_evidence.value.extractor_version != "forged"


def test_authored_literal_receipt_is_refused_before_acceptance(tmp_path: Path) -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    instance, _root = claims_world(tmp_path, {"entries": []})
    base = claims_spec().model_dump(mode="python", by_alias=True)
    base["steps"][-1] = {
        "id": "write",
        "propose": [{"procedure_evidence": {"value": {"basis": "extracted"}}}],
        "result": {},
    }
    before = instance.accepted_coordinate()
    with pytest.raises(
        (AuthoringLoweringError, ValidationError), match="procedure_node_kind_retired.*claims"
    ):
        author_procedure(instance, procedure_spec(base=base))
    assert instance.accepted_coordinate() == before


@pytest.mark.parametrize("feature", ["when_absent", "covers", "enumeration", "mirror"])
def test_misplaced_or_unpaired_absence_in_authored_spec_is_refused(feature: str) -> None:
    base = claims_spec().model_dump(mode="python", by_alias=True)
    base["steps"][-1][feature] = "mirror"
    with pytest.raises(ValidationError):
        procedure_spec(base=base, reference_text=True)


@pytest.mark.parametrize("capture", [True, False], ids=["extract-row", "no-capture"])
def test_input_value_is_inadmissible_not_extracted(tmp_path: Path, capture: bool) -> None:
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
    )
    base = claims_spec().model_dump(mode="python", by_alias=True)
    base["input"] = {"status": "enum[open, closed, unknown]"}
    base["steps"][-1]["claims"][0]["value"] = "$input.status"
    if not capture:
        base["slots"] = {}
        terminal = base["steps"][-1]
        terminal["for_each"] = "$steps.rows.rows"
        terminal["subject"]["key"] = {"cve_id": "$item.key"}
        base["steps"] = [
            {"project": "rows", "fields": {"rows": [{"key": "CVE-2026-0001"}]}},
            terminal,
        ]
    state = _direct(
        instance, root, procedure_spec(base=base, reference_text=True), {"status": "closed"}
    )
    assert state.status == "succeeded", state.terminal
    assert state.terminal_egress[0].proposal_id is None
    (diagnostic,) = state.terminal_egress[0].claims_diagnostics
    assert diagnostic.kind == "extract_refused" and diagnostic.problem == "basis_unsupported"


@pytest.mark.parametrize("value_source", ["read", "literal", "params"])
def test_query_joined_basis_and_unchanged_constant_handling(
    tmp_path: Path, value_source: str
) -> None:
    from tests.test_pilot.support import kev_spec, pilot_world

    base = kev_spec().model_dump(mode="python", by_alias=True)
    base["procedure"] = "test.query-joined"
    base["params"]["constant"] = "string{max_len: 256}"
    base["steps"].insert(
        -1,
        {
            "transform": "keyed",
            "kind": "shape",
            "spec": {
                "input": "$steps.entries.records",
                "fields": {"cve_id": {"from": "$item.key.cve_id"}},
            },
        },
    )
    base["steps"].insert(
        -1,
        {
            "transform": "joined",
            "kind": "join",
            "spec": {
                "left": "$steps.keyed.records",
                "right": "$steps.watched.rows",
                "left_field": "cve_id",
                "right_field": "cve_id",
                "right_fields": {"from_read": "subject_id"},
            },
        },
    )
    base["steps"].insert(
        -1,
        {
            "transform": "typed",
            "kind": "shape",
            "spec": {
                "input": "$steps.joined.records",
                "fields": {
                    "from_read": {"from": "$item.fields.from_read", "as": "string{max_len: 256}"}
                },
            },
        },
    )
    terminal = base["steps"][-1]
    terminal["for_each"] = "$steps.typed.records"
    terminal["subject"]["key"] = {"cve_id": "CVE-2026-0001"}
    value = {
        "read": "$item.fields.from_read",
        "literal": "A declared constant.",
        "params": "$params.constant",
    }[value_source]
    terminal["claims"] = [{"type": KIND + ".notes", "value": value}]
    instance, root = pilot_world(tmp_path, "kev.json")
    # One matched row keeps a constant from restating the same slot twice.
    document = json.loads((root / "data/kev.json").read_text())
    document["vulnerabilities"] = document["vulnerabilities"][:1]
    document["count"] = 1
    (root / "data/kev.json").write_text(json.dumps(document))
    from cruxible_client.contracts.claim_types import render_claim_type

    note = claim_type(KIND + ".notes", "string", evidence_requirement="none")
    accept_tree(instance, {claim_type_path(note.predicate): render_claim_type(note)}, name="notes")
    base["steps"][-1]["claims"][0]["type"] = note.predicate
    spec = procedure_spec(base=base, reference_text=True)
    install(
        instance,
        spec,
        line_name="test.joined",
        parameters={
            "logical_source": "commerce.production.orders",
            "relative_path": "data/kev.json",
            "constant": "A declared parameter.",
        },
    )
    state = run(instance, root, line="test.joined")
    assert state.status == "succeeded", state.terminal
    if value_source == "read":
        assert state.terminal_egress[0].proposal_id is None
        assert {d.problem for d in state.terminal_egress[0].claims_diagnostics} == {
            "basis_unsupported"
        }
    else:
        (claim,) = candidate_claims(instance, state)
        assert claim.backing.procedure_evidence.value.basis == "extracted"
        assert claim.statement.object.value == (
            value if value_source == "literal" else "A declared parameter."
        )


def test_diagnostics_are_typed_bounded_and_not_reposted(tmp_path: Path) -> None:
    from cruxible_client.contracts.claim_evidence import ClaimsDiagnostic

    instance, root = claims_world(
        tmp_path, {"entries": [{"id": f"CVE-2026-{i:04d}", "status": "Maybe"} for i in range(1, 6)]}
    )
    install(
        instance,
        claims_spec(mappings=[{"type": PREDICATE, "value": "$item.fields.status"}] * 201),
        findings=("extract_refused",),
    )
    first = run(instance, root)
    assert first.status == "succeeded", first.terminal
    (egress,) = first.terminal_egress
    assert egress.claims_diagnostics_count == 1005
    assert len(egress.claims_diagnostics) == 100 and egress.claims_diagnostics_truncated == 905
    assert egress.claims_diagnostics_inbox_truncation.selected == 1005
    assert egress.claims_diagnostics_inbox_truncation.posted == 1000
    before = _inbox(instance)
    assert len(before) == 5
    diagnostics = [ClaimsDiagnostic.model_validate(item.payload) for item in before]
    assert {d.target for d in diagnostics} == {
        KIND + ':{"cve_id":"CVE-2026-' + f"{i:04d}" + '"}:' + PREDICATE for i in range(1, 6)
    }
    store = LineInboxStore(instance)
    with store.locked() as conn:
        sequence = conn.execute("SELECT sequence FROM progress").fetchone()[0]
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    assert _inbox(instance) == before
    with store.locked() as conn:
        assert conn.execute("SELECT sequence FROM progress").fetchone()[0] == sequence


def test_diagnostics_are_not_posted_before_refused_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.procedures.proposal_delivery import (
        ProposalDeliveryRefused,
        ProposalTerminalEgressSink,
    )

    note = claim_type(KIND + ".note", "string")
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": "CVE-2026-0001", "status": "Maybe", "note": "valid"}]},
        types=(claim_type(), note),
    )
    install(
        instance,
        claims_spec(
            mappings=[
                {"type": PREDICATE, "value": "$item.fields.status"},
                {"type": note.predicate, "value": "$item.fields.note"},
            ]
        ),
    )

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ProposalDeliveryRefused("proposal_item_invalid", "refused delivery")

    monkeypatch.setattr(ProposalTerminalEgressSink, "prepare_terminal_egress", refuse)
    state = run(instance, root)
    assert state.status == "node_refused"
    assert state.findings_count == 1
    assert state.findings[0].kind == "extract_refused"
    assert not _inbox(instance)


def test_many_value_canonical_refresh_respects_protected_holder(tmp_path: Path) -> None:
    from cruxible_client.contracts.claim_types import ClaimPrecedence
    from cruxible_client.contracts.policies import ClaimResolutionPolicy

    ct = claim_type(
        value="string",
        canonical_form=("casefold",),
        cardinality="many",
        precedence=ClaimPrecedence(revisable_by_procedures=()),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="many", eligible_verdicts=("supported",), selector="all"
        ),
    )
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": "CVE-2026-0001", "status": "open", "note": "open"}]},
        types=(ct,),
    )
    install(instance, claims_spec(mappings=[{"type": PREDICATE, "value": "$item.fields.note"}]))
    activate(instance, run(instance, root))
    before = instance.accepted_coordinate()
    (root / "data/doc.json").write_text(
        json.dumps({"entries": [{"id": "CVE-2026-0001", "status": "open", "note": "OPEN"}]})
    )
    state = run(instance, root, minute=1)
    assert state.status == "succeeded", state.terminal
    assert state.terminal_egress[0].proposal_id is None
    assert not state.terminal_egress[0].claims_diagnostics
    assert state.terminal_egress[0].claims_same_count == 1
    assert instance.accepted_coordinate() == before
    assert not _inbox(instance)


def test_incomplete_source_names_only_own_aliases() -> None:
    from cruxible_core.procedures.completeness import Completeness

    completeness = Completeness(incomplete={"own", "unrelated"})
    assert completeness.incomplete_inputs((("own", ()), ("complete", ()))) == ["own"]


def test_inbox_reasons_stay_with_their_own_node() -> None:
    from cruxible_client.contracts.procedures.models import InboxEgressNode
    from cruxible_client.contracts.shapes import Contract
    from cruxible_core.procedures.execution import _inbox_items, _RunState

    contract = Contract.model_validate(
        {
            "name": "test.requires_complete_input",
            "version": 1,
            "fields": {
                "status": {
                    "type": "enum[open, closed, unknown]",
                    "requires_complete_input": ["closed"],
                    "incomplete_fallback": "unknown",
                },
            },
        }
    )
    state = _RunState(outputs={"own": {"status": "closed"}}, input_payload={}, parameters={})
    state.completeness.incomplete.update({"own", "unrelated"})
    for node_id in ("first", "second"):
        node = InboxEgressNode(
            node_id=node_id, contract=contract.ref, fields={"status": "$steps.own.status"}
        )
        values, _tokens = _inbox_items(
            node, state=state, contract=contract, resolve=lambda _ref: None, enums=None, keys=None
        )
        assert values == [{"status": "unknown"}]
    assert set(state.claims_diagnostics) == {"first", "second"}
    for reasons in state.claims_diagnostics.values():
        assert reasons.count == 1
        assert reasons.reasons[0].input == "own"
        assert reasons.findings == reasons.reasons


def measured_line(tmp_path: Path) -> tuple[Any, Path, Any]:
    """A settle Line whose one Claim value a provider operation computes (basis measured)."""

    score = claim_type(KIND + ".score", "int", value_type={"type": "integer"})
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}, types=(score,)
    )
    base = claims_spec().model_dump(mode="python", by_alias=True)
    base["slots"]["compute"] = {"in": "test.value@1", "out": "test.value@1", "effect": "pure"}
    base["hard_caps"]["provider_calls"] = 2
    base["steps"].insert(-1, {"call": "computed", "slot": "compute", "input": {"value": 1}})
    base["steps"].insert(
        -1,
        {
            "project": "rows",
            "fields": {
                "rows": [
                    {
                        "key": "CVE-2026-0001",
                        "value": "$steps.computed.value",
                        "observation": "$steps.entries.header",
                    }
                ]
            },
        },
    )
    base["steps"][-1].update(
        for_each="$steps.rows.rows",
        subject={"kind": KIND, "key": {"cve_id": "$item.key"}},
        claims=[{"type": score.predicate, "value": "$item.value"}],
    )
    spec = procedure_spec(base=base, reference_text=True)
    install(instance, spec, authority="settle")
    return instance, root, score


def run_measured(instance: Any, root: Path, *, minute: int = 1, value: int = 1) -> Any:
    """Run the measured Line once; the provider answers ``value + 1``."""

    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.test_procedures.test_completeness_transfer import _Clock

    return service_run_playbill_line(
        instance,
        path_identity_digest="test.claims-line",
        request=LineRunRequest(line="test.claims-line"),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=_Clock(NOW + timedelta(minutes=minute)),
        provider_runtime_operator=Operator(_MixedInvoker(output=lambda _c: {"value": value + 1})),
        workspace_file_reader=_reader(instance, root),
    )


@pytest.mark.parametrize("listed", (False, True))
def test_measured_receipt_settles_only_when_the_scope_lists_measured(
    tmp_path: Path, listed: bool
) -> None:
    from tests.support.mandates import scope_changes

    instance, root, score = measured_line(tmp_path)
    basis = ("extracted", "measured") if listed else ("extracted",)
    _grant_settle(
        instance, "test.claims-line", score.predicate, changes=scope_changes("create", basis=basis)
    )
    before = instance.accepted_coordinate()
    state = run_measured(instance, root)
    (measured,) = candidate_claims(instance, state)
    receipt = measured.backing.procedure_evidence.value
    assert receipt.basis == "measured"
    assert receipt.operation == "compute" and receipt.build.target.kind == "Provider"
    tree = instance.tree_at(before.git_oid)
    line = parse_line_spec(
        tree[line_spec_path("test.claims-line")], path=line_spec_path("test.claims-line")
    )
    assert receipt.build == line.bindings["compute"].build
    assert receipt.capture_digest == measured.backing.capture_digests[0]
    assert receipt.enumerates is None and receipt.declared_fields == {}
    assert receipt.field_path == "$item.value" and receipt.transform_node_ids == ()
    if listed:
        assert state.terminal_egress[0].settle_outcome == "settled"
        assert instance.accepted_coordinate() != before
        return
    assert state.terminal_egress[0].settle_outcome == "proposed"
    # A measured value settles only under a scope that lists measured.
    (gap,) = state.terminal_egress[0].outside_scope
    assert (gap.claim_type, gap.change, gap.basis, gap.term) == (
        f"ClaimType:{score.predicate}",
        "create",
        "measured",
        "basis",
    )
    assert instance.accepted_coordinate() == before


def test_subjects_expected_refuses_a_claims_node_without_within(tmp_path: Path) -> None:
    """``subjects: expected`` holds structurally only where every write is fenced."""

    instance, _root, score = measured_line(tmp_path)
    with pytest.raises(AssertionError) as refused:
        _grant_settle(instance, "test.claims-line", score.predicate, subjects="expected")
    assert "cruxible.procedure_mandate.subjects_unfenced" in str(refused.value)


def test_within_is_resolved_once_for_all_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.procedures import execution
    from tests.test_pilot.support import pilot_world

    instance, root = pilot_world(tmp_path, "kev.json")
    original = execution._resolve_template
    calls = 0

    def counted(value: object, **kwargs: Any) -> Any:
        nonlocal calls
        if value == "$steps.watched.rows":
            calls += 1
        return original(value, **kwargs)

    monkeypatch.setattr(execution, "_resolve_template", counted)
    assert len(candidate_claims(instance, run(instance, root))) == 8
    assert calls == 1
