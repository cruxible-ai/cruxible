"""U1's own typed refusals reach the wire as 4xx with a runnable repair."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

import cruxible_core.service.playbill_procedure_runs as procedure_run_service
from cruxible_client.contracts.acquisition_policies import (
    IndependentCoherenceV1,
    InputAcquisitionRuleV1,
    SourceAcquisitionPolicyV1,
    acquisition_policy_digest,
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandateV1,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1, render_procedure
from cruxible_client.contracts.procedures.line_specs import (
    LineSpecV1,
    ManualTriggerPolicyV1,
    line_identity_digest,
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.repairs import (
    DECLARED_HAND_EDIT_CHANGES,
    RUNNABLE_REFUSAL_REPAIRS,
)
from cruxible_core.playbill.keys import GeneratedKeyMaterial
from cruxible_core.playbill.procedures.execution import procedure_line_partition
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.settlement import ChangeActorBinding
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_procedure_run_surface import _slotless_procedure


def _acquisition_policy(name: str) -> SourceAcquisitionPolicyV1:
    return SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name=name),
        inputs=(
            InputAcquisitionRuleV1(
                input_name="status",
                requirement="optional",
                permitted_replayability=("exact",),
                on_unavailable="omit_optional",
                on_stale="omit_optional",
                on_oversized="omit_optional",
                on_conflict="refuse",
            ),
        ),
        coherence=IndependentCoherenceV1(),
    )


def _served_line(
    name: str,
    *,
    accepted: AcceptedProcedureV1,
    policy: SourceAcquisitionPolicyV1,
) -> LineSpecV1:
    procedure_pin = ArtifactPin(
        role="procedure",
        target=accepted.procedure.identity,
        artifact_digest=accepted.artifact_digest,
    )
    policy_pin = ArtifactPin(
        role="acquisition-policy",
        target=policy.identity,
        artifact_digest=acquisition_policy_digest(policy).tagged,
    )
    return LineSpecV1(
        identity=ArtifactIdentity(kind="Line", name=name),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={"status": "open"},
        slot_bindings=(),
        trigger_policy=ManualTriggerPolicyV1(),
        acquisition_policy=policy_pin,
        requested_terminal_rung=2,
        budgets={
            "max_capture_bytes": 0,
            "max_items": 100,
            "max_provider_calls": 0,
            "max_wall_clock_microseconds": 1_000_000,
        },
        epsilon={"$decimal": "0.1"},
        pins=tuple(
            sorted(
                (procedure_pin, policy_pin),
                key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest),
            )
        ),
    )


def _line_mandate(accepted: AcceptedProcedureV1) -> ProcedureMandateV1:
    return ProcedureMandateV1(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="served-line-mandate"),
        procedure=ArtifactPin(
            role="procedure",
            target=accepted.procedure.identity,
            artifact_digest=accepted.artifact_digest,
        ),
        rung=2,
        authority_ceiling=accepted.procedure.definition.hard_caps,
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )


_ABSENT = "sha256:" + "c" * 64
_OTHER = "sha256:" + "d" * 64


def _body(digest: str) -> dict[str, object]:
    # No asserted instant: the occurrence's EVALUATION INSTANT is the daemon's.
    return {
        "tag": "playbill-line-run-request-v1",
        "line_identity_digest": digest,
        "occurrence_id": None,
        "evaluation_time": None,
    }


def test_an_unaccepted_line_refuses_typed_over_http_instead_of_a_daemon_fault(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/lines/{_ABSENT}/runs",
        json=_body(_ABSENT),
    )

    assert response.status_code == 404, response.text
    payload = response.json()
    assert payload["error_code"] == "line_not_accepted"
    assert payload["repair"] == {
        "hand_edit": {
            "target": "refusal/line_not_accepted",
            "required_change": DECLARED_HAND_EDIT_CHANGES["line_not_accepted"],
        }
    }


def test_a_route_body_identity_mismatch_refuses_typed_over_http(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/lines/{_ABSENT}/runs",
        json=_body(_OTHER),
    )

    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["error_code"] == "line_identity_mismatch"
    assert payload["repair"] == RUNNABLE_REFUSAL_REPAIRS["line_identity_mismatch"].model_dump(
        mode="json"
    )


def test_the_sibling_procedure_run_route_refuses_typed_in_the_same_family(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """The 500 posture U1 inherited is repaired for the whole surface family."""

    client, instance_id, _reviewer_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/procedures/no-such-procedure/runs",
        json={
            "tag": "playbill-procedure-run-request-v2",
            "evaluation_time": None,
            "input": {},
        },
    )

    assert response.status_code == 404, response.text
    payload = response.json()
    assert payload["error_type"] == "ProcedureNotFound"
    assert payload["message"] != "internal server error"


def test_an_instant_outside_the_daemon_skew_bound_refuses_typed_over_http(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """A due proof the caller controls is not a rate: the bound is served."""

    client, instance_id, _reviewer_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/playbill/lines/{_ABSENT}/runs",
        json={
            "tag": "playbill-line-run-request-v1",
            "line_identity_digest": _ABSENT,
            "occurrence_id": None,
            "evaluation_time": "2099-01-01T00:00:00Z",
        },
    )

    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["error_code"] == "evaluation_instant_skewed"
    assert payload["repair"] == RUNNABLE_REFUSAL_REPAIRS["evaluation_instant_skewed"].model_dump(
        mode="json"
    )


def _accept_members(instance, reviewer_key_path: Path, members, *, timestamp: str) -> None:  # type: ignore[no-untyped-def]
    """Accept artifact members into the served instance's real accepted tree."""

    base = instance.accepted_coordinate()
    candidate_tree = instance.tree_at(base.git_oid)
    candidate_tree.update(members)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="operator", capabilities=("propose",)),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/operator/served-line",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    assert proposed.candidate is not None, proposed.evaluation.diagnostics
    assert proposed.evaluation.evaluated_tree_oid is not None
    reviewer = instance._recovered.head.principals.require_active("reviewer")  # noqa: SLF001
    material = GeneratedKeyMaterial(
        principal=reviewer,
        private_key_path=reviewer_key_path,
        public_key_path=reviewer_key_path.with_suffix(".pub"),
    )
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(proposed.evaluation.evaluated_tree_oid),
        candidate=proposed.candidate,
        approvals=(_sign(material, proposed.candidate.candidate_digest, base.semantic_root),),
        actor_binding=ChangeActorBinding(actor_id="operator"),
        proposal_actor_id="operator",
        sequence=len(instance.accepted_history()),
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def test_a_real_accepted_line_runs_through_the_live_route_with_no_patch(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """The milestone: route -> real service -> real admission, nothing doubled."""

    client, instance_id, reviewer_key_path = playbill_http
    instance = get_playbill_manager().get(instance_id)
    accepted = _slotless_procedure("served-line-triage")
    policy = _acquisition_policy("served-line-inputs")
    line = _served_line("served-line-hourly", accepted=accepted, policy=policy)
    mandate = _line_mandate(accepted)
    _accept_members(
        instance,
        reviewer_key_path,
        {
            accepted.path: render_procedure(accepted.procedure),
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        timestamp="2026-09-03T09:00:00.000000Z",
    )

    identity_digest = line_identity_digest(line.identity)
    response = client.post(
        f"/api/v1/{instance_id}/playbill/lines/{identity_digest}/runs",
        json=_body(identity_digest),
    )

    assert response.status_code == 200, response.text
    state = response.json()
    # Every stage below the route ran for real: the Line resolved out of the
    # accepted tree, its closure and law were evaluated, its accepted mandate
    # granted the rung, the daemon derived the occurrence, and the admission was
    # bound under the Line's own journal partition.
    assert state["procedure_artifact_digest"] == accepted.artifact_digest
    assert state["status"] == "succeeded", state["terminal"]
    assert state["run_id"] is not None
    admissions = procedure_run_service._line_admissions(  # noqa: SLF001
        instance,
        procedure_run_service._accepted_line_by_identity_digest(  # noqa: SLF001
            instance.tree_at(instance.accepted_coordinate().git_oid),
            identity_digest=identity_digest,
        ),
    )
    assert len(admissions) == 1
    assert admissions[0].journal_partition_id == procedure_line_partition(line.identity)
    assert admissions[0].line_spec_digest == line_spec_digest(line).tagged
