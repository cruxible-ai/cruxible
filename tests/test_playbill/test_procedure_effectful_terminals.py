from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.procedure_mandates import procedure_mandate_digest
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.playbill.procedures.egress import (
    MANDATE_FREE_RUNG_CEILING,
    ProcedureProducerReceiptV1,
    TerminalAuthorityRefusal,
    TerminalEgressItemV1,
    TerminalEgressRequestV1,
    build_terminal_egress_request_v2,
    compute_effective_rung,
    procedure_producer_receipt_digest,
    terminal_operation_key,
    verify_terminal_egress_receipt,
)
from cruxible_core.playbill.procedures.terminal_services import (
    ProposalTerminalAdapter,
    SettlementDoorResultV1,
    SettlementTargetV1,
    SettlementTerminalAdapter,
)
from cruxible_core.service.playbill_procedure_runs import SERVED_NODE_KINDS
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_procedure_execution import _actor, _coordinate, _digest, _pin
from tests.test_playbill.test_procedure_mandates import _caps, _mandate

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def test_effectful_terminals_and_post_inbox_remain_dark_until_p2_b_runtime() -> None:
    assert SERVED_NODE_KINDS.isdisjoint(
        {"emit_capture", "post_inbox", "propose_change_set", "mandate_settlement"}
    )


def _item(path: str, *, value: object = "value") -> TerminalEgressItemV1:
    return TerminalEgressItemV1(
        child_index=0,
        item_key=path,
        manifest_digest=_digest("manifest"),
        value=value,
    )


def _base_request(
    kind: str,
    *,
    item: TerminalEgressItemV1,
    bound: ArtifactPin | None = None,
) -> TerminalEgressRequestV1:
    settlement = kind == "mandate_settlement"
    return TerminalEgressRequestV1(
        kind=kind,
        run_id="run-1",
        node_id="terminal",
        accepted_coordinate=_coordinate(),
        procedure_identity=ArtifactIdentity(kind="Procedure", name="triage"),
        procedure_artifact_digest=_digest("procedure"),
        admission_binding_digest=_digest("admission"),
        effective_rung={"emit_capture": 0, "propose_change_set": 2, "mandate_settlement": 3}[kind],
        required_rung={"emit_capture": 0, "propose_change_set": 2, "mandate_settlement": 3}[kind],
        limiting_term="procedure_terminal_capability",
        granted_operation={
            "emit_capture": "compile_capture",
            "propose_change_set": "propose_change_set",
            "mandate_settlement": "activate_change_set",
        }[kind],
        bound_artifact_pin=bound,
        mandate_pin=(_pin("mandate", "StandingMandate", "settlement") if settlement else None),
        mandate_basis_digests=(_digest("standing-mandate"),) if settlement else (),
        actor_context=_actor(),
        items=(item,),
        prepared_at=NOW,
    )


def _effectful_request(
    kind: str,
    *,
    item: TerminalEgressItemV1,
    target_paths: tuple[str, ...],
    mandate_digest: str,
    bound: ArtifactPin | None = None,
):
    base = _base_request(kind, item=item, bound=bound)
    return build_terminal_egress_request_v2(
        base,
        procedure_mandate_digest=mandate_digest,
        calibration_reading_digests=(),
        requested_authority=_caps(),
        target_paths=target_paths,
        evaluation_time=NOW,
    )


def test_producer_receipt_is_topological_and_mutation_sensitive() -> None:
    contract = _pin("capture-contract", "CaptureContract", "capture")
    base = _base_request("emit_capture", item=_item("capture"), bound=contract)
    request = build_terminal_egress_request_v2(
        base,
        procedure_mandate_digest=None,
        calibration_reading_digests=(),
        requested_authority=_caps(),
        target_paths=(),
        evaluation_time=NOW,
    )
    producer = request.producer_receipt
    assert isinstance(producer, ProcedureProducerReceiptV1)
    digest = procedure_producer_receipt_digest(producer)
    assert (
        producer.model_dump(mode="json")
        .keys()
        .isdisjoint({"capture_digests", "terminal_receipt_digest", "run_receipt_digest"})
    )
    assert (
        procedure_producer_receipt_digest(producer.model_copy(update={"run_id": "run-2"})) != digest
    )


def test_terminal_operation_key_excludes_delivery_and_evaluation_clocks() -> None:
    base = _base_request("propose_change_set", item=_item("subjects/project.work_item/wi-1.json"))
    assert terminal_operation_key(base) == terminal_operation_key(
        base.model_copy(update={"prepared_at": NOW.replace(hour=13)})
    )


def test_mandate_free_fold_stops_below_proposal_even_with_a_standing_grant() -> None:
    rung = compute_effective_rung(
        procedure_terminal_capability=3,
        requested_terminal_rung=3,
        selector_privacies={},
        taint_labels=(),
        mandate_grants={},
        calibration_caps=(),
        evaluation_time=NOW,
        procedure_definition_digest=_digest("definition"),
        line_spec_digest=_digest("line"),
        sensitivity_policy_digest=_digest("sensitivity"),
        mandate_coordinate_digest=_digest("mandates"),
        calibration_coordinate_digest=_digest("calibration"),
    )
    assert MANDATE_FREE_RUNG_CEILING == 1
    assert rung.effective_rung == 1
    assert not rung.permits("propose_change_set")


