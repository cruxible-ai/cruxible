"""Immutable string values validate once without losing kind or refusal semantics."""

from contextlib import nullcontext

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    CasDigest,
    Sha256Value,
    _valid_sha256_hex,
)
from cruxible_client.contracts.content_memo import artifact_request
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


@pytest.mark.parametrize("in_request", [False, True])
def test_rejected_digests_never_grow_validation_or_value_caches(in_request):
    _valid_sha256_hex.cache_clear()
    Sha256Value._from_hex.cache_clear()
    try:
        with artifact_request() if in_request else nullcontext():
            digest = "e" * 64
            tagged = "sha256:" + digest
            first = ArtifactDigest.from_tagged(tagged)
            assert ArtifactDigest.from_tagged(tagged) is first
            assert Sha256Value._from_hex.cache_info().hits == 1
            assert type(CasDigest.from_tagged(tagged)) is CasDigest
            assert _valid_sha256_hex.cache_info().hits == 1
            baseline_hex = _valid_sha256_hex.cache_info()
            baseline_values = Sha256Value._from_hex.cache_info()

            for index in range(16):
                for invalid in (
                    f"{index:x}",
                    f"{index:063x}A",
                    f"{index:063x}z",
                    "a" * 65 + str(index),
                    "a" * (1024 * 1024) + str(index),
                ):
                    before_hex = _valid_sha256_hex.cache_info()
                    before_values = Sha256Value._from_hex.cache_info()
                    for kind in (ArtifactDigest, CasDigest, Sha256Value):
                        message = f"{kind.kind} must be 32 bytes of lowercase SHA-256 hex"
                        for _ in range(2):
                            with pytest.raises(ValueError) as direct:
                                kind(invalid)
                            assert str(direct.value) == message
                            with pytest.raises(ValueError) as tagged_error:
                                kind.from_tagged("sha256:" + invalid)
                            assert str(tagged_error.value) == message
                    if len(invalid) != 64:
                        assert _valid_sha256_hex.cache_info() == before_hex
                        assert Sha256Value._from_hex.cache_info() == before_values
                    assert _valid_sha256_hex.cache_info().currsize == baseline_hex.currsize
                    assert Sha256Value._from_hex.cache_info().currsize == baseline_values.currsize

            for kind in (ArtifactDigest, CasDigest, Sha256Value):
                for invalid in ("sha1:" + digest, "sha1:" + "a" * (1024 * 1024), digest):
                    with pytest.raises(ValueError) as algorithm_error:
                        kind.from_tagged(invalid)
                    assert str(algorithm_error.value) == (
                        f"{kind.kind} must use the 'sha256' algorithm tag"
                    )

            # No refusal became a hit; the successful entries survived them all.
            assert _valid_sha256_hex.cache_info().hits == baseline_hex.hits
            assert Sha256Value._from_hex.cache_info().hits == baseline_values.hits
        assert _valid_sha256_hex.cache_info().currsize == baseline_hex.currsize
        assert Sha256Value._from_hex.cache_info().currsize == baseline_values.currsize
        assert ArtifactDigest.from_tagged(tagged) is first
        assert Sha256Value._from_hex.cache_info().hits == baseline_values.hits + 1
        assert ArtifactDigest(digest) == first
        assert _valid_sha256_hex.cache_info().hits == baseline_hex.hits + 1
    finally:
        _valid_sha256_hex.cache_clear()
        Sha256Value._from_hex.cache_clear()


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
