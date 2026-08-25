"""Frozen Markdown declaration, canonical stamp, and local evidence-boundary laws."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime

import pytest

from cruxible_client.authoring.bind import bind_working_selection_input
from cruxible_client.authoring.blocks import (
    ProjectionIndependentEvidenceForbidden,
    ProjectionMarkerError,
    assert_independent_projection_evidence,
    parse_projection_blocks,
    render_projection_opening,
)
from cruxible_client.authoring.examples import claim_self_source_example
from cruxible_client.authoring.inputs import ClaimInput
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_BLOCKS_PER_SOURCE,
    MAX_PROJECTION_SOURCE_BYTES,
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    ProjectionQueryBackingV1,
    ProjectionResolvedParameterBindingV1,
    projection_parameter_digest,
    projection_query_semantic_result_digest,
)
from cruxible_client.contracts.projection import AcceptedCoordinate

COORDINATE = AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
BODY = b"Visible prose --> stays exactly as the author wrote it.\n"


def _stamp(*, block_id: str = "summary") -> ProjectionBlockStampV1:
    return ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id=block_id,
        declared_generation=7,
        declared_coordinate=COORDINATE,
        backing=(
            ProjectionClaimBackingV1(
                identity=ArtifactIdentity(kind="Claim", name="CLM-example"),
                statement_digest="sha256:" + "5" * 64,
            ),
        ),
        body_digest="sha256:" + hashlib.sha256(BODY).hexdigest(),
    )


def _block(*, block_id: str = "summary", body: bytes = BODY) -> bytes:
    return (
        render_projection_opening(_stamp(block_id=block_id))
        + body
        + f"<!-- /playbill:block:{block_id} -->\n".encode()
    )


def test_declaration_round_trip_preserves_prose_and_presentation_offsets() -> None:
    content = b"before\n" + _block() + b"after\n"

    (parsed,) = parse_projection_blocks(content, source_id="corpus.runbook")

    assert parsed.stamp == _stamp()
    assert parsed.body_digest == _stamp().body_digest
    assert content[parsed.body_start : parsed.body_end] == BODY
    assert content[parsed.opening_start : parsed.opening_end] == render_projection_opening(_stamp())
    assert parsed.summary().start_byte == len(b"before\n")


def test_unstamped_bootstrap_is_undeclared_except_during_explicit_repin() -> None:
    content = b"<!-- playbill:block:summary -->\n" + BODY + b"<!-- /playbill:block:summary -->\n"

    with pytest.raises(ProjectionMarkerError, match="unstamped bootstrap"):
        parse_projection_blocks(content, source_id="corpus.runbook")
    (parsed,) = parse_projection_blocks(
        content,
        source_id="corpus.runbook",
        allow_bootstrap=True,
    )
    assert parsed.stamp is None


@pytest.mark.parametrize("fence", [b"```", b"````", b"~~~", b"   ~~~~"])
def test_markers_inside_commonmark_fences_are_inert(fence: bytes) -> None:
    stripped = fence.lstrip()
    content = fence + b" python\n" + _block() + stripped + b"\n" + _block()

    (parsed,) = parse_projection_blocks(content, source_id="corpus.runbook")

    assert parsed.opening_start > content.find(stripped + b"\n")


def test_short_closing_fence_cannot_expose_marker_candidates() -> None:
    content = b"````\n```\n" + _block() + b"````\n"

    assert parse_projection_blocks(content, source_id="corpus.runbook") == ()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b" <!-- playbill:block:summary -->\n", "column zero"),
        (b"<!-- playbill:block:summary -->\r\n", "LF-only"),
        (b"<!-- playbill:block:summary:bad= -->\n", "malformed grammar"),
        (b"<!-- playbill:block:UPPER -->\n", "malformed grammar"),
        (b"<!-- /playbill:block:summary -->\n", "absent or different"),
        (b"<!-- playbill:block:summary -->\n", "matching closing"),
        (b"\xff", "valid UTF-8"),
    ],
)
def test_marker_grammar_refuses_ambiguous_or_invalid_lines(content: bytes, message: str) -> None:
    with pytest.raises(ProjectionMarkerError, match=message):
        parse_projection_blocks(content, source_id="corpus.runbook", allow_bootstrap=True)


def test_stamp_refuses_duplicate_json_keys_noncanonical_json_and_source_substitution() -> None:
    payload = _stamp().model_dump(mode="json")
    variants = (
        b'{"tag":"one","tag":"two"}',
        json.dumps(payload, sort_keys=True).encode(),
        canonical_bytes({**payload, "source_id": "other.source"}),
        canonical_bytes({**payload, "unexpected": True}),
    )
    for raw in variants:
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=")
        content = (
            b"<!-- playbill:block:summary:"
            + encoded
            + b" -->\n"
            + BODY
            + b"<!-- /playbill:block:summary -->\n"
        )
        with pytest.raises(ProjectionMarkerError):
            parse_projection_blocks(content, source_id="corpus.runbook")


def test_duplicate_nested_unclosed_and_excess_blocks_refuse() -> None:
    with pytest.raises(ProjectionMarkerError, match="repeats block"):
        parse_projection_blocks(_block() + _block(), source_id="corpus.runbook")
    nested = render_projection_opening(_stamp()) + _block(block_id="inner")
    with pytest.raises(ProjectionMarkerError, match="nest or overlap"):
        parse_projection_blocks(nested, source_id="corpus.runbook")
    many = b"".join(
        _block(block_id=f"block{index}") for index in range(MAX_PROJECTION_BLOCKS_PER_SOURCE + 1)
    )
    with pytest.raises(ProjectionMarkerError, match="128-block"):
        parse_projection_blocks(many, source_id="corpus.runbook")
    with pytest.raises(ProjectionMarkerError, match="4 MiB"):
        parse_projection_blocks(
            b"x" * (MAX_PROJECTION_SOURCE_BYTES + 1), source_id="corpus.runbook"
        )


def test_evidence_intersection_is_typed_but_ordinary_prose_is_allowed() -> None:
    content = b"before\n" + _block() + b"after\n"
    start = content.index(BODY)

    with pytest.raises(ProjectionIndependentEvidenceForbidden) as refusal:
        assert_independent_projection_evidence(
            source_id="corpus.runbook",
            content=content,
            start_byte=start,
            end_byte=start + 4,
        )
    assert refusal.value.code == "playbill.projection.independent_evidence_forbidden"
    assert refusal.value.source_id == "corpus.runbook"
    assert refusal.value.block_id == "summary"
    assert "explicit copy citation" in str(refusal.value)
    assert_independent_projection_evidence(
        source_id="corpus.runbook",
        content=content,
        start_byte=0,
        end_byte=len(b"before\n"),
    )


def test_unstamped_block_preserves_independent_evidence_and_stamped_block_refusal() -> None:
    bootstrap_body = b"Unstamped draft evidence.\n"
    content = (
        b"Independent source evidence.\n"
        + b"<!-- playbill:block:draft -->\n"
        + bootstrap_body
        + b"<!-- /playbill:block:draft -->\n"
        + _block()
    )
    template = claim_self_source_example().model_dump(mode="json")
    template["source"] = {"kind": "working_selection", "source_id": "corpus.runbook"}
    evidence = ClaimInput.model_validate({**template, "citation_role": "evidence"})

    for anchor in ("Independent source evidence.", "Unstamped draft evidence."):
        allowed = bind_working_selection_input(evidence, content=content, anchor=anchor)
        assert allowed.citation_role == "evidence"

    with pytest.raises(ProjectionIndependentEvidenceForbidden) as refusal:
        bind_working_selection_input(evidence, content=content, anchor="Visible prose")
    assert refusal.value.block_id == "summary"
    assert refusal.value.code == "playbill.projection.independent_evidence_forbidden"


def test_query_backing_commits_existing_resolved_parameter_digest_and_semantics_only() -> None:
    binding = ProjectionResolvedParameterBindingV1(
        name="status",
        value_type="string",
        value="ready",
    )
    result = {
        "rows": [{"identity": "Subject:wi-42"}],
        "conflicts": [],
        "result_shape": "subject",
        "result_cardinality": "many",
        "result_binding": "item",
        "dedupe": "subject",
        "coordinate": {"ignored": True},
        "evaluated_at": "2026-08-25T00:00:00Z",
    }
    digest = projection_query_semantic_result_digest(result)
    assert (
        projection_query_semantic_result_digest(
            {**result, "coordinate": {"different": True}, "evaluated_at": "later"}
        )
        == digest
    )
    backing = ProjectionQueryBackingV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name="project.items"),
        definition_digest="sha256:" + "6" * 64,
        resolved_parameter_bindings=(binding,),
        canonical_param_digest=projection_parameter_digest((binding,)),
        declared_evaluation_time=datetime(2026, 8, 25, tzinfo=UTC),
        semantic_result_digest=digest,
    )
    assert backing.resolved_parameter_bindings[0].value == "ready"
    with pytest.raises(ValueError, match="does not reproduce"):
        ProjectionQueryBackingV1.model_validate(
            {**backing.model_dump(mode="json"), "canonical_param_digest": "sha256:" + "7" * 64}
        )


@pytest.mark.parametrize("window_lines", [None, 1])
def test_flow_a_bind_refuses_independent_evidence_but_permits_the_same_explicit_copy(
    window_lines: int | None,
) -> None:
    content = b"before\n" + _block() + b"after\n"
    template = claim_self_source_example().model_dump(mode="json")
    template["source"] = {"kind": "working_selection", "source_id": "corpus.runbook"}
    evidence = ClaimInput.model_validate({**template, "citation_role": "evidence"})
    copy = ClaimInput.model_validate({**template, "citation_role": "copy"})

    with pytest.raises(ProjectionIndependentEvidenceForbidden):
        bind_working_selection_input(
            evidence,
            content=content,
            anchor="Visible prose",
            window_lines=window_lines,
        )

    allowed = bind_working_selection_input(
        copy,
        content=content,
        anchor="Visible prose",
        window_lines=window_lines,
    )
    assert allowed.citation_role == "copy"
