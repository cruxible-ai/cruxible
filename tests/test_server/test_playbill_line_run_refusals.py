"""U1's own typed refusals reach the wire as 4xx with a runnable repair."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import cruxible_core.service.procedures.procedure_runs as procedure_run_service
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedure_mandates import (
    PROCEDURE_MANDATE_CLOCK_SKEW,
    ProcedureMandate,
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure, render_procedure
from cruxible_client.contracts.procedures.line_specs import (
    LineSpec,
    line_identity_digest,
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.repairs import (
    DECLARED_HAND_EDIT_CHANGES,
    RUNNABLE_REFUSAL_REPAIRS,
)
from cruxible_core.claims.closure import DEFERRED_PIN_TARGET_KINDS
from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.procedures.execution import procedure_line_partition
from cruxible_core.procedures.reference_binding import authored_procedure
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.proposals.settlement import ChangeActorBinding
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.config import get_server_state_root
from cruxible_core.service.procedures.procedure_runs import (
    PROCEDURE_RUN_CONFIG_PATH,
    ProcedureRunOperationalConfigV1,
)
from tests.support.procedures import RUN_WALL_CLOCK
from tests.test_ledger.test_activation import _sign
from tests.test_procedures.test_procedure_run_surface import _slotless_procedure


def _served_line(
    name: str,
    *,
    accepted: AcceptedProcedure,
) -> LineSpec:
    """A propose Line over ``accepted`` (no slots to bind, no acquisition overrides)."""

    return LineSpec(
        identity=ArtifactIdentity(kind="Line", name=name),
        occurrence_epoch=1,
        procedure=ArtifactPin(
            role="procedure",
            target=accepted.procedure.identity,
            artifact_digest=accepted.artifact_digest,
        ),
        parameters={"status": "open"},
        max_authority="propose",
        budgets={
            "max_capture_bytes": 0,
            "max_items": 100,
            "max_provider_calls": 0,
            "max_wall_clock_microseconds": RUN_WALL_CLOCK.microseconds,
        },
        epsilon={"$decimal": "0.1"},
    )


def _line_mandate(accepted: AcceptedProcedure, line: LineSpec) -> ProcedureMandate:
    """A propose grant pinning ``line`` exactly (ruling C3)."""

    return ProcedureMandate(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="served-line-mandate"),
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
        grants="propose",
        resource_ceiling=accepted.procedure.definition.hard_caps,
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
        "line": digest,
        "occurrence_id": None,
        "evaluation_time": None,
    }


def test_an_unaccepted_line_refuses_typed_over_http_instead_of_a_daemon_fault(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, _reviewer_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/lines/{_ABSENT}/runs",
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
        f"/api/v1/{instance_id}/lines/{_ABSENT}/runs",
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
        f"/api/v1/{instance_id}/procedures/no-such-procedure/runs",
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
    assert payload["repair"] == {
        "operation": "cruxible.orient",
        "arguments": {"section": "procedures"},
    }
    assert payload["context"] == {"procedure": "no-such-procedure", "nearest": []}


def test_an_unknown_procedure_names_the_nearest_accepted_ones_and_the_repair(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    client, instance_id, reviewer_key_path = playbill_http
    instance = get_playbill_manager().get(instance_id)
    accepted = _slotless_procedure("served-line-triage")
    _accept_members(
        instance,
        reviewer_key_path,
        {accepted.path: render_procedure(authored_procedure(accepted))},
        timestamp="2026-09-03T09:00:00.000000Z",
    )

    response = client.post(
        f"/api/v1/{instance_id}/procedures/served-line-triag/runs",
        json={
            "tag": "playbill-procedure-run-request-v2",
            "evaluation_time": None,
            "input": {},
        },
    )

    assert response.status_code == 404, response.text
    payload = response.json()
    assert payload["error_type"] == "ProcedureNotFound"
    assert payload["context"] == {
        "procedure": "served-line-triag",
        "nearest": ["served-line-triage"],
    }
    assert payload["message"] == (
        "cruxible.procedure.not_found: no accepted Procedure named 'served-line-triag'; "
        "nearest: served-line-triage; run `cruxible orient --section procedures` for every "
        "accepted Procedure"
    )
    assert payload["repair"] == {
        "operation": "cruxible.orient",
        "arguments": {"section": "procedures"},
    }


def test_an_instant_outside_the_daemon_skew_bound_refuses_typed_over_http(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """A due proof the caller controls is not a rate: the bound is served."""

    client, instance_id, _reviewer_key = playbill_http

    response = client.post(
        f"/api/v1/{instance_id}/lines/{_ABSENT}/runs",
        json={
            "tag": "playbill-line-run-request-v1",
            "line": _ABSENT,
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


def test_the_daemon_not_the_caller_configures_the_evaluation_instant_skew(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """The bound a caller's assertion is checked against is operational state.

    An operator whose hosts keep looser time widens it in the daemon's own
    state root; nothing on the wire carries it, and the ratified ProcedureMandate
    skew is what an unconfigured daemon uses.
    """

    client, instance_id, _reviewer_key = playbill_http
    assert ProcedureRunOperationalConfigV1().evaluation_instant_skew == PROCEDURE_MANDATE_CLOCK_SKEW
    asserted = datetime.now(UTC) + timedelta(seconds=60)
    body = {
        "tag": "playbill-line-run-request-v1",
        "line": _ABSENT,
        "occurrence_id": None,
        "evaluation_time": asserted.isoformat().replace("+00:00", "Z"),
    }

    # A minute of drift is inside the default bound, so the run reaches the Line.
    admitted = client.post(f"/api/v1/{instance_id}/lines/{_ABSENT}/runs", json=body)
    assert admitted.status_code == 404, admitted.text
    assert admitted.json()["error_code"] == "line_not_accepted"

    config_path = get_server_state_root() / PROCEDURE_RUN_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_bytes(
        canonical_bytes(
            {
                "tag": "cruxible-procedure-run-operational-config-v1",
                "evaluation_instant_skew_seconds": 1,
            }
        )
    )

    refused = client.post(f"/api/v1/{instance_id}/lines/{_ABSENT}/runs", json=body)
    assert refused.status_code == 400, refused.text
    payload = refused.json()
    assert payload["error_code"] == "evaluation_instant_skewed"
    assert "1s daemon clock skew bound" in payload["message"]
    assert payload["repair"] == RUNNABLE_REFUSAL_REPAIRS["evaluation_instant_skewed"].model_dump(
        mode="json"
    )

    # A configuration that exists and cannot be read is a daemon fault; falling
    # back would silently restore the bound the operator meant to move.
    config_path.write_text("not-json", encoding="utf-8")
    malformed = client.post(f"/api/v1/{instance_id}/lines/{_ABSENT}/runs", json=body)
    assert malformed.status_code == 500, malformed.text
    assert "daemon/procedure-runs.json" in malformed.json()["message"]


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
    assert proposed.candidate is not None, [
        (d.code, d.message, d.subject) for d in proposed.evaluation.diagnostics
    ]
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


def test_a_line_pinning_a_query_definition_runs_through_the_live_route(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """What acceptance admits as a deferred pin, the route cannot refuse.

    A QueryDefinition is pinned by digest and carries no ledger artifact
    envelope, so the closure evaluator admits a Line that pins one with no tree
    member. The route re-checks that closure, and it reads the evaluator's own
    set rather than a copy, or an accepted Line would refuse where it runs.
    """

    client, instance_id, reviewer_key_path = playbill_http
    instance = get_playbill_manager().get(instance_id)
    accepted = _slotless_procedure("query-pinning-triage")
    line = _served_line("query-pinning-hourly", accepted=accepted)
    query_pin = ArtifactPin(
        role="query-definition",
        target=ArtifactIdentity(kind="QueryDefinition", name="query-pinning-open-work"),
        artifact_digest="sha256:" + "e" * 64,
    )
    assert query_pin.target.kind in DEFERRED_PIN_TARGET_KINDS
    line = line.model_copy(
        update={
            "pins": tuple(
                sorted(
                    (*line.pins, query_pin),
                    key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest),
                )
            )
        }
    )
    mandate = _line_mandate(accepted, line)
    _accept_members(
        instance,
        reviewer_key_path,
        {
            accepted.path: render_procedure(authored_procedure(accepted)),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        timestamp="2026-09-03T09:00:00.000000Z",
    )
    # The pinned QueryDefinition really is absent from the accepted tree.
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert [path for path in tree if path.startswith("query-definitions/")] == []

    identity_digest = line_identity_digest(line.identity)
    response = client.post(
        f"/api/v1/{instance_id}/lines/{identity_digest}/runs",
        json=_body(identity_digest),
    )

    assert response.status_code == 200, response.text
    state = response.json()
    assert state["status"] == "succeeded", state["terminal"]


def test_a_real_accepted_line_runs_through_the_live_route_with_no_patch(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """The milestone: route -> real service -> real admission, nothing doubled."""

    client, instance_id, reviewer_key_path = playbill_http
    instance = get_playbill_manager().get(instance_id)
    accepted = _slotless_procedure("served-line-triage")
    line = _served_line("served-line-hourly", accepted=accepted)
    mandate = _line_mandate(accepted, line)
    _accept_members(
        instance,
        reviewer_key_path,
        {
            accepted.path: render_procedure(authored_procedure(accepted)),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        timestamp="2026-09-03T09:00:00.000000Z",
    )

    identity_digest = line_identity_digest(line.identity)
    response = client.post(
        f"/api/v1/{instance_id}/lines/{identity_digest}/runs",
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
        procedure_run_service._accepted_line_by_reference(  # noqa: SLF001
            instance,
            coordinate=instance.accepted_coordinate(),
            reference=identity_digest,
        ),
    )
    assert len(admissions) == 1
    assert admissions[0].journal_partition_id == procedure_line_partition(line.identity)
    assert admissions[0].line_spec_digest == line_spec_digest(line).tagged


@pytest.mark.parametrize("successions", [1, 2])
def test_accepted_line_successors_run_against_their_retained_predecessor(
    playbill_http: tuple[TestClient, str, Path],
    successions: int,
) -> None:
    client, instance_id, reviewer_key_path = playbill_http
    instance = get_playbill_manager().get(instance_id)
    accepted = _slotless_procedure("successor-triage")
    line = _served_line("successor-line", accepted=accepted)
    mandate = _line_mandate(accepted, line)
    _accept_members(
        instance,
        reviewer_key_path,
        {
            accepted.path: render_procedure(authored_procedure(accepted)),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        timestamp="2026-09-03T09:00:00.000000Z",
    )
    for revision in range(successions):
        line = line.model_copy(
            update={
                "parameters": {"status": "closed" if revision == 0 else "open"},
                "lifecycle": ArtifactLifecycle(predecessor_digest=line_spec_digest(line).tagged),
            }
        )
        # A grant never follows a changed Line: its Mandate is carried, propose-only.
        mandate = mandate.model_copy(
            update={
                "line": mandate.line.model_copy(
                    update={"artifact_digest": line_spec_digest(line).tagged}
                ),
                "lifecycle": ArtifactLifecycle(
                    predecessor_digest=procedure_mandate_digest(mandate).tagged
                ),
            }
        )
        _accept_members(
            instance,
            reviewer_key_path,
            {
                line_spec_path(line.identity.name): render_line_spec(line),
                procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
            },
            timestamp=f"2026-09-03T09:0{revision + 1}:00.000000Z",
        )
    identity_digest = line_identity_digest(line.identity)
    response = client.post(
        f"/api/v1/{instance_id}/lines/{identity_digest}/runs",
        json=_body(identity_digest),
    )
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["status"] == "succeeded", state["terminal"]
    assert state["receipt"]["line_spec_digest"] == line_spec_digest(line).tagged
