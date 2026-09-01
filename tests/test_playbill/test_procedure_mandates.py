from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.authoring.models import ProcedureMandateAuthoringPayloadV1
from cruxible_client.contracts.canonical import ArtifactDigest
from cruxible_client.contracts.procedure_mandates import (
    AcceptedProcedureMandateV1,
    ProcedureMandateError,
    ProcedureMandateInvocationV1,
    ProcedureMandateV1,
    evaluate_procedure_mandate,
    evaluate_procedure_mandate_law,
    parse_procedure_mandate,
    procedure_mandate_digest,
    procedure_mandate_evaluation_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.procedures.models import CanonicalDurationV1, ProcedureHardCapsV3
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.preflight import compute_preflight
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.proposals import AuthenticatedActor, evaluate_proposal_tree
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_procedure_artifacts import _artifact, _definition
from tests.test_playbill.test_resolution_contracts import _accept_tree

_D0 = "sha256:" + "0" * 64


def _caps(*, calls: int = 0) -> ProcedureHardCapsV3:
    return ProcedureHardCapsV3(
        max_wall_clock=CanonicalDurationV1(microseconds=2_000_000),
        max_provider_calls=calls,
        max_capture_bytes=0,
        max_items=200,
        max_repeat_attempts=1,
    )


def _procedure() -> AcceptedProcedureV1:
    artifact = _artifact(_definition(terminal_capability=3))
    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest

    return AcceptedProcedureV1(
        path="procedures/triage.json",
        procedure=artifact,
        artifact_digest=procedure_artifact_digest(artifact).tagged,
    )


def _mandate(*, procedure: AcceptedProcedureV1 | None = None) -> ProcedureMandateV1:
    accepted = procedure or _procedure()
    return ProcedureMandateV1(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="triage"),
        procedure=ArtifactPin(
            role="procedure",
            target=accepted.procedure.identity,
            artifact_digest=accepted.artifact_digest,
        ),
        rung=3,
        authority_ceiling=_caps(),
        namespace=("claims", "documents"),
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )


def test_procedure_mandate_round_trip_and_digest() -> None:
    mandate = _mandate()
    content = render_procedure_mandate(mandate)
    assert parse_procedure_mandate(content, path=procedure_mandate_path("triage")) == mandate
    assert procedure_mandate_digest(mandate) == ArtifactDigest.from_tagged(
        procedure_mandate_digest(mandate).tagged
    )
    with pytest.raises(ProcedureMandateError, match="canonical wire"):
        parse_procedure_mandate(content + b"\n", path=procedure_mandate_path("triage"))


def test_procedure_mandate_law_checks_exact_procedure_and_ceiling() -> None:
    procedure = _procedure()
    mandate = _mandate(procedure=procedure)
    accepted = evaluate_procedure_mandate_law(
        mandate,
        path=procedure_mandate_path("triage"),
        predecessor=None,
        procedure=procedure,
    )
    assert accepted.verdict == "accepted"

    widened = mandate.model_copy(update={"authority_ceiling": _caps(calls=1)})
    refused = evaluate_procedure_mandate_law(
        widened,
        path=procedure_mandate_path("triage"),
        predecessor=None,
        procedure=procedure,
    )
    assert refused.diagnostics[0].code == (
        "playbill.procedure_mandate.authority_ceiling_widens_procedure"
    )


def test_procedure_mandate_runtime_refusals_are_complete_and_deterministic() -> None:
    mandate = _mandate()
    digest = procedure_mandate_digest(mandate).tagged
    invocation = ProcedureMandateInvocationV1(
        procedure_identity=mandate.procedure.target,
        procedure_artifact_digest=mandate.procedure.artifact_digest,
        requested_rung=3,
        requested_authority=_caps(),
        target_paths=("claims/aa/CLM-" + "a" * 32 + ".json",),
        evaluation_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        accepted_mandate_digest=digest,
    )
    permitted = evaluate_procedure_mandate(mandate, invocation)
    assert permitted.verdict == "permitted"
    assert procedure_mandate_evaluation_digest(permitted).startswith("sha256:")

    refused = evaluate_procedure_mandate(
        mandate,
        invocation.model_copy(
            update={
                "accepted_mandate_digest": _D0,
                "evaluation_time": mandate.expires_at + timedelta(seconds=1),
                "procedure_artifact_digest": _D0,
                "requested_authority": _caps(calls=1),
                "target_paths": ("principals/root.json",),
            }
        ),
    )
    assert refused.refusal_codes == (
        "procedure_mandate_authority_ceiling_insufficient",
        "procedure_mandate_expired",
        "procedure_mandate_namespace_mismatch",
        "procedure_mandate_procedure_mismatch",
        "procedure_mandate_superseded",
    )
    rung_two = mandate.model_copy(update={"rung": 2})
    rung_refused = evaluate_procedure_mandate(
        rung_two,
        invocation.model_copy(
            update={"accepted_mandate_digest": procedure_mandate_digest(rung_two).tagged}
        ),
    )
    assert rung_refused.refusal_codes == ("procedure_mandate_rung_insufficient",)


