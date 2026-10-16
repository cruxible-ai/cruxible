"""Immutable string values validate once without losing kind or refusal semantics."""

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.canonical import ArtifactDigest, CasDigest, Sha256Value
from cruxible_client.contracts.semantic import SemanticAddress


def test_digest_values_are_shared_by_kind_and_refuse_invalid_values():
    value = "sha256:" + "e" * 64
    first = ArtifactDigest.from_tagged(value)
    assert ArtifactDigest.from_tagged(value) is first
    assert isinstance(first, ArtifactDigest)
    assert type(CasDigest.from_tagged(value)) is CasDigest
    assert type(Sha256Value.from_tagged(value)) is Sha256Value
    for invalid in ("sha1:" + "e" * 64, "sha256:" + "E" * 64, "sha256:1"):
        for _ in range(2):
            with pytest.raises(ValueError):
                ArtifactDigest.from_tagged(invalid)


def test_cached_identities_and_addresses_are_immutable_and_copies_are_independent():
    identity = parse_artifact_identity("Subject:kind/item")
    assert parse_artifact_identity(identity.qualified) is identity
    address = SemanticAddress.whole_artifact("subjects/kind/item.json")
    assert SemanticAddress.whole_artifact(address.artifact_path) is address
    with pytest.raises(ValidationError):
        identity.name = "changed"
    with pytest.raises(ValidationError):
        address.selector.value = "changed"
    changed = address.model_copy(update={"artifact_path": "subjects/kind/other.json"})
    assert changed != SemanticAddress.whole_artifact(address.artifact_path)
    for _ in range(2):
        with pytest.raises(ValueError):
            parse_artifact_identity("missing-kind")
        with pytest.raises(ValueError):
            SemanticAddress.whole_artifact("../outside.json")
