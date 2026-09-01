from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.procedure_mandates import procedure_mandate_digest
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.playbill.procedures.egress import (
    MANDATE_FREE_RUNG_CEILING,
    ProcedureProducerReceiptV1,
    TerminalAuthorityRefusal,
    TerminalEgressChildReceiptV1,
    TerminalEgressError,
    TerminalEgressItemV1,
    TerminalEgressReceiptV2,
    TerminalEgressRequestV1,
    TerminalEgressRequestV2,
    build_terminal_egress_request_v2,
    compute_effective_rung,
    procedure_producer_receipt_digest,
    require_procedure_mandate,
    terminal_operation_key,
    verify_terminal_egress_receipt,
)
from cruxible_core.playbill.procedures.execution import (
    ProcedureRunAdmissionV1,
    prepare_direct_procedure_run,
)
from cruxible_core.playbill.procedures.terminal_services import (
    EffectfulTerminalError,
    PlaybillSettlementDoor,
    ProposalTerminalAdapter,
    SettlementCandidateInspection,
    SettlementDoorResultV1,
    SettlementLostCas,
    SettlementTargetV1,
    SettlementTerminalAdapter,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.service.playbill_procedure_runs import SERVED_NODE_KINDS
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_procedure_execution import (
    _actor,
    _coordinate,
    _digest,
    _fixture,
    _line_admission,
    _pin,
    _StateReader,
)
from tests.test_playbill.test_procedure_mandates import _mandate, _procedure

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
    admission: ProcedureRunAdmissionV1,
    item: TerminalEgressItemV1,
    bound: ArtifactPin | None = None,
) -> TerminalEgressRequestV1:
    settlement = kind == "mandate_settlement"
    return TerminalEgressRequestV1(
        kind=kind,
        run_id=admission.run_id,
        node_id="terminal",
        accepted_coordinate=admission.accepted_coordinate,
        procedure_identity=admission.procedure_identity,
        procedure_artifact_digest=admission.procedure_artifact_digest,
        admission_binding_digest=admission.admission_binding_digest,
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
        actor_context=admission.actor_context,
        items=(item,),
        prepared_at=admission.admitted_at,
    )


def _admission(
    tmp_path,
    *,
    coordinate=None,
    actor=None,
    admitted_at: datetime = NOW,
) -> ProcedureRunAdmissionV1:
    root = tmp_path / "admission"
    root.mkdir()
    fixture = _fixture(root)
    accepted = _procedure()
    return prepare_direct_procedure_run(
        accepted,
        instance_id="instance-a",
        run_id=None,
        accepted_coordinate=coordinate or _coordinate(),
        invocation_input={"value": 7},
        actor_context=actor or _actor(),
        state_reader=_StateReader(),
        bodies=fixture.bodies,
        journal_stream=fixture.stream,
        journal_partition_id=None,
        admitted_at=admitted_at,
    ).admission


def _runtime_mandate(
    admission: ProcedureRunAdmissionV1,
    *,
    namespace: tuple[str, ...],
):
    base = _mandate()
    return base.model_copy(
        update={
            "procedure": base.procedure.model_copy(
                update={
                    "target": admission.procedure_identity,
                    "artifact_digest": admission.procedure_artifact_digest,
                }
            ),
            "authority_ceiling": admission.hard_caps,
            "namespace": namespace,
        }
    )


def _effectful_request(
    kind: str,
    *,
    admission: ProcedureRunAdmissionV1,
    item: TerminalEgressItemV1,
    target_paths: tuple[str, ...],
    mandate_digest: str,
    bound: ArtifactPin | None = None,
    prepared_at: datetime | None = None,
):
    base = _base_request(kind, admission=admission, item=item, bound=bound)
    if prepared_at is not None:
        base = base.model_copy(update={"prepared_at": prepared_at})
    return build_terminal_egress_request_v2(
        base,
        admission=admission,
        procedure_mandate_digest=mandate_digest,
        calibration_reading_digests=(),
        target_paths=target_paths,
    )


