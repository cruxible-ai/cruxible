"""Deterministic conservative Procedure guard builder tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.canonical import ArtifactDigest, typed_digest
from cruxible_core.playbill.captures import CanonicalDurationV1
from cruxible_core.playbill.compiler import current_compiler_coordinate
from cruxible_core.playbill.procedures.artifacts import (
    ProcedureArtifactV1,
    render_procedure,
)
from cruxible_core.playbill.procedures.authoring import (
    AcceptedClaimGuardBuilderV1,
    ExhaustGuardBuilderV1,
    SourceCaptureGuardBuilderV1,
    build_accepted_claim_guard,
    build_exhaust_guard,
    build_source_capture_guard,
)
from cruxible_core.playbill.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_core.playbill.procedures.models import (
    ExhaustTapNodeV3,
    ProcedureBudgetV3,
    ProcedureHardCapsV3,
    SourceNodeV3,
    StateTapNodeV3,
)
from cruxible_core.playbill.proposals import evaluate_proposal_tree
from tests.test_playbill._support import initialize_local


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-builder-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _common() -> dict[str, object]:
    return {
        "name": "release-guard",
        "contract_in": _pin("contract-in", "Contract", "empty-input"),
        "contract_out": _pin("contract-out", "Contract", "guard-result"),
        "observed_path": ("status",),
        "expected_value": "released",
        "refusal_code": "release.not_ready",
        "refusal_message": "The exact accepted release status is not released.",
        "budget": ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=1_000_000),
            max_provider_calls=1,
            max_capture_bytes=1024,
            max_items=100,
        ),
        "hard_caps": ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=2_000_000),
            max_provider_calls=2,
            max_capture_bytes=2048,
            max_items=200,
            max_repeat_attempts=1,
        ),
        "authoring_source_digest": _digest("compact-source"),
    }


def test_accepted_claim_builder_expands_to_complete_expert_graph_golden() -> None:
    spec = AcceptedClaimGuardBuilderV1(
        **_common(),
        query=_pin("query", "QueryDefinition", "release-status"),
        claim_type=_pin("claim-type", "ClaimType", "release.status"),
        parameters={"lot": "LOT-001"},
    )
    first = build_accepted_claim_guard(
        spec,
        compiler_rule_digest=current_compiler_coordinate().rule_digest,
    )
    second = build_accepted_claim_guard(
        AcceptedClaimGuardBuilderV1.model_validate(spec.model_dump(mode="json")),
        compiler_rule_digest=current_compiler_coordinate().rule_digest,
    )

    assert first == second
    assert first.expanded_output_digest == (
        "sha256:a37aea91feecdf0df4055f547be86ee1ac2af68c76a9253e5fc7da243035dd49"
    )
    assert [node.kind for node in first.definition.nodes] == ["state_tap", "guard", "project"]
    assert isinstance(first.definition.nodes[0], StateTapNodeV3)
    guard = first.definition.nodes[1]
    assert guard.on_false == "$abort"
    assert guard.refusal_code == "release.not_ready"
    assert first.required_acquisition_policy is None
    assert tuple(mapping.node_id for mapping in first.source_mappings) == (
        "input",
        "guard",
        "project",
    )


def test_source_and_exhaust_builders_preserve_their_distinct_planes() -> None:
    acquisition = _pin(
        "acquisition-policy",
        "SourceAcquisitionPolicy",
        "release-inputs",
    )
    source = build_source_capture_guard(
        SourceCaptureGuardBuilderV1(
            **_common(),
            capture_contract=_pin("capture-contract", "CaptureContract", "erp-release"),
            provider=_pin("provider", "Provider", "erp"),
            acquisition_policy=acquisition,
            request={"lot": "LOT-001"},
        ),
        compiler_rule_digest=current_compiler_coordinate().rule_digest,
    )
    exhaust = build_exhaust_guard(
        ExhaustGuardBuilderV1(
            **_common(),
            reducer_or_query=_pin("reducer", "Reducer", "release-receipts"),
            acquisition_policy=acquisition,
            journal_identity="release-exhaust",
        ),
        compiler_rule_digest=current_compiler_coordinate().rule_digest,
    )

    assert isinstance(source.definition.nodes[0], SourceNodeV3)
    assert isinstance(exhaust.definition.nodes[0], ExhaustTapNodeV3)
    assert source.required_acquisition_policy == acquisition
    assert exhaust.required_acquisition_policy == acquisition
    assert source.definition.nodes[0].kind != exhaust.definition.nodes[0].kind


def test_builders_refuse_remote_state_effects_missing_budgets_and_wrong_interfaces() -> None:
    payload = {
        **_common(),
        "query": _pin("query", "QueryDefinition", "release-status"),
        "claim_type": _pin("claim-type", "ClaimType", "release.status"),
    }
    with pytest.raises(ValidationError, match="literal_error"):
        AcceptedClaimGuardBuilderV1.model_validate({**payload, "read_scope": "remote"})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AcceptedClaimGuardBuilderV1.model_validate({**payload, "effect": "dispatch"})

    missing_budget = dict(payload)
    missing_budget.pop("budget")
    with pytest.raises(ValidationError, match="Field required"):
        AcceptedClaimGuardBuilderV1.model_validate(missing_budget)

    wrong_query = {
        **payload,
        "query": _pin("provider", "Provider", "release-status"),
    }
    with pytest.raises(ValidationError, match="QueryDefinition"):
        AcceptedClaimGuardBuilderV1.model_validate(wrong_query)


def test_expanded_procedure_enters_only_the_ordinary_proposal_receive_path(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    acquisition = _pin(
        "acquisition-policy",
        "SourceAcquisitionPolicy",
        "release-inputs",
    )
    expansion = build_exhaust_guard(
        ExhaustGuardBuilderV1(
            **_common(),
            reducer_or_query=_pin("reducer", "Reducer", "release-receipts"),
            acquisition_policy=acquisition,
            journal_identity="release-exhaust",
        ),
        compiler_rule_digest=current_compiler_coordinate().rule_digest,
    )
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name="release-guard"),
        definition=expansion.definition,
        definition_digest=compute_procedure_definition_digest_v3(expansion.definition).tagged,
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
        pins=expansion.envelope_pins,
        activation_policy="snapshot",
    )
    service = instance.proposal_service()
    current_tree = service.transport.read_tree(instance.inspect().head_oid)
    path = "procedures/release-guard.yaml"
    evaluation = evaluate_proposal_tree(
        base_tree=current_tree,
        current_tree=current_tree,
        proposed_tree={**current_tree, path: render_procedure(procedure)},
        current=instance.accepted_coordinate(),
        bodies=instance.body_store(),
        timestamp="2026-08-16T12:00:00.000000Z",
        rebased=False,
        actor_id="owner",
    )

    assert evaluation.candidate is not None
    assert evaluation.candidate.members[0].artifact_kind == "procedure"
    law_result = evaluation.candidate.law_evidence[0].result
    assert law_result["authoring_expansion"] == expansion.definition.annotations
    assert evaluation.candidate.activation_policy == "snapshot"
    assert service.transport.read_main() == instance.inspect().head_oid
