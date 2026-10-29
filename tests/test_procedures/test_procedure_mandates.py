from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.authoring.models import ProcedureMandateAuthoringPayload
from cruxible_client.contracts.canonical import ArtifactDigest
from cruxible_client.contracts.procedure_mandates import (
    AcceptedProcedureMandate,
    MandateClaimScope,
    MandateCondition,
    ProcedureMandate,
    ProcedureMandateError,
    ProcedureMandateInvocation,
    evaluate_procedure_mandate,
    evaluate_procedure_mandate_v2_law,
    parse_procedure_mandate,
    parse_procedure_mandate_any,
    procedure_mandate_digest,
    procedure_mandate_evaluation_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    procedure_artifact_digest,
    render_procedure,
)
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpec,
    LineSpec,
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import CanonicalDuration, ProcedureHardCaps
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.preflight import compute_preflight
from cruxible_core.authoring.store import AuthoringIntentStore
from cruxible_core.procedures.reference_binding import authored_procedure
from cruxible_core.proposals.proposals import AuthenticatedActor, evaluate_proposal_tree
from tests.core_support._support import initialize_local
from tests.support.procedures import named
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_procedure_artifacts import _artifact, _definition

_D0 = "sha256:" + "0" * 64


def _caps(*, calls: int = 0) -> ProcedureHardCaps:
    return ProcedureHardCaps(
        max_wall_clock=CanonicalDuration(microseconds=2_000_000),
        max_provider_calls=calls,
        max_capture_bytes=0,
        max_items=200,
        max_repeat_attempts=1,
    )


def _closed_definition(*, terminal_capability=3):
    from cruxible_client.contracts.procedures.models import ProjectNode

    template = _definition(terminal_capability=terminal_capability)
    return _definition(
        terminal_capability=terminal_capability,
        nodes=(
            ProjectNode(
                node_id="shape",
                fields={"rows": []},
                contract_out=named(template.contract_out),
                as_="result",
            ),
        ),
    )


def _procedure() -> AcceptedProcedure:
    artifact = _artifact(_closed_definition())
    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest

    return AcceptedProcedure(
        path="procedures/triage.json",
        procedure=artifact,
        artifact_digest=procedure_artifact_digest(artifact).tagged,
    )


def _line(*, procedure: AcceptedProcedure | None = None, name: str = "triage-line") -> LineSpec:
    """One Line over ``procedure`` (no slots to bind: a closed-form, provider-free graph)."""

    accepted = procedure or _procedure()
    caps = accepted.procedure.definition.hard_caps
    return LineSpec(
        identity=ArtifactIdentity(kind="Line", name=name),
        occurrence_epoch=1,
        procedure=ArtifactPin(
            role="procedure",
            target=accepted.procedure.identity,
            artifact_digest=accepted.artifact_digest,
        ),
        parameters={},
        max_authority="settle",
        budgets={
            "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
            "max_provider_calls": caps.max_provider_calls,
            "max_capture_bytes": caps.max_capture_bytes,
            "max_items": caps.max_items,
        },
        epsilon={"$decimal": "0"},
    )


def _accepted_line(line: LineSpec) -> AcceptedLineSpec:
    return AcceptedLineSpec(
        path=line_spec_path(line.identity.name),
        line=line,
        artifact_digest=line_spec_digest(line).tagged,
    )


def _settle_scope() -> tuple[MandateClaimScope, ...]:
    return (
        MandateClaimScope(
            claim_type=ArtifactPin(
                role="claim-type",
                target=ArtifactIdentity(kind="ClaimType", name="security.vulnerability.triaged"),
                artifact_digest=_D0,
            ),
            change_kinds=("create", "revise"),
        ),
    )


def _settle_condition() -> MandateCondition:
    return MandateCondition(
        query=ArtifactPin(
            role="condition-query",
            target=ArtifactIdentity(kind="QueryDefinition", name="triage-condition"),
            artifact_digest=_D0,
        ),
        binding_parameter="subject",
        required_fields=("status",),
        fallback="refuse",
    )


def _mandate(
    *, procedure: AcceptedProcedure | None = None, line: LineSpec | None = None, grants="propose"
) -> ProcedureMandate:
    bound_line = line or _line(procedure=procedure)
    settle_only = (
        {"scope": _settle_scope(), "condition": _settle_condition()} if grants == "settle" else {}
    )
    namespace = ("claims",) if grants == "settle" else ("claims", "documents")
    return ProcedureMandate(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="triage"),
        line=ArtifactPin(
            role="line",
            target=bound_line.identity,
            artifact_digest=line_spec_digest(bound_line).tagged,
        ),
        grants=grants,
        resource_ceiling=_caps(),
        namespace=namespace,
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        **settle_only,
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
    line = _line(procedure=procedure)
    accepted_line = _accepted_line(line)
    mandate = _mandate(line=line)
    accepted = evaluate_procedure_mandate_v2_law(
        mandate,
        path=procedure_mandate_path("triage"),
        predecessor=None,
        line=accepted_line,
        procedure=procedure,
        claim_types={},
        condition_query=None,
    )
    assert accepted.verdict == "accepted"

    widened = mandate.model_copy(update={"resource_ceiling": _caps(calls=1)})
    refused = evaluate_procedure_mandate_v2_law(
        widened,
        path=procedure_mandate_path("triage"),
        predecessor=None,
        line=accepted_line,
        procedure=procedure,
        claim_types={},
        condition_query=None,
    )
    assert refused.diagnostics[0].code == (
        "cruxible.procedure_mandate.resource_ceiling_widens_procedure"
    )