def test_producer_receipt_is_topological_and_mutation_sensitive(tmp_path) -> None:
    admission = _admission(tmp_path)
    contract = _pin("capture-contract", "CaptureContract", "capture")
    base = _base_request("emit_capture", admission=admission, item=_item("capture"), bound=contract)
    request = build_terminal_egress_request_v2(
        base,
        admission=admission,
        procedure_mandate_digest=None,
        calibration_reading_digests=(),
        target_paths=(),
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


def test_terminal_operation_key_excludes_clocks_and_commits_authority_scope(tmp_path) -> None:
    admission = _admission(tmp_path)
    path = "subjects/project.work_item/wi-1.json"
    mandate_digest = _digest("procedure-mandate")
    request = _effectful_request(
        "propose_change_set",
        admission=admission,
        item=_item(path),
        target_paths=(path,),
        mandate_digest=mandate_digest,
    )
    key = terminal_operation_key(request)
    assert key == terminal_operation_key(
        request.model_copy(
            update={
                "prepared_at": NOW.replace(hour=13),
                "evaluation_time": NOW.replace(hour=14),
            }
        )
    )
    mutations = (
        {"kind": "mandate_settlement"},
        {"target_paths": ("documents/other.md",)},
        {"procedure_mandate_digest": _digest("other-mandate")},
        {"procedure_artifact_digest": _digest("other-procedure")},
    )
    assert all(
        terminal_operation_key(request.model_copy(update=update)) != key for update in mutations
    )


def test_v2_builder_derives_authority_and_uses_monotone_prepared_time(tmp_path) -> None:
    admission = _admission(tmp_path)
    path = "subjects/project.work_item/wi-1.json"
    prepared_at = admission.admitted_at + timedelta(hours=1)
    request = _effectful_request(
        "propose_change_set",
        admission=admission,
        item=_item(path),
        target_paths=(path,),
        mandate_digest=_digest("procedure-mandate"),
        prepared_at=prepared_at,
    )
    assert request.requested_authority == admission.hard_caps
    assert request.evaluation_time == prepared_at
    assert request.mandate_pin is None and request.mandate_basis_digests == ()

    forged = request.model_copy(
        update={
            "requested_authority": request.requested_authority.model_copy(update={"max_items": 1})
        }
    )
    with pytest.raises(TerminalAuthorityRefusal) as caught:
        require_procedure_mandate(forged, admission=admission, accepted_mandates={})
    assert caught.value.codes == ("procedure_authority_admission_mismatch",)
    assert caught.value.repair_kind == "rebind_admission"


def test_v2_refuses_inherited_standing_mandate_authority(tmp_path) -> None:
    admission = _admission(tmp_path)
    path = "claims/aa/CLM-" + "a" * 32 + ".json"
    request = _effectful_request(
        "mandate_settlement",
        admission=admission,
        item=_item("candidate", value=_settlement_target(admission).model_dump(mode="json")),
        target_paths=(path,),
        mandate_digest=_digest("procedure-mandate"),
        bound=_pin("target-law", "ClaimType", "prediction"),
    )
    with pytest.raises(ValueError, match="StandingMandate authority"):
        TerminalEgressRequestV2.model_validate(
            {
                **request.model_dump(mode="python"),
                "mandate_pin": _pin("mandate", "StandingMandate", "settlement"),
                "mandate_basis_digests": (_digest("standing-mandate"),),
            }
        )


def test_non_effectful_mandate_check_names_the_declared_rung_repair(tmp_path) -> None:
    admission = _admission(tmp_path)
    contract = _pin("capture-contract", "CaptureContract", "capture")
    base = _base_request(
        "emit_capture",
        admission=admission,
        item=_item("capture"),
        bound=contract,
    )
    request = build_terminal_egress_request_v2(
        base,
        admission=admission,
        procedure_mandate_digest=None,
        calibration_reading_digests=(),
        target_paths=(),
    )
    with pytest.raises(TerminalAuthorityRefusal) as caught:
        require_procedure_mandate(request, admission=admission, accepted_mandates={})
    assert caught.value.codes == ("procedure_mandate_not_applicable",)
    assert caught.value.repair_kind == "use_declared_rung"
    assert caught.value.repair_command == "Use the terminal's declared rung."


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
    base = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    admission = _admission(
        tmp_path,
        coordinate=base,
        actor=_actor().model_copy(update={"actor_id": "owner"}),
    )
    subject = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-1"),
        subject_kind="project.work_item",
        subject_id="wi-1",
    )
    path = subject_path("project.work_item", "wi-1")
    candidate_tree = {**instance.tree_at(base.git_oid), path: render_subject(subject)}
    mandate = _runtime_mandate(admission, namespace=("subjects",))
    mandate_digest = procedure_mandate_digest(mandate).tagged
    request = _effectful_request(
        "propose_change_set",
        admission=admission,
        item=_item(path),
        target_paths=(path,),
        mandate_digest=mandate_digest,
    )
    adapter = ProposalTerminalAdapter(service=instance.proposal_service())
    assert request.operation_key is not None
    target_ref = (
        "refs/proposals/owner/procedure-" + request.operation_key.removeprefix("sha256:")[:64]
    )
    assert instance.proposal_service().transport.read_proposal_ref(target_ref) is None
    with pytest.raises(TerminalAuthorityRefusal, match="procedure_mandate_required") as caught:
        adapter.deliver(
            request=request,
            admission=admission,
            candidate_tree=candidate_tree,
            accepted_mandates={},
        )
    assert caught.value.codes == ("procedure_mandate_required",)
    assert caught.value.procedure_name == "triage"
    assert caught.value.required_rung == 2
    assert caught.value.target_namespace == (path,)
    assert caught.value.repair_kind == "create_mandate"
    assert caught.value.repair_command == (
        "cruxible playbill authoring create --example procedure-mandate"
    )
    assert instance.proposal_service().transport.read_proposal_ref(target_ref) is None


