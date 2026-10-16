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


def test_public_coordinates_are_reused_by_all_four_scalar_values():
    from types import SimpleNamespace

    from cruxible_client.contracts.projection import AcceptedCoordinate

    internal = SimpleNamespace(
        git_oid="1" * 64,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler=SimpleNamespace(rule_digest="sha256:" + "4" * 64),
    )
    first = AcceptedCoordinate.from_internal(internal)
    assert AcceptedCoordinate.from_internal(internal) is first
    assert first == AcceptedCoordinate.model_validate(first.model_dump(mode="json"))
    for field in ("git_oid", "semantic_root", "generation_root", "compiler_digest"):
        replacement = "5" * 64 if field == "git_oid" else "sha256:" + "5" * 64
        changed = first.model_copy(update={field: replacement})
        other = SimpleNamespace(
            git_oid=changed.git_oid,
            semantic_root=changed.semantic_root,
            generation_root=changed.generation_root,
            compiler=SimpleNamespace(rule_digest=changed.compiler_digest),
        )
        assert AcceptedCoordinate.from_internal(other) == changed
        assert AcceptedCoordinate.from_internal(other) is not first
    with pytest.raises(ValidationError):
        first.git_oid = "6" * 64
    internal.git_oid = "invalid"
    for _ in range(2):
        with pytest.raises(ValidationError):
            AcceptedCoordinate.from_internal(internal)


def test_provider_build_names_reuse_validated_scalar_values_and_preserve_refusals():
    from cruxible_client.contracts.providers import (
        ProviderBuildName,
        ProviderFormatError,
        parse_provider_build_name,
    )

    for text in ("workspace.file", "example.provider/" + "a" * 64):
        first = parse_provider_build_name(text)
        assert parse_provider_build_name(text) is first
        assert first == ProviderBuildName.model_validate(first.model_dump(mode="json"))
        with pytest.raises(ValidationError):
            first.name = "changed"
    for text in ("", "provider/short", "provider/" + "A" * 64, "provider/extra/path"):
        for _ in range(2):
            with pytest.raises(ProviderFormatError):
                parse_provider_build_name(text)