def test_procedure_mandate_successor_requires_exact_predecessor() -> None:
    procedure = _procedure()
    first = _mandate(procedure=procedure)
    accepted = AcceptedProcedureMandateV1(
        path=procedure_mandate_path("triage"),
        mandate=first,
        artifact_digest=procedure_mandate_digest(first).tagged,
    )
    successor = first.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=accepted.artifact_digest),
            "expires_at": datetime(2028, 1, 1, tzinfo=timezone.utc),
        }
    )
    assert (
        evaluate_procedure_mandate_law(
            successor,
            path=accepted.path,
            predecessor=accepted,
            procedure=procedure,
        ).verdict
        == "accepted"
    )


def test_procedure_and_mandate_successors_are_one_changeset_relation(tmp_path) -> None:
    instance, _owner = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    base_tree = instance.tree_at(current.git_oid)
    first_procedure = _artifact(_definition(terminal_capability=3))
    first_accepted = AcceptedProcedureV1(
        path="procedures/triage.json",
        procedure=first_procedure,
        artifact_digest=procedure_artifact_digest(first_procedure).tagged,
    )
    first_mandate = _mandate(procedure=first_accepted)
    first_tree = {
        **base_tree,
        first_accepted.path: render_procedure(first_procedure),
        procedure_mandate_path("triage"): render_procedure_mandate(first_mandate),
    }
    initial = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=base_tree,
        proposed_tree=first_tree,
        current=current,
        bodies=instance.body_store(),
        timestamp="2026-09-01T12:00:00.000000Z",
        rebased=False,
        actor_id="owner",
    )
    assert initial.diagnostics == ()

    successor_definition = _definition(terminal_capability=3).model_copy(
        update={"description": "Successor Procedure."}
    )
    successor_procedure = _artifact(successor_definition).model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=first_accepted.artifact_digest,
            )
        }
    )
    successor_digest = procedure_artifact_digest(successor_procedure).tagged
    successor_mandate = first_mandate.model_copy(
        update={
            "procedure": first_mandate.procedure.model_copy(
                update={"artifact_digest": successor_digest}
            ),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(first_mandate).tagged,
            ),
            "expires_at": datetime(2028, 1, 1, tzinfo=timezone.utc),
        }
    )
    successor_tree = {
        **first_tree,
        first_accepted.path: render_procedure(successor_procedure),
        procedure_mandate_path("triage"): render_procedure_mandate(successor_mandate),
    }
    paired = evaluate_proposal_tree(
        base_tree=first_tree,
        current_tree=first_tree,
        proposed_tree=successor_tree,
        current=current,
        bodies=instance.body_store(),
        timestamp="2026-09-01T12:01:00.000000Z",
        rebased=False,
        actor_id="owner",
    )
    assert paired.diagnostics == ()

    unpaired = evaluate_proposal_tree(
        base_tree=first_tree,
        current_tree=first_tree,
        proposed_tree={**first_tree, first_accepted.path: render_procedure(successor_procedure)},
        current=current,
        bodies=instance.body_store(),
        timestamp="2026-09-01T12:02:00.000000Z",
        rebased=False,
        actor_id="owner",
    )
    assert tuple(item.code for item in unpaired.diagnostics) == (
        "playbill.procedure_mandate.successor_pair_required",
    )

    mandate_only = evaluate_proposal_tree(
        base_tree=first_tree,
        current_tree=first_tree,
        proposed_tree={
            **first_tree,
            procedure_mandate_path("triage"): render_procedure_mandate(successor_mandate),
        },
        current=current,
        bodies=instance.body_store(),
        timestamp="2026-09-01T12:03:00.000000Z",
        rebased=False,
        actor_id="owner",
    )
    assert tuple(item.code for item in mandate_only.diagnostics) == (
        "playbill.procedure_mandate.successor_pair_required",
    )


def test_procedure_mandate_authoring_resolves_machine_owned_digests(tmp_path) -> None:
    instance, owner = initialize_local(tmp_path)
    procedure = _artifact(_definition(terminal_capability=3))
    tree = {
        **instance.tree_at(instance.accepted_coordinate().git_oid),
        "procedures/triage.json": render_procedure(procedure),
    }
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-09-01T11:59:00.000000Z",
        proposal_name="seed-procedure",
    )
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: "9" * 32,
        ),
    )
    payload = ProcedureMandateAuthoringPayloadV1(
        name="triage",
        procedure_name="triage",
        rung=2,
        authority_ceiling=_caps(),
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp="2026-09-01T12:00:00.000000Z",
    ).intent
    computed = compute_preflight(
        instance,
        intent=intent,
        actor=actor,
    )
    assert computed.result.verdict == "passed", computed.result.frontier
    assert computed.lowered is not None
    assert "procedure_digest" not in payload.model_fields_set
    mandate = parse_procedure_mandate(
        computed.evaluated_tree[procedure_mandate_path("triage")],
        path=procedure_mandate_path("triage"),
    )
    assert mandate.procedure.artifact_digest == procedure_artifact_digest(procedure).tagged