def test_proposal_adapter_reuses_proposal_service_without_advancing_main(tmp_path) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    admission = _admission(
        tmp_path,
        coordinate=base,
        actor=_actor().model_copy(update={"actor_id": "owner"}),
    )
    subject = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-2"),
        subject_kind="project.work_item",
        subject_id="wi-2",
    )
    path = subject_path("project.work_item", "wi-2")
    candidate_tree = {**instance.tree_at(base.git_oid), path: render_subject(subject)}
    mandate = _runtime_mandate(admission, namespace=("subjects",))
    mandate_digest = procedure_mandate_digest(mandate).tagged
    request = _effectful_request(
        "propose_change_set",
        admission=admission,
        item=_item(path),
        target_paths=(path,),
        mandate_digest=mandate_digest,
    )
    receipt = ProposalTerminalAdapter(service=instance.proposal_service()).deliver(
        request=request,
        admission=admission,
        candidate_tree=candidate_tree,
        accepted_mandates={mandate_digest: mandate},
    )
    verify_terminal_egress_receipt(request, receipt)
    assert receipt.disposition == "received"
    assert AcceptedCoordinate.from_internal(instance.accepted_coordinate()) == base


class _Door:
    def __init__(
        self,
        result: SettlementDoorResultV1,
        inspection: SettlementCandidateInspection,
    ) -> None:
        self.result = result
        self.inspection = inspection
        self.calls = 0
        self.inspect_calls = 0

    def inspect_exact_candidate(self, *, target: SettlementTargetV1):
        self.inspect_calls += 1
        return self.inspection

    def activate_exact_candidate(self, *, target: SettlementTargetV1, actor_id: str):
        self.calls += 1
        assert target.proposal_id == self.result.proposal_id
        assert actor_id == "operator"
        return self.result


