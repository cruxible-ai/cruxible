"""Execution-owned terminal egress, receipt, and item-closure laws."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import cruxible_core.service.procedures.procedure_runs as procedure_run_service
from cruxible_client.contracts.procedures.models import (
    ReadNode,
)
from cruxible_client.contracts.procedures.results import ProcedureNodeRefusal
from cruxible_client.contracts.shapes import Contract
from cruxible_core.exhaust import parse_journal_payload
from cruxible_core.procedures.egress import (
    TerminalEgressChildReceiptV1,
    TerminalEgressReceiptV1,
    TerminalEgressRequestV1,
    compute_effective_rung,
)
from cruxible_core.procedures.execution import ProcedureExecutor
from cruxible_core.procedures.terminal_services import ProposalDeliveryRefused
from cruxible_core.storage.cas import BodyAccessContext
from tests.support.inbox import inbox_node
from tests.support.procedures import named, procedure_definition
from tests.test_procedures.test_procedure_execution import (
    _accepted,
    _Authority,
    _Contracts,
    _digest,
    _fixture,
    _hard_caps,
    _line_bound_prepared,
    _pin,
    _prepare,
    _StateReader,
)

#: The item each row of the read posts: its id.
ITEM = Contract.model_validate(
    {"name": "test.terminal-item", "version": 1, "fields": {"id": {"type": "string"}}}
)


def _terminal_procedure():
    contract_in = _pin("contract-in", "Contract", "terminal-input/v1")
    contract_out = _pin("contract-out", "Contract", "terminal-output/v1")
    query = _pin("query", "QueryDefinition", "terminal-items")
    definition = procedure_definition(
        name="terminal-procedure",
        contract_in=named(contract_in),
        contract_out=named(contract_out),
        nodes=(
            ReadNode(
                node_id="read",
                query=named(query),
                parameters={},
                as_="rows",
                next="inbox",
            ),
            inbox_node(
                fields={"id": "$item.fields.0.value"},
                for_each="$steps.rows.result.rows",
                contract=ITEM,
            ),
        ),
        returns="rows",
        hard_caps=_hard_caps(items=100),
    )
    return _accepted(definition, pins=(contract_in, contract_out, query))


class _InboxSink:
    def __init__(self) -> None:
        self.requests: list[TerminalEgressRequestV1] = []
        self.manifests: list[object] = []

    def deliver_terminal_egress(
        self, *, request: TerminalEgressRequestV1
    ) -> TerminalEgressReceiptV1:
        raise AssertionError("post_inbox is delivered through the inbox door")

    def deliver_inbox_egress(
        self, *, request: TerminalEgressRequestV1, manifests: object
    ) -> TerminalEgressReceiptV1:
        self.requests.append(request)
        self.manifests.append(manifests)
        return TerminalEgressReceiptV1(
            kind=request.kind,
            run_id=request.run_id,
            node_id=request.node_id,
            disposition="posted",
            children=tuple(
                TerminalEgressChildReceiptV1(
                    child_index=item.child_index,
                    item_key=item.item_key,
                    egress_digest=_digest(f"inbox-{item.child_index}"),
                )
                for item in request.items
            ),
        )


class _ProposalRefusingSink:
    def deliver_terminal_egress(self, *, request: TerminalEgressRequestV1) -> None:
        raise ProposalDeliveryRefused(
            "proposal_target_paths_mismatch",
            "Target paths differ from the admitted candidate.",
            details={"target_paths": ["claims/expected.json"]},
        )

    def deliver_inbox_egress(self, *, request: TerminalEgressRequestV1, manifests: object) -> None:
        self.deliver_terminal_egress(request=request)


def _payload(fixture, digest: str) -> object:
    return parse_journal_payload(
        fixture.bodies.read(
            digest,
            access=BodyAccessContext(principal_id="terminal-egress-test", can_read_body=True),
        )
    )


def test_terminal_without_a_sink_still_records_each_item_dependency(tmp_path) -> None:
    root = tmp_path / "no-sink"
    root.mkdir()
    fixture = _fixture(root)
    accepted = _terminal_procedure()
    prepared = _prepare(
        accepted,
        fixture,
        _StateReader({"items": [{"id": "one"}, {"id": "two"}]}),
    )

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        contract_resolver=lambda _ref: ITEM,
    ).execute(prepared, accepted)

    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == "terminal_not_available"
    records = fixture.journal.all_records(fixture.stream, "runs")
    assert [item.record.event_kind for item in records].count("item_dependencies") == 2
    terminal = next(item for item in records if item.record.event_kind == "terminal_egress")
    payload = _payload(fixture, terminal.record.payload_digest)
    assert isinstance(payload, dict)
    assert payload["verdict"] == ("dependencies_bound_egress_pending")


def test_terminal_sink_delivery_is_receipted_after_item_dependencies(tmp_path) -> None:
    root = tmp_path / "with-sink"
    root.mkdir()
    fixture = _fixture(root)
    accepted = _terminal_procedure()
    prepared = _line_bound_prepared(
        fixture,
        _prepare(
            accepted,
            fixture,
            _StateReader({"items": [{"id": "one"}, {"id": "two"}]}),
        ),
    )
    admission = prepared.admission
    rung = compute_effective_rung(
        procedure_terminal_capability=1,
        requested_terminal_rung=1,
        selector_privacies={},
        taint_labels=(),
        procedure_definition_digest=admission.definition_digest,
        line_spec_digest=admission.line_spec_digest or "",
        sensitivity_policy_digest=admission.sensitivity_policy_digest or "",
        mandate_coordinate_digest=admission.mandate_coordinate_digest or "",
        calibration_coordinate_digest=admission.calibration_coordinate_digest or "",
    )
    sink = _InboxSink()

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        contract_resolver=lambda _ref: ITEM,
        effective_rung=rung,
        egress_sink=sink,
    ).execute(prepared, accepted)

    assert result.status == "succeeded"
    assert len(sink.requests) == 1 and len(sink.requests[0].items) == 2
    # The inbox door receives each item's own dependency manifest.
    assert set(sink.manifests[0]) == {item.item_key for item in sink.requests[0].items}  # type: ignore[call-overload]
    records = fixture.journal.all_records(fixture.stream, admission.journal_partition_id)
    kinds = [item.record.event_kind for item in records]
    assert kinds.index("item_dependencies") < kinds.index("terminal_egress")
    terminal = next(item for item in records if item.record.event_kind == "terminal_egress")
    payload = _payload(fixture, terminal.record.payload_digest)
    assert isinstance(payload, dict)
    assert payload["verdict"] == "delivered"
    assert payload["receipt"]["disposition"] == "posted"


def test_proposal_refusal_projects_as_a_repairable_public_node_terminal(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "proposal-refusal"
    root.mkdir()
    fixture = _fixture(root)
    accepted = _terminal_procedure()
    prepared = _line_bound_prepared(
        fixture,
        _prepare(
            accepted,
            fixture,
            _StateReader({"items": [{"id": "one"}]}),
        ),
    )
    admission = prepared.admission
    rung = compute_effective_rung(
        procedure_terminal_capability=1,
        requested_terminal_rung=1,
        selector_privacies={},
        taint_labels=(),
        procedure_definition_digest=admission.definition_digest,
        line_spec_digest=admission.line_spec_digest or "",
        sensitivity_policy_digest=admission.sensitivity_policy_digest or "",
        mandate_coordinate_digest=admission.mandate_coordinate_digest or "",
        calibration_coordinate_digest=admission.calibration_coordinate_digest or "",
    )

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        contract_resolver=lambda _ref: ITEM,
        effective_rung=rung,
        egress_sink=_ProposalRefusingSink(),  # type: ignore[arg-type]
    ).execute(prepared, accepted)

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "proposal_target_paths_mismatch"
    records = fixture.journal.all_records(fixture.stream, admission.journal_partition_id)
    monkeypatch.setattr(
        procedure_run_service,
        "_records_for_run",
        lambda _instance, _run_id: records,
    )
    # The stand-in instance has no inbox to count.
    monkeypatch.setattr(procedure_run_service, "_run_inbox_postings", lambda *_args: (0, 0))
    state = procedure_run_service._state_from_records(  # noqa: SLF001
        SimpleNamespace(body_store=lambda: fixture.bodies),  # type: ignore[arg-type]
        run_id=prepared.admission.run_id,
        receipt=result.receipt,
    )
    assert isinstance(state.terminal, ProcedureNodeRefusal)
    assert state.terminal.code == "proposal_target_paths_mismatch"
    # The live proposal refusal retains its declared repair through journal projection.
    assert state.terminal.repair.hand_edit.required_change == (
        "rebind_the_terminal_from_the_exact_admitted_run"
    )


def test_the_mandate_term_names_whichever_source_reached_the_rung() -> None:
    """A tier-held rung is never attributed to a mandate that grants less."""

    common = {
        "procedure_terminal_capability": 3,
        "requested_terminal_rung": 3,
        "selector_privacies": {},
        "taint_labels": (),
        "procedure_definition_digest": _digest("definition"),
        "line_spec_digest": _digest("line"),
        "sensitivity_policy_digest": _digest("sensitivity"),
        "mandate_coordinate_digest": _digest("mandate-coordinate"),
        "calibration_coordinate_digest": _digest("calibration"),
    }

    tier_wins = compute_effective_rung(
        **common,  # type: ignore[arg-type]
        procedure_mandate_rung=2,
        caller_tier_rung=3,
    )
    term = next(item for item in tier_wins.terms if item.term == "mandate_grant")
    assert term.rung == 3
    assert "authority tier holds settle" in term.reason
    assert "mandate grants propose" in term.reason

    mandate_wins = compute_effective_rung(
        **common,  # type: ignore[arg-type]
        procedure_mandate_rung=3,
        caller_tier_rung=0,
    )
    term = next(item for item in mandate_wins.terms if item.term == "mandate_grant")
    assert term.rung == 3
    assert term.reason == "The exact accepted Procedure mandate grants settle."


@pytest.mark.parametrize("kind", ["post_inbox"])
def test_terminal_fan_out_obeys_run_budget_with_scalar_return(tmp_path, kind):
    from cruxible_client.contracts.procedures.models import (
        ProjectNode,
    )
    from tests.test_procedures.test_procedure_proposal_delivery import item_template

    fixture = _fixture(tmp_path)
    original = _terminal_procedure().procedure.definition
    if kind == "post_inbox":
        terminal = inbox_node(
            fields={"id": "$item.id"}, for_each="$steps.fan.items", contract=ITEM, max_items=1
        )
    definition = procedure_definition(
        base=original,
        nodes=(
            ProjectNode(node_id="summary", literal={"count": 2}, as_="summary", next="fan"),
            ProjectNode(
                node_id="fan",
                literal={
                    "items": [{"id": "a"}, {"id": "b"}]
                    if kind == "post_inbox"
                    else [item_template(), item_template()]
                },
                as_="fan",
                next="inbox",
            ),
            terminal,
        ),
        returns="summary",
        hard_caps=_hard_caps(items=1),
    )
    accepted = _accepted(
        definition,
        pins=(
            _pin("contract-in", "Contract", "terminal-input/v1"),
            _pin("contract-out", "Contract", "terminal-output/v1"),
        ),
    )
    prepared = _prepare(accepted, fixture, _StateReader({}))
    sink = _InboxSink()
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        contract_resolver=lambda _ref: ITEM,
        egress_sink=sink,
    ).execute(prepared, accepted)
    assert result.refusal.code == "budget_max_items_exceeded"
    assert result.refusal.details["observed"] == 2
    assert sink.requests == []
    kinds = [item.record.event_kind for item in fixture.journal.all_records(fixture.stream, "runs")]
    assert "item_dependencies" not in kinds and "terminal_egress" not in kinds
    final = next(
        item
        for item in fixture.journal.all_records(fixture.stream, "runs")
        if item.record.event_kind == "attempt_finalized"
    )
    budget = _payload(fixture, final.record.payload_digest)["budget"]["observed"]["max_items"]
    assert budget["high_water"] == 2
    assert budget["boundary"] == "terminal:inbox"
    assert budget["field_path"] == "items"


def _record_items_over_a_capture_list(tmp_path, *, captures: int, items: int):  # type: ignore[no-untyped-def]
    """Journal one terminal's ``items``, each derived from the same ``captures`` and one own.

    The shape of a list-fed terminal (an OSV-style fetch of one response per
    advisory, every item derived from the whole list): returns each item
    record's payload bytes, what the inline v1 shape would have stored, the
    child receipts, the in-memory manifests and the bytes of every body stored.
    """

    from cruxible_client.contracts.procedures.models import iter_references
    from cruxible_core.exhaust.records import journal_payload_bytes
    from cruxible_core.procedures import execution as execution_module
    from cruxible_core.procedures.terminal_dependencies import (
        AcquisitionInputOutcomeV1,
        DependencyEvidenceFactsV1,
        produced_capture_token,
        receipt_token,
        resolve_item_dependencies,
    )

    tmp_path.mkdir()
    fixture = _fixture(tmp_path)
    accepted = _terminal_procedure()
    prepared = _prepare(accepted, fixture, _StateReader({"items": [{"id": "one"}]}))
    node = next(item for item in accepted.procedure.definition.nodes if item.node_id == "inbox")
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        contract_resolver=lambda _ref: ITEM,
    )
    executor.references = {
        reference.target.qualified: _digest(reference.target.qualified)
        for reference in iter_references(node)
    }
    listed = [_digest(f"capture-{index}") for index in range(captures)]
    own = [_digest(f"own-{index}") for index in range(items)]
    fact = DependencyEvidenceFactsV1(
        epistemic_grade="observed",
        provenance_grade="daemon-fetched",
        acquisition_input_name="osv.advisories",
    )
    shared = frozenset(
        {
            *(produced_capture_token(digest) for digest in listed),
            *(receipt_token(_digest(f"receipt-{digest}")) for digest in listed),
        }
    )
    state = execution_module._RunState(  # noqa: SLF001
        outputs={},
        input_payload={},
        parameters={},
        facts={digest: fact for digest in (*listed, *own)},
        outcomes={
            "osv.advisories": AcquisitionInputOutcomeV1(
                input_name="osv.advisories",
                disposition="acquired",
                capture_digests=tuple(sorted((*listed, *own))),
            )
        },
        claims_items={
            "inbox": (
                [{"id": f"item-{index}"} for index in range(items)],
                tuple(shared | {produced_capture_token(own[index])} for index in range(items)),
            )
        },
    )
    records: list[object] = []
    # The run is open, as execute() opens it, before its terminal journals items.
    admission = prepared.admission
    executor._append_event(  # noqa: SLF001
        admission,
        records,
        "attempt_started",
        {"attempt": admission.attempt, "admitted_at": admission.admitted_at.isoformat()},
    )
    executor._append_event(  # noqa: SLF001
        admission, records, "admission_bound", prepared.bound_payload().model_dump(mode="json")
    )
    before = {path.name for path in (tmp_path / "cas").rglob("*") if path.is_file()}
    children, _values, manifests, _selected = executor._record_terminal_items(  # noqa: SLF001
        node, admission=prepared.admission, state=state, records=records
    )
    stored = sum(
        path.stat().st_size
        for path in (tmp_path / "cas").rglob("*")
        if path.is_file() and path.name not in before
    )
    access = BodyAccessContext(principal_id="terminal-egress-test", can_read_body=True)
    read = lambda digest: fixture.bodies.read(digest, access=access)  # noqa: E731
    journaled = [
        item
        for item in fixture.journal.all_records(fixture.stream, "runs")
        if item.record.event_kind == "item_dependencies"
    ]
    sizes, inline = [], []
    for record, child in zip(journaled, children, strict=True):
        payload = _payload(fixture, record.record.payload_digest)
        assert isinstance(payload, dict)
        assert payload["tag"] == "playbill-procedure-item-dependencies-v2"
        manifest, derived = resolve_item_dependencies(payload, read)
        # Every reader resolves the same closure the run held, under the same digest.
        assert manifest == manifests[child.item_key]
        assert derived.manifest_digest == child.manifest_digest
        sizes.append(len(read(record.record.payload_digest)))
        inline.append(
            len(
                journal_payload_bytes(
                    {
                        "tag": "playbill-procedure-item-dependencies-v1",
                        "manifest": manifest.model_dump(mode="json"),
                        "derived": derived.model_dump(mode="json"),
                        "audit": payload["audit"],
                    }
                )
            )
        )
    return sizes, inline, stored


def test_an_item_record_does_not_grow_with_the_runs_capture_count(tmp_path) -> None:
    """Notes fix 1a: a list-fed terminal's item records cite the shared list by digest.

    Before, every item record restated every Capture digest of the list three
    times (produced, receipt, coverage): records grew as items x captures.
    """

    small, small_inline, small_stored = _record_items_over_a_capture_list(
        tmp_path / "small", captures=10, items=3
    )
    large, large_inline, large_stored = _record_items_over_a_capture_list(
        tmp_path / "large", captures=300, items=3
    )
    # A record's size is independent of the Capture count (only digest values differ).
    assert small == large
    # The inline shape grew by ~ 3 digests per Capture per item.
    assert min(large_inline) - max(small_inline) > 290 * 3 * 64
    # What the run stores grows once per terminal (the shared set), never per item.
    assert large_stored - small_stored < (300 - 10) * 3 * 80
    print(
        f"item record bytes: v2 {small[0]} at 10 and {large[0]} at 300 captures; "
        f"v1 {small_inline[0]} -> {large_inline[0]}; "
        f"terminal stored {small_stored} -> {large_stored}"
    )