def test_proposal_adapter_checks_authority_before_creating_a_ref(tmp_path) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    subject = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-1"),
        subject_kind="project.work_item",
        subject_id="wi-1",
    )
    path = subject_path("project.work_item", "wi-1")
    candidate_tree = {**instance.tree_at(base.git_oid), path: render_subject(subject)}
    mandate = _mandate().model_copy(
        update={
            "procedure": _mandate().procedure.model_copy(
                update={
                    "target": ArtifactIdentity(kind="Procedure", name="triage"),
                    "artifact_digest": _digest("procedure"),
                }
            ),
            "namespace": ("subjects",),
        }
    )
    mandate_digest = procedure_mandate_digest(mandate).tagged
    request = _effectful_request(
        "propose_change_set",
        item=_item(path),
        target_paths=(path,),
        mandate_digest=mandate_digest,
    ).model_copy(
        update={
            "accepted_coordinate": base,
            "actor_context": _actor().model_copy(update={"actor_id": "owner"}),
        }
    )
    adapter = ProposalTerminalAdapter(service=instance.proposal_service())
    assert request.operation_key is not None
    target_ref = (
        "refs/proposals/owner/procedure-" + request.operation_key.removeprefix("sha256:")[:64]
    )
    assert instance.proposal_service().transport.read_proposal_ref(target_ref) is None
    with pytest.raises(TerminalAuthorityRefusal, match="procedure_mandate_required") as caught:
        adapter.deliver(request=request, candidate_tree=candidate_tree, accepted_mandates={})
    assert caught.value.repair_command == (
        "cruxible playbill authoring create --example procedure-mandate"
    )
    assert instance.proposal_service().transport.read_proposal_ref(target_ref) is None


def test_proposal_adapter_reuses_proposal_service_without_advancing_main(tmp_path) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    subject = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-2"),
        subject_kind="project.work_item",
        subject_id="wi-2",
    )
    path = subject_path("project.work_item", "wi-2")
    candidate_tree = {**instance.tree_at(base.git_oid), path: render_subject(subject)}
    mandate = _mandate().model_copy(
        update={
            "procedure": _mandate().procedure.model_copy(
                update={
                    "target": ArtifactIdentity(kind="Procedure", name="triage"),
                    "artifact_digest": _digest("procedure"),
                }
            ),
            "namespace": ("subjects",),
        }
    )
    mandate_digest = procedure_mandate_digest(mandate).tagged
    request = _effectful_request(
        "propose_change_set",
        item=_item(path),
        target_paths=(path,),
        mandate_digest=mandate_digest,
    ).model_copy(
        update={
            "accepted_coordinate": base,
            "actor_context": _actor().model_copy(update={"actor_id": "owner"}),
        }
    )
    receipt = ProposalTerminalAdapter(service=instance.proposal_service()).deliver(
        request=request,
        candidate_tree=candidate_tree,
        accepted_mandates={mandate_digest: mandate},
    )
    verify_terminal_egress_receipt(request, receipt)
    assert receipt.disposition == "received"
    assert instance.accepted_coordinate() == base


class _Door:
    def __init__(self, result: SettlementDoorResultV1) -> None:
        self.result = result
        self.calls = 0

    def activate_exact_candidate(self, *, target: SettlementTargetV1, actor_id: str):
        self.calls += 1
        assert target.proposal_id == self.result.proposal_id
        assert actor_id == "operator"
        return self.result


def test_settlement_adapter_delegates_only_after_exact_authority() -> None:
    proposal_id = typed_digest(ArtifactDigest, "proposal-test-v1", {"value": 1}).tagged
    candidate_digest = typed_digest(ArtifactDigest, "candidate-test-v1", {"value": 1}).tagged
    target = SettlementTargetV1(
        proposal_id=proposal_id,
        candidate_digest=candidate_digest,
        base_semantic_root=_coordinate().semantic_root,
    )
    mandate = _mandate().model_copy(
        update={
            "procedure": _mandate().procedure.model_copy(
                update={
                    "target": ArtifactIdentity(kind="Procedure", name="triage"),
                    "artifact_digest": _digest("procedure"),
                }
            ),
            "namespace": ("claims",),
        }
    )
    mandate_digest = procedure_mandate_digest(mandate).tagged
    request = _effectful_request(
        "mandate_settlement",
        item=_item("candidate", value=target.model_dump(mode="json")),
        target_paths=("claims/aa/CLM-" + "a" * 32 + ".json",),
        mandate_digest=mandate_digest,
        bound=_pin("target-law", "ClaimType", "prediction"),
    )
    door = _Door(
        SettlementDoorResultV1(
            status="accepted",
            proposal_id=proposal_id,
            candidate_digest=candidate_digest,
        )
    )
    adapter = SettlementTerminalAdapter(door=door)
    with pytest.raises(TerminalAuthorityRefusal):
        adapter.deliver(request=request, accepted_mandates={})
    assert door.calls == 0
    receipt = adapter.deliver(
        request=request,
        accepted_mandates={mandate_digest: mandate},
    )
    verify_terminal_egress_receipt(request, receipt)
    assert receipt.disposition == "settled"
    assert door.calls == 1
