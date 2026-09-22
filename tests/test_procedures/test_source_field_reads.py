"""Source field reads reuse the bounded Claim service at an exact accepted base."""

from datetime import datetime

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.errors import PlaybillExecutionError
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
    with pytest.raises(PlaybillExecutionError, match="absent"):
        reader.read_accepted_claim(
            claim_type=pin,
            subject_kind=SUBJECT_KIND,
            subject_id="missing",
            cardinality="one",
            coordinate=at,
        )