def _settlement_target(
    admission: ProcedureRunAdmissionV1,
    *,
    proposal_value: int = 1,
    candidate_value: int = 1,
    base_semantic_root: str | None = None,
) -> SettlementTargetV1:
    proposal_id = typed_digest(ArtifactDigest, "proposal-test-v1", {"value": 1}).tagged
    candidate_digest = typed_digest(
        ArtifactDigest, "candidate-test-v1", {"value": candidate_value}
    ).tagged
    if proposal_value != 1:
        proposal_id = typed_digest(
            ArtifactDigest, "proposal-test-v1", {"value": proposal_value}
        ).tagged
    return SettlementTargetV1(
        proposal_id=proposal_id,
        candidate_digest=candidate_digest,
        base_semantic_root=base_semantic_root or admission.accepted_coordinate.semantic_root,
    )


def _settlement_door(
    target: SettlementTargetV1,
    *,
    target_paths: tuple[str, ...],
    inspection_proposal_id: str | None = None,
    inspection_candidate_digest: str | None = None,
) -> _Door:
    return _Door(
        SettlementDoorResultV1(
            status="accepted",
            proposal_id=target.proposal_id,
            candidate_digest=target.candidate_digest,
        ),
        SettlementCandidateInspection(
            proposal_id=inspection_proposal_id or target.proposal_id,
            candidate_digest=inspection_candidate_digest or target.candidate_digest,
            base_semantic_root=target.base_semantic_root,
            target_paths=target_paths,
        ),
    )


class _SettlementEvidence:
    def __init__(
        self,
        *,
        target: SettlementTargetV1,
        base_oid: str,
        candidate_tree_oid: str,
    ) -> None:
        self.target = target
        self.base_oid = base_oid
        self.candidate_tree_oid = candidate_tree_oid

    def resolve_proposal_id(self, value: str) -> str:
        return value

    def read_admission(self, value: str):
        return SimpleNamespace(proposal_id=value)

    def read_evaluation(self, value: str):
        return SimpleNamespace(
            proposal_id=value,
            candidate_digest=self.target.candidate_digest,
            evaluated_base_oid=self.base_oid,
            evaluated_tree_oid=self.candidate_tree_oid,
        )

    def read_candidate(self, value: str):
        return SimpleNamespace(
            candidate_digest=value,
            candidate=SimpleNamespace(parent_semantic_root=self.target.base_semantic_root),
        )


class _SettlementInstance:
    def __init__(self, *, admission: ProcedureRunAdmissionV1, target: SettlementTargetV1) -> None:
        self.admission = admission
        self.target = target
        self.candidate_tree_oid = "b" * 40
        self.evidence = _SettlementEvidence(
            target=target,
            base_oid=admission.accepted_coordinate.git_oid,
            candidate_tree_oid=self.candidate_tree_oid,
        )
        self.current_coordinate = self._internal_coordinate(admission.accepted_coordinate)

    @staticmethod
    def _internal_coordinate(coordinate: AcceptedCoordinate):
        return SimpleNamespace(
            git_oid=coordinate.git_oid,
            semantic_root=coordinate.semantic_root,
            generation_root=coordinate.generation_root,
            compiler=SimpleNamespace(rule_digest=coordinate.compiler_digest),
        )

    def proposal_evidence(self):
        return self.evidence

    def coordinate_for_oid(self, oid: str):
        assert oid == self.admission.accepted_coordinate.git_oid
        return self._internal_coordinate(self.admission.accepted_coordinate)

    def tree_at(self, oid: str):
        assert oid == self.admission.accepted_coordinate.git_oid
        return {
            "claims/changed.json": b"old",
            "claims/removed.json": b"removed",
            "claims/same.json": b"same",
        }

    def proposal_tree(self, oid: str):
        assert oid == self.candidate_tree_oid
        return {
            "claims/added.json": b"added",
            "claims/changed.json": b"new",
            "claims/same.json": b"same",
        }

    def accepted_coordinate(self):
        return self.current_coordinate