def test_procedure_mandate_runtime_refusals_are_complete_and_deterministic() -> None:
    line = _line()
    mandate = _mandate(line=line, grants="settle")
    digest = procedure_mandate_digest(mandate).tagged
    invocation = ProcedureMandateInvocation(
        line_identity=mandate.line.target,
        line_artifact_digest=mandate.line.artifact_digest,
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
                "line_artifact_digest": _D0,
                "requested_authority": _caps(calls=1),
                "target_paths": ("principals/root.json",),
            }
        ),
    )
    assert refused.refusal_codes == (
        "procedure_mandate_authority_ceiling_insufficient",
        "procedure_mandate_expired",
        "procedure_mandate_line_mismatch",
        "procedure_mandate_namespace_mismatch",
        "procedure_mandate_superseded",
    )
    propose_only = mandate.model_copy(update={"grants": "propose"})
    rung_refused = evaluate_procedure_mandate(
        propose_only,
        invocation.model_copy(
            update={"accepted_mandate_digest": procedure_mandate_digest(propose_only).tagged}
        ),
    )
    assert rung_refused.refusal_codes == ("procedure_mandate_grant_insufficient",)


def test_procedure_mandate_successor_requires_exact_predecessor() -> None:
    procedure = _procedure()
    line = _line(procedure=procedure)
    accepted_line = _accepted_line(line)
    first = _mandate(line=line)
    accepted = AcceptedProcedureMandate(
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
        evaluate_procedure_mandate_v2_law(
            successor,
            path=accepted.path,
            predecessor=accepted,
            line=accepted_line,
            procedure=procedure,
            claim_types={},
            condition_query=None,
        ).verdict
        == "accepted"
    )


def test_line_and_mandate_successors_are_one_changeset_relation(tmp_path) -> None:
    """Ruling C3, restated: a Mandate retargets only with its Line's exact
    successor in the same ChangeSet, and the carried successor is propose-only
    (the Procedure itself need not change -- it is the Line, not the
    Procedure, that a Mandate pins)."""

    instance, _owner = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    base_tree = instance.tree_at(current.git_oid)
    procedure = _procedure()
    first_line = _line(procedure=procedure)
    first_mandate = _mandate(line=first_line, grants="propose")
    first_tree = {
        **base_tree,
        procedure.path: render_procedure(authored_procedure(procedure)),
        line_spec_path(first_line.identity.name): render_line_spec(first_line),
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

    successor_line = first_line.model_copy(
        update={
            "parameters": {"changed": True},
            "lifecycle": ArtifactLifecycle(predecessor_digest=line_spec_digest(first_line).tagged),
        }
    )
    successor_digest = line_spec_digest(successor_line).tagged
    successor_mandate = first_mandate.model_copy(
        update={
            "line": first_mandate.line.model_copy(update={"artifact_digest": successor_digest}),
            "grants": "propose",
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(first_mandate).tagged,
            ),
            "expires_at": datetime(2028, 1, 1, tzinfo=timezone.utc),
        }
    )
    successor_tree = {
        **first_tree,
        line_spec_path(first_line.identity.name): render_line_spec(successor_line),
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
        proposed_tree={
            **first_tree,
            line_spec_path(first_line.identity.name): render_line_spec(successor_line),
        },
        current=current,
        bodies=instance.body_store(),
        timestamp="2026-09-01T12:02:00.000000Z",
        rebased=False,
        actor_id="owner",
    )
    assert tuple(item.code for item in unpaired.diagnostics) == (
        "cruxible.procedure_mandate.successor_pair_required",
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
        "cruxible.procedure_mandate.successor_pair_required",
    )

    # NOTE: a real "carried settle grant must downgrade to propose" case
    # (cruxible.procedure_mandate.carried_grant_must_propose) needs a settle
    # Mandate, which needs a resolvable ClaimType + condition QueryDefinition
    # pin pair accepted in the tree (ProcedureMandate's own `_shape` validator
    # and the generic change-set pin closure both require it) -- not built
    # here for time; left as a follow-up rather than faked with an
    # unresolvable placeholder pin.


def test_procedure_mandate_authoring_resolves_machine_owned_digests(tmp_path) -> None:
    instance, owner = initialize_local(tmp_path)
    procedure = _artifact(_closed_definition())
    procedure_accepted = AcceptedProcedure(
        path="procedures/triage.json",
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    line = _line(procedure=procedure_accepted)
    tree = {
        **instance.tree_at(instance.accepted_coordinate().git_oid),
        "procedures/triage.json": render_procedure(procedure),
        line_spec_path(line.identity.name): render_line_spec(line),
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
    payload = ProcedureMandateAuthoringPayload(
        name="triage",
        line_name=line.identity.name,
        grants="propose",
        resource_ceiling=_caps(),
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
    assert "line_digest" not in payload.model_fields_set
    mandate = parse_procedure_mandate_any(
        computed.evaluated_tree[procedure_mandate_path("triage")],
        path=procedure_mandate_path("triage"),
    )
    assert mandate.line.artifact_digest == line_spec_digest(line).tagged
