"""Typed candidates use ordinary proposal delivery and backend-owned bindings.

A ``playbill-source-claim-candidate-v1`` template is a proposal item the
executor binds to the run's exact evidence and admitted Claim reads.
"""

import pytest

from tests.test_procedures import test_procedure_proposal_delivery as delivery


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
    revision = bind_source_candidate(
        {
            **candidate,
            "revision_claim": value,
            "dispositions": [{"claim": value, "disposition": "not_tested"}],
        },
        **arguments,
    )
    assert revision["revises"] == value["claim_id"]
    assert revision["existing_claim_dispositions"][0]["claim_id"] == value["claim_id"]
    for field in ("revision_claim", "dispositions"):
        forged = {**value, "value": "changed"}
        entry = (
            forged if field == "revision_claim" else [{"claim": forged, "disposition": "support"}]
        )
        with pytest.raises(ValueError, match="exact admitted"):
            bind_source_candidate({**candidate, field: entry}, **arguments)
    with pytest.raises(ValueError, match="exact admitted"):
        bind_source_candidate({**candidate, "basis": [{**value, "value": "changed"}]}, **arguments)


def test_nested_capture_selection_rejects_unrelated_handles():
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_core.procedures.source_candidates import bind_source_candidate
    from cruxible_core.procedures.terminal_dependencies import (
        AliasProvenanceV1,
        produced_capture_token,
    )

    observation = produced_capture_token("sha256:" + "a" * 64)
    registered = produced_capture_token("sha256:" + "b" * 64)
    tokens = frozenset({observation, registered})
    candidate = dict(
        tag="playbill-source-claim-candidate-v1",
        subject_kind=delivery.SUBJECT_KIND,
        subject_id=delivery.SUBJECT_ID,
        predicate=delivery.PREDICATE,
        value="high",
        role="observation",
        rationale="The child registered this observation.",
        source_kind="supported_by",
        source_value={"capture_digest": registered.digest},
        source_alias="child.terminal.capture",
    )
    args = dict(
        procedure_identity=ArtifactIdentity(kind="Procedure", name="parent"),
        procedure_digest="sha256:" + "c" * 64,
        outputs={},
        provenance={"child": AliasProvenanceV1(whole=tokens)},
        item_tokens=tokens,
    )
    result = bind_source_candidate(candidate, **args)
    assert result["source"]["capture_digest"] == registered.digest
    with pytest.raises(ValueError, match="one verified Capture"):
        bind_source_candidate(
            {**candidate, "source_value": {"capture_digest": "sha256:" + "d" * 64}}, **args
        )


def _mixed_closure_candidate(selection: dict[str, object] | str):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_core.procedures.terminal_dependencies import (
        AliasProvenanceV1,
        admitted_capture_token,
        produced_capture_token,
    )

    admitted = admitted_capture_token("sha256:" + "a" * 64)
    produced = produced_capture_token("sha256:" + "b" * 64)
    tokens = frozenset({admitted, produced})
    candidate = dict(
        tag="playbill-source-claim-candidate-v1",
        subject_kind=delivery.SUBJECT_KIND,
        subject_id=delivery.SUBJECT_ID,
        predicate=delivery.PREDICATE,
        value="high",
        role="observation",
        rationale="Read from the feed.",
        source_kind="supported_by",
        source_value=selection,
        source_alias="feed.content",
    )
    args = dict(
        procedure_identity=ArtifactIdentity(kind="Procedure", name="mixed"),
        procedure_digest="sha256:" + "c" * 64,
        outputs={},
        provenance={"feed": AliasProvenanceV1(whole=tokens)},
        item_tokens=tokens,
    )
    return candidate, args, admitted.digest, produced.digest


def test_an_explicit_selection_may_name_the_admitted_capture_beside_a_produced_one():
    """S3 review b F-003: explicit selection is checked against produced and admitted."""

    from cruxible_core.procedures.source_candidates import bind_source_candidate

    candidate, args, admitted, produced = _mixed_closure_candidate({"capture_digest": "x"})
    for chosen in (admitted, produced):
        bound = bind_source_candidate(
            {**candidate, "source_value": {"capture_digest": chosen}}, **args
        )
        assert bound["source"]["capture_digest"] == chosen
    with pytest.raises(ValueError, match="one verified Capture"):
        bind_source_candidate(
            {**candidate, "source_value": {"capture_digest": "sha256:" + "e" * 64}}, **args
        )


def test_an_implicit_selection_prefers_the_produced_capture():
    from cruxible_core.procedures.source_candidates import bind_source_candidate

    candidate, args, _admitted, produced = _mixed_closure_candidate("high")
    bound = bind_source_candidate(candidate, **args)
    assert bound["source"]["capture_digest"] == produced


@pytest.mark.parametrize(
    "kind,value",
    [
        ("subject", {"subject_kind": "security.advisory", "subject_id": "another"}),
        ("exact_content", "Exact words"),
        ("literal", "high"),
    ],
)
def test_source_candidate_preserves_value_kind_period_and_dispositions(kind, value):
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_core.procedures.source_candidates import bind_source_candidate

    claim_id = "CLM-" + "a" * 32
    resolved = dict(
        tag="playbill-source-claim-candidate-v1",
        subject_kind="security.advisory",
        subject_id="osv-2026-0001",
        predicate="security.advisory.severity",
        object_kind=kind,
        value=value,
        role="observation",
        rationale="Author supplied this observation.",
        source_kind="self_source",
        source_value="The advisory is high severity.",
        source_alias=None,
        effective_from="2026-09-22T00:00:00Z",
        dispositions=[{"claim_id": claim_id, "disposition": "not_tested"}],
    )
    item = bind_source_candidate(
        resolved,
        procedure_identity=ArtifactIdentity(kind="Procedure", name="example"),
        procedure_digest="sha256:" + "c" * 64,
        outputs={},
        provenance={},
        item_tokens=frozenset(),
    )
    assert item["statement"]["object"]["kind"] == (
        "exact_content_body" if kind == "exact_content" else kind
    )
    assert item["statement"]["effective_from"] == "2026-09-22T00:00:00+00:00"
    assert item["existing_claim_dispositions"] == [
        {"claim_id": claim_id, "disposition": "not_tested"}
    ]
    if kind == "subject":
        assert item["statement"]["object"]["address"]["artifact_path"].endswith("another.json")
    if kind == "exact_content":
        import base64

        assert base64.b64decode(item["statement"]["object"]["content_base64"]) == b"Exact words"
    with pytest.raises(ValueError, match="increasing"):
        bind_source_candidate(
            {**resolved, "effective_until": "2026-09-21T00:00:00Z"},
            procedure_identity=ArtifactIdentity(kind="Procedure", name="example"),
            procedure_digest="sha256:" + "c" * 64,
            outputs={},
            provenance={},
            item_tokens=frozenset(),
        )