def test_concrete_settlement_door_resolves_at_admission_and_rechecks_activation(
    tmp_path, monkeypatch
) -> None:
    admission = _admission(tmp_path)
    target = _settlement_target(admission)
    instance = _SettlementInstance(admission=admission, target=target)
    required: list[str] = []
    activated: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "cruxible_core.playbill.procedures.terminal_services.principal_registry_from_tree",
        lambda tree, *, semantic_root: SimpleNamespace(
            require_active=lambda actor_id: required.append(actor_id)
        ),
    )
    monkeypatch.setattr(
        "cruxible_core.playbill.procedures.terminal_services.service_activate_playbill_proposal",
        lambda passed_instance, *, proposal_id, activated_by: (
            activated.append((proposal_id, activated_by)) or SimpleNamespace(status="accepted")
        ),
    )
    door = PlaybillSettlementDoor(
        instance=instance,
        admitted_coordinate=admission.accepted_coordinate,
    )

    inspection = door.inspect_exact_candidate(target=target)
    assert inspection.target_paths == (
        "claims/added.json",
        "claims/changed.json",
        "claims/removed.json",
    )
    assert door.activate_exact_candidate(target=target, actor_id="operator").status == "accepted"
    assert required == ["operator"]
    assert activated == [(target.proposal_id, "operator")]

    instance.current_coordinate = instance._internal_coordinate(
        admission.accepted_coordinate.model_copy(update={"git_oid": "c" * 40})
    )
    with pytest.raises(EffectfulTerminalError, match="coordinate_changed"):
        door.activate_exact_candidate(target=target, actor_id="operator")
    assert activated == [(target.proposal_id, "operator")]


def test_settlement_adapter_delegates_only_after_exact_authority(tmp_path) -> None:
    admission = _admission(tmp_path)
    target_path = "claims/aa/CLM-" + "a" * 32 + ".json"
    target = _settlement_target(admission)
    mandate = _runtime_mandate(admission, namespace=("claims",))
    mandate_digest = procedure_mandate_digest(mandate).tagged
    request = _effectful_request(
        "mandate_settlement",
        admission=admission,
        item=_item("candidate", value=target.model_dump(mode="json")),
        target_paths=(target_path,),
        mandate_digest=mandate_digest,
        bound=_pin("target-law", "ClaimType", "prediction"),
    )
    door = _settlement_door(target, target_paths=(target_path,))
    adapter = SettlementTerminalAdapter(door=door)
    with pytest.raises(TerminalAuthorityRefusal):
        adapter.deliver(request=request, admission=admission, accepted_mandates={})
    assert door.calls == 0
    receipt = adapter.deliver(
        request=request,
        admission=admission,
        accepted_mandates={mandate_digest: mandate},
    )
    verify_terminal_egress_receipt(request, receipt)
    assert receipt.disposition == "settled"
    assert door.calls == 1


def test_lost_cas_refuses_with_observed_head_and_reprepare_repair(tmp_path) -> None:
    admission = _admission(tmp_path)
    target_path = "claims/aa/CLM-" + "a" * 32 + ".json"
    target = _settlement_target(admission)
    mandate = _runtime_mandate(admission, namespace=("claims",))
    mandate_digest = procedure_mandate_digest(mandate).tagged
    request = _effectful_request(
        "mandate_settlement",
        admission=admission,
        item=_item("candidate", value=target.model_dump(mode="json")),
        target_paths=(target_path,),
        mandate_digest=mandate_digest,
        bound=_pin("target-law", "ClaimType", "prediction"),
    )
    observed = admission.accepted_coordinate.model_copy(update={"git_oid": "f" * 40})
    door = _settlement_door(target, target_paths=(target_path,))
    door.result = SettlementDoorResultV1(
        status="lost_cas",
        proposal_id=target.proposal_id,
        candidate_digest=target.candidate_digest,
        observed_head=observed,
    )

    with pytest.raises(SettlementLostCas) as caught:
        SettlementTerminalAdapter(door=door).deliver(
            request=request,
            admission=admission,
            accepted_mandates={mandate_digest: mandate},
        )
    assert observed.git_oid in str(caught.value)
    assert "re-prepare at the new head" in str(caught.value)


