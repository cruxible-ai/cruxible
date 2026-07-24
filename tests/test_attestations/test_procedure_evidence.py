"""D6 declared typed-output evidence coverage."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.types import ProcedureDefinition, ProcedureExecutionResult
from cruxible_core.service import (
    service_accept_procedure,
    service_attest,
    service_lock,
    service_propose_procedure,
    service_run_procedure,
)
from tests.test_attestations.conftest import actor, add_live_claim


def _definition(
    name: str,
    *,
    evidence_outputs: list[str] | None = None,
) -> ProcedureDefinition:
    payload: dict[str, object] = {
        "name": name,
        "contract_in": "ProcedureInput",
        "steps": [
            {
                "id": "first-step",
                "shape_items": {
                    "items": [{"value": "$input.value"}],
                    "fields": {"value": "$item.value"},
                },
                "as": "first",
            },
            {
                "id": "final-step",
                "shape_items": {
                    "items": [{"value": "$steps.first.items[0].value"}],
                    "fields": {"value": "$item.value"},
                },
                "as": "final",
            },
        ],
        "returns": "final",
        "precondition": {},
        "budget": {"wall_clock_s": 10, "max_provider_calls": 0},
    }
    if evidence_outputs is not None:
        payload["evidence_outputs"] = evidence_outputs
    return ProcedureDefinition.model_validate(payload)


def _run(
    instance: CruxibleInstance,
    definition: ProcedureDefinition,
) -> ProcedureExecutionResult:
    service_lock(instance)
    proposed = service_propose_procedure(
        instance,
        definition,
        actor_context=actor("procedure-proposer"),
    )
    live = service_accept_procedure(
        instance,
        proposed.procedure.procedure_id,
        expected_version=1,
        actor_context=actor("procedure-reviewer"),
    )
    return service_run_procedure(
        instance,
        live.procedure.procedure_id,
        {"value": 7},
        actor("procedure-runner"),
    )


def test_definition_rejects_unknown_or_duplicate_evidence_aliases() -> None:
    with pytest.raises(ValidationError, match="unknown step aliases"):
        _definition("unknown-output", evidence_outputs=["missing"])
    with pytest.raises(ValidationError, match="duplicate aliases"):
        _definition("duplicate-output", evidence_outputs=["first", "first"])


def test_default_captures_final_output_only(
    attestation_instance: CruxibleInstance,
) -> None:
    result = _run(attestation_instance, _definition("default-final"))
    assert len(result.evidence_refs) == 1
    ref = result.evidence_refs[0]
    assert ref.source == "procedure_run"
    assert ref.source_record_id == result.run.run_id
    assert ref.label == "final"
    assert ref.artifact_id is not None
    store = attestation_instance.get_procedure_store()
    try:
        artifact = store.get_evidence_artifact(ref.artifact_id)
        assert artifact is not None
        assert artifact.payload == result.output
        assert artifact.payload["items"] == [{"value": 7}]
    finally:
        store.close()


def test_allowlist_persists_only_declared_alias(
    attestation_instance: CruxibleInstance,
) -> None:
    result = _run(
        attestation_instance,
        _definition("declared-first", evidence_outputs=["first"]),
    )
    assert [ref.label for ref in result.evidence_refs] == ["first"]
    store = attestation_instance.get_procedure_store()
    try:
        assert store.list_run_evidence_refs(result.run.run_id) == result.evidence_refs
    finally:
        store.close()


def test_oversized_artifact_keeps_digest_head_and_refuses_attestation(
    attestation_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cruxible_core.service.procedures.MAX_PROCEDURE_EVIDENCE_BYTES", 4)
    result = _run(attestation_instance, _definition("oversized-output"))
    ref = result.evidence_refs[0]
    assert ref.artifact_id is not None
    store = attestation_instance.get_procedure_store()
    try:
        artifact = store.get_evidence_artifact(ref.artifact_id)
        assert artifact is not None
        assert artifact.oversized is True
        assert artifact.payload is None
        assert artifact.truncated_head is not None
    finally:
        store.close()

    add_live_claim(attestation_instance)
    with pytest.raises(ConfigError, match="exceeds the size cap"):
        service_attest(
            attestation_instance,
            relationship_type="protected_by",
            from_type="Service",
            from_id="svc-1",
            to_type="Control",
            to_id="ctl-1",
            stance="support",
            evidence_refs=[ref],
            observed_at=datetime(2026, 7, 24, 11, 0, tzinfo=timezone.utc),
            actor_context=actor("observer"),
        )
