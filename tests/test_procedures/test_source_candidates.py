"""Typed candidates use ordinary proposal delivery and backend-owned bindings."""

import textwrap

import pytest

from cruxible_client.contracts.claim_type_structure import ClaimTypeStructure
from cruxible_client.contracts.procedures.source_compiler import SourceCompileError, compile_source
from cruxible_client.contracts.procedures.source_program import ProcedureSourceV1, SourceClaimType
from tests.test_procedures import test_procedure_proposal_delivery as delivery
from tests.test_procedures.test_procedure_execution import _budget, _hard_caps
from tests.test_procedures.test_source_compiler import INPUT, OUTPUT


def source_program(value="'high'"):
    return ProcedureSourceV1(
        text=textwrap.dedent(f"""
            def example(request, world):
                subject = world.security.advisory['osv-2026-0001']
                candidate = claim_candidate(
                    subject=subject, predicate=world.claim_type('security.advisory.severity'),
                    value={value}, role='observation',
                    rationale='Author supplied this observation.',
                    self_source='The advisory is high severity.')
                return propose_change_set(candidates=[candidate],
                    result=Output.value(value='proposed'))
        """),
        filename="candidate.py",
        function="example",
        contracts={"Output": OUTPUT},
        subject_kinds=("security.advisory",),
        claim_types={
            "security.advisory.severity": SourceClaimType(
                version="sha256:" + "1" * 64,
                structure=ClaimTypeStructure(
                    predicate="security.advisory.severity",
                    allowed_subject_kinds=("security.advisory",),
                    object_kind="literal",
                    literal_schema={"type": "string", "enum": ["high", "low"]},
                    cardinality="one",
                    permitted_roles=("observation",),
                ),
            )
        },
    )


def test_candidate_compilation_binds_type_and_rejects_wrong_range():
    compiled = compile_source(
        source_program(),
        name="example",
        input=INPUT,
        output=OUTPUT,
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=2,
    )
    terminal = compiled.definition.nodes[-1]
    assert terminal.kind == "propose_change_set"
    assert terminal.claim_types[0].target.qualified == "ClaimType:security.advisory.severity"
    assert terminal.candidate_templates[0]["source_kind"] == "self_source"
    assert "capture_digest" not in terminal.candidate_templates[0]
    with pytest.raises(SourceCompileError) as failure:
        compile_source(
            source_program("'invalid'"),
            name="example",
            input=INPUT,
            output=OUTPUT,
            budget=_budget(),
            hard_caps=_hard_caps(),
            terminal_capability=2,
        )

    assert failure.value.diagnostic.code == "playbill.source.contract_value_invalid"


def test_candidate_binding_rejects_unrelated_evidence_and_preserves_copy_role():
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_core.procedures.source_candidates import bind_source_candidate
    from cruxible_core.procedures.terminal_dependencies import (
        AliasProvenanceV1,
        produced_capture_token,
    )

    captured = produced_capture_token("sha256:" + "a" * 64)
    candidate = dict(
        tag="playbill-source-claim-candidate-v1",
        subject_kind=delivery.SUBJECT_KIND,
        subject_id=delivery.SUBJECT_ID,
        predicate=delivery.PREDICATE,
        value="high",
        role="observation",
        rationale="Copied from the acquired report.",
        source_kind="copied_from",
        source_value={"severity": "high"},
        source_alias="observed",
        basis=[],
    )
    arguments = dict(
        procedure_identity=ArtifactIdentity(kind="Procedure", name="example"),
        procedure_digest="sha256:" + "b" * 64,
        outputs={},
        provenance={"observed": AliasProvenanceV1(whole=frozenset({captured}))},
    )
    result = bind_source_candidate(candidate, item_tokens=frozenset({captured}), **arguments)
    assert result["source"]["capture_digest"] == captured.digest
    assert result["citation_role"] == "copy"
    assert result["statement"]["subject"]["artifact_path"].endswith("osv-2026-0001.json")
    with pytest.raises(ValueError, match="dataflow"):
        bind_source_candidate(candidate, item_tokens=frozenset(), **arguments)


def test_derivation_basis_is_bound_from_the_real_admitted_claim(tmp_path):
    from datetime import datetime

    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.procedures.source_candidates import bind_source_candidate
    from cruxible_core.procedures.terminal_dependencies import (
        AliasProvenanceV1,
        accepted_state_token,
    )
    from cruxible_core.service.procedures.procedures import PlaybillProcedureStateTapReader
    from tests.core_support._knowledge_loop_support import (
        EVALUATION_TIME,
        PREDICATE,
        SUBJECT_KIND,
        seed_claims,
    )

    instance, _ = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    at = AcceptedCoordinate(
        git_oid=coordinate.git_oid,
        semantic_root=coordinate.semantic_root,
        generation_root=coordinate.generation_root,
        compiler_digest=coordinate.compiler.rule_digest,
    )
    with instance.bind_accepted_projection(coordinate) as projection:
        envelope = projection.typed.envelope("ClaimType:" + PREDICATE)
    reader = PlaybillProcedureStateTapReader(
        instance=instance, evaluation_time=datetime.fromisoformat(EVALUATION_TIME)
    )
    value = reader.read_accepted_claim(
        claim_type=ArtifactPin(
            role="claim-type",
            target=ArtifactIdentity(kind="ClaimType", name=PREDICATE),
            artifact_digest=envelope.artifact_digest,
        ),
        subject_kind=SUBJECT_KIND,
        subject_id="wi-42",
        cardinality="one",
        coordinate=at,
    ).value
    token = accepted_state_token("sha256:" + "c" * 64)
    candidate = dict(
        tag="playbill-source-claim-candidate-v1",
        subject_kind=SUBJECT_KIND,
        subject_id="wi-42",
        predicate=PREDICATE,
        value="ready",
        role="derivation",
        rationale="Derived from the admitted basis.",
        source_kind="self_source",
        source_value="Supplied rationale",
        source_alias=None,
        basis=[value],
    )
    arguments = dict(
        procedure_identity=ArtifactIdentity(kind="Procedure", name="example"),
        procedure_digest="sha256:" + "d" * 64,
        outputs={"basis": value},
        provenance={"basis": AliasProvenanceV1(whole=frozenset({token}))},
        item_tokens=frozenset({token}),
    )
    result = bind_source_candidate(candidate, **arguments)
    assert result["derivation"]["inputs"][0]["artifact_digest"] == value["artifact_digest"]
    assert result["derivation"]["procedure"]["target"]["name"] == "example"
    with pytest.raises(ValueError, match="exact admitted"):
        bind_source_candidate({**candidate, "basis": [{**value, "value": "changed"}]}, **arguments)