def test_settlement_refuses_mandate_expired_between_admission_and_egress(tmp_path) -> None:
    admission = _admission(tmp_path, admitted_at=NOW - timedelta(days=10))
    target_path = "claims/aa/CLM-" + "a" * 32 + ".json"
    target = _settlement_target(admission)
    mandate = _runtime_mandate(admission, namespace=("claims",)).model_copy(
        update={"expires_at": NOW - timedelta(days=9)}
    )
    mandate_digest = procedure_mandate_digest(mandate).tagged
    request = _effectful_request(
        "mandate_settlement",
        admission=admission,
        item=_item("candidate", value=target.model_dump(mode="json")),
        target_paths=(target_path,),
        mandate_digest=mandate_digest,
        bound=_pin("target-law", "ClaimType", "prediction"),
        prepared_at=NOW,
    )
    door = _settlement_door(target, target_paths=(target_path,))

    with pytest.raises(TerminalAuthorityRefusal) as caught:
        SettlementTerminalAdapter(door=door).deliver(
            request=request,
            admission=admission,
            accepted_mandates={mandate_digest: mandate},
        )

    assert caught.value.codes == ("procedure_mandate_expired",)
    assert request.evaluation_time == request.prepared_at == NOW
    assert door.calls == 0


def test_settlement_refuses_expired_mandate_with_rewound_v3_admission(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    honest_admission = _line_admission(_procedure(), fixture, admitted_at=NOW)
    rewound_admission = type(honest_admission).model_validate(
        {
            **honest_admission.model_dump(mode="python"),
            "admitted_at": NOW - timedelta(days=30),
        }
    )
    assert rewound_admission.admission_binding_digest == (honest_admission.admission_binding_digest)

    target_path = "claims/aa/CLM-" + "a" * 32 + ".json"
    target = _settlement_target(rewound_admission)
    mandate = _runtime_mandate(rewound_admission, namespace=("claims",)).model_copy(
        update={"expires_at": NOW - timedelta(days=9)}
    )
    mandate_digest = procedure_mandate_digest(mandate).tagged
    request = _effectful_request(
        "mandate_settlement",
        admission=rewound_admission,
        item=_item("candidate", value=target.model_dump(mode="json")),
        target_paths=(target_path,),
        mandate_digest=mandate_digest,
        bound=_pin("target-law", "ClaimType", "prediction"),
        prepared_at=NOW,
    )
    door = _settlement_door(target, target_paths=(target_path,))

    with pytest.raises(TerminalAuthorityRefusal) as caught:
        SettlementTerminalAdapter(door=door).deliver(
            request=request,
            admission=rewound_admission,
            accepted_mandates={mandate_digest: mandate},
        )

    assert caught.value.codes == ("procedure_mandate_expired",)
    assert door.calls == 0


@pytest.mark.parametrize("mismatch", ["base", "paths", "proposal", "candidate"])
def test_settlement_adapter_refuses_candidate_scope_before_activation(tmp_path, mismatch) -> None:
    admission = _admission(tmp_path)
    target_path = "claims/aa/CLM-" + "a" * 32 + ".json"
    target = _settlement_target(
        admission,
        base_semantic_root=(
            _digest("other-semantic-root")
            if mismatch == "base"
            else admission.accepted_coordinate.semantic_root
        ),
    )
    mandate = _runtime_mandate(admission, namespace=("claims",))
    mandate_digest = procedure_mandate_digest(mandate).tagged
    request = _effectful_request(
        "mandate_settlement",
        admission=admission,
        item=_item("candidate", value=target.model_dump(mode="json")),
        target_paths=(target_path,),
        mandate_digest=mandate_digest,
        bound=_pin("target-law", "ClaimType", "prediction"),
    )
    door = _settlement_door(
        target,
        target_paths=(
            ("claims/bb/CLM-" + "b" * 32 + ".json",) if mismatch == "paths" else (target_path,)
        ),
        inspection_proposal_id=(
            typed_digest(ArtifactDigest, "proposal-test-v1", {"value": 2}).tagged
            if mismatch == "proposal"
            else None
        ),
        inspection_candidate_digest=(
            typed_digest(ArtifactDigest, "candidate-test-v1", {"value": 2}).tagged
            if mismatch == "candidate"
            else None
        ),
    )
    with pytest.raises(EffectfulTerminalError, match="settlement_.*_mismatch"):
        SettlementTerminalAdapter(door=door).deliver(
            request=request,
            admission=admission,
            accepted_mandates={mandate_digest: mandate},
        )
    assert door.calls == 0
    assert door.inspect_calls == (0 if mismatch == "base" else 1)


def test_procedure_mandate_refusal_reports_every_failed_law_and_repair(tmp_path) -> None:
    admission = _admission(tmp_path)
    target_path = "claims/aa/CLM-" + "a" * 32 + ".json"
    target = _settlement_target(admission)
    base = _runtime_mandate(admission, namespace=("claims",))
    refused = base.model_copy(
        update={
            "procedure": base.procedure.model_copy(
                update={"artifact_digest": _digest("other-procedure")}
            ),
            "rung": 2,
            "authority_ceiling": base.authority_ceiling.model_copy(update={"max_items": 1}),
            "namespace": ("documents",),
            "expires_at": NOW - timedelta(days=1),
            "lifecycle": base.lifecycle.model_copy(update={"state": "retired"}),
        }
    )
    refused_digest = procedure_mandate_digest(refused).tagged
    request = _effectful_request(
        "mandate_settlement",
        admission=admission,
        item=_item("candidate", value=target.model_dump(mode="json")),
        target_paths=(target_path,),
        mandate_digest=refused_digest,
        bound=_pin("target-law", "ClaimType", "prediction"),
    )
    door = _settlement_door(target, target_paths=(target_path,))
    with pytest.raises(TerminalAuthorityRefusal) as caught:
        SettlementTerminalAdapter(door=door).deliver(
            request=request,
            admission=admission,
            accepted_mandates={refused_digest: refused},
        )
    assert caught.value.codes == (
        "procedure_mandate_authority_ceiling_insufficient",
        "procedure_mandate_expired",
        "procedure_mandate_namespace_mismatch",
        "procedure_mandate_procedure_mismatch",
        "procedure_mandate_rung_insufficient",
        "procedure_mandate_superseded",
    )
    assert caught.value.procedure_name == "triage"
    assert caught.value.required_rung == 3
    assert caught.value.target_namespace == (target_path,)
    assert caught.value.repair_kind == "author_successor"
    assert caught.value.repair_command == (
        "cruxible playbill authoring create --example procedure-mandate"
    )
    assert door.calls == 0


def test_v2_receipt_checks_apply_even_when_request_is_v1(tmp_path) -> None:
    admission = _admission(tmp_path)
    request = _base_request(
        "propose_change_set",
        admission=admission,
        item=_item("subjects/project.work_item/wi-1.json"),
    )
    receipt = TerminalEgressReceiptV2(
        kind="propose_change_set",
        run_id=request.run_id,
        node_id=request.node_id,
        disposition="received",
        children=(
            TerminalEgressChildReceiptV1(
                child_index=0,
                item_key=request.items[0].item_key,
                egress_digest=_digest("candidate-member"),
            ),
        ),
        operation_key=_digest("forged-operation"),
    )
    with pytest.raises(TerminalEgressError, match="v2 terminal receipt"):
        verify_terminal_egress_receipt(request, receipt)
