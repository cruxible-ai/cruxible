"""Source field reads reuse the bounded Claim service at an exact accepted base."""

from datetime import datetime

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.service.procedures.procedures import PlaybillProcedureStateTapReader
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    SUBJECT_KIND,
    seed_claims,
)


def test_field_read_selects_one_subject_and_retains_exact_claim(tmp_path, monkeypatch):
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
    pin = ArtifactPin(
        role="claim-type",
        target=ArtifactIdentity(kind="ClaimType", name=PREDICATE),
        artifact_digest=envelope.artifact_digest,
    )
    reader = PlaybillProcedureStateTapReader(
        instance=instance, evaluation_time=datetime.fromisoformat(EVALUATION_TIME)
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Field reads must not scan the world or accepted history")

    monkeypatch.setattr(instance, "tree_at", forbidden)
    monkeypatch.setattr(instance, "accepted_history", forbidden)
    result = reader.read_accepted_claim(
        claim_type=pin,
        subject_kind=SUBJECT_KIND,
        subject_id="wi-42",
        cardinality="one",
        coordinate=at,
    )
    assert result.value["value"] == "ready"
    assert result.value["claim"]["statement"]["subject"]["artifact_path"].endswith("wi-42.json")
    assert result.value["artifact_digest"].startswith("sha256:")
    with pytest.raises(ExecutionError, match="absent"):
        reader.read_accepted_claim(
            claim_type=pin,
            subject_kind=SUBJECT_KIND,
            subject_id="missing",
            cardinality="one",
            coordinate=at,
        )


def test_plural_field_read_requires_bound_and_retains_semantic_metadata(tmp_path):
    instance, _ = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    at = AcceptedCoordinate.from_internal(coordinate)
    with instance.bind_accepted_projection(coordinate) as projection:
        envelope = projection.typed.envelope("ClaimType:" + PREDICATE)
    args = dict(
        claim_type=ArtifactPin(
            role="claim-type",
            target=ArtifactIdentity(kind="ClaimType", name=PREDICATE),
            artifact_digest=envelope.artifact_digest,
        ),
        subject_kind=SUBJECT_KIND,
        subject_id="wi-42",
        cardinality="all",
        coordinate=at,
    )
    reader = PlaybillProcedureStateTapReader(
        instance=instance, evaluation_time=datetime.fromisoformat(EVALUATION_TIME)
    )
    with pytest.raises(ExecutionError, match="explicit positive limit"):
        reader.read_accepted_claim(**args)
    (selected,) = reader.read_accepted_claim(limit=1, **args).value
    assert selected["predicate"] == PREDICATE
    assert selected["role"] == "observation" and selected["lifecycle_state"] == "live"
    assert selected["subject"]["artifact_path"].endswith("wi-42.json")
    assert selected["claim_id"].startswith("CLM-") and selected["captures"]


def _paged(instance, monkeypatch, total):  # type: ignore[no-untyped-def]
    """Serve ``total`` Claims about wi-42 (the one real view, repeated) a page at a time."""

    from cruxible_core.service.claims import claim_reads

    real = claim_reads.service_read_claim_batch
    pages: list[int] = []

    def paged(instance_arg, *, request):  # type: ignore[no-untyped-def]
        first = real(instance_arg, request=request.model_copy(update={"cursor": None, "limit": 1}))
        offset = int(request.cursor or 0)
        count = min(request.limit, total - offset)
        pages.append(request.limit)
        more = offset + count < total
        return first.model_copy(
            update={
                "claims": first.claims * count,
                "truncated": more,
                "cursor": str(offset + count) if more else None,
            }
        )

    monkeypatch.setattr(claim_reads, "service_read_claim_batch", paged)
    return pages


def _plural_args(instance):  # type: ignore[no-untyped-def]
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        envelope = projection.typed.envelope("ClaimType:" + PREDICATE)
    return dict(
        claim_type=ArtifactPin(
            role="claim-type",
            target=ArtifactIdentity(kind="ClaimType", name=PREDICATE),
            artifact_digest=envelope.artifact_digest,
        ),
        subject_kind=SUBJECT_KIND,
        subject_id="wi-42",
        cardinality="all",
        coordinate=AcceptedCoordinate.from_internal(coordinate),
    )


def test_a_plural_claim_read_runs_past_the_interactive_cap_to_the_end(tmp_path, monkeypatch):
    """F8: no 256-result clamp; the selection pages to the end and reports its rows."""

    instance, _ = seed_claims(tmp_path)
    pages = _paged(instance, monkeypatch, total=600)
    reader = PlaybillProcedureStateTapReader(
        instance=instance, evaluation_time=datetime.fromisoformat(EVALUATION_TIME)
    )

    result = reader.read_accepted_claim(limit=1000, ceiling=5000, **_plural_args(instance))

    assert len(result.value) == 600 and result.rows == 600
    assert len(pages) == 3  # 256 + 256 + 88 (and one more to see the end)
    assert result.effective_budgets.max_results == 5000


@pytest.mark.parametrize(
    ("limit", "ceiling", "refusal"),
    [(1000, 599, "read_ceiling_exceeded"), (599, 5000, "must be complete")],
)
def test_a_plural_claim_read_refuses_rather_than_return_part_of_its_selection(
    tmp_path, monkeypatch, limit, ceiling, refusal
):
    from cruxible_core.procedures.execution import ReadCeilingExceeded

    instance, _ = seed_claims(tmp_path)
    _paged(instance, monkeypatch, total=600)
    reader = PlaybillProcedureStateTapReader(
        instance=instance, evaluation_time=datetime.fromisoformat(EVALUATION_TIME)
    )
    expected = ReadCeilingExceeded if refusal == "read_ceiling_exceeded" else ExecutionError
    with pytest.raises(expected, match=refusal):
        reader.read_accepted_claim(limit=limit, ceiling=ceiling, **_plural_args(instance))
