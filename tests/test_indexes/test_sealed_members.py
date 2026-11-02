"""Carried Claim rows equal fresh derivation and cannot survive changed bytes."""

from contextlib import nullcontext
from copy import deepcopy

import pytest

from cruxible_client.contracts.canonical import file_digest
from cruxible_client.contracts.claims import (
    _parse_claim,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    render_claim,
)
from cruxible_core.compiler.projection_artifacts import _claim_static_facts
from cruxible_core.compiler.sealed_members import sealed_claim_member
from tests.test_claims.test_claims import _claim


def _member():
    claim = _claim(
        claim_id="CLM-" + "d" * 32,
        capture_digest="sha256:" + "3" * 64,
        source_digest="sha256:" + "4" * 64,
        source_length=10,
    )
    path = claim_path(claim.identity.name)
    return path, render_claim(claim)


def test_carried_member_equals_the_cold_parse_digests_and_normalized_rows(monkeypatch):
    path, content = _member()
    member = sealed_claim_member(content, path=path)
    fresh = _parse_claim(content, path=path)
    assert member.claim == fresh
    assert member.input_digest == file_digest(content).tagged
    assert member.artifact_digest == claim_artifact_digest(fresh).tagged
    assert member.statement_digest == claim_statement_digest(fresh.statement).tagged
    assert member.facts == _claim_static_facts(
        fresh,
        path=path,
        input_digest=member.input_digest,
        artifact_digest=member.artifact_digest,
        statement_digest=member.statement_digest,
    )
    from cruxible_core.compiler import sealed_members

    def refuse(*args, **kwargs):
        raise AssertionError("a carried member must not parse again")

    monkeypatch.setattr(sealed_members, "parse_claim", refuse)
    assert sealed_claim_member(bytes(bytearray(content)), path=path) is member


def test_carried_rows_are_deeply_immutable_and_changed_content_derives_afresh():
    path, content = _member()
    first = sealed_claim_member(content, path=path)
    for fact in first.facts:
        with pytest.raises(TypeError):
            fact.value["poison"] = True
    changed = deepcopy(first.claim)
    changed = changed.model_copy(
        update={"statement": changed.statement.model_copy(update={"qualifier": "changed"})}
    )
    second = sealed_claim_member(render_claim(changed), path=path)
    assert second is not first
    assert second.artifact_digest != first.artifact_digest
    assert second.statement_digest != first.statement_digest
    assert second.facts != first.facts
    assert sealed_claim_member(content, path=path) is first


def test_carried_member_does_not_hide_wrong_path_or_noncanonical_bytes():
    path, content = _member()
    sealed_claim_member(content, path=path)
    from cruxible_client.contracts.claims import ClaimFormatError

    with pytest.raises(ClaimFormatError):
        sealed_claim_member(content, path=claim_path("CLM-" + "e" * 32))
    with pytest.raises(ClaimFormatError):
        sealed_claim_member(content + b" ", path=path)


def test_request_owns_a_member_when_the_process_carrier_cannot_retain_it(monkeypatch):
    from cruxible_client.contracts.content_memo import artifact_request
    from cruxible_core.compiler import sealed_members

    monkeypatch.setattr(sealed_members, "_MAX_BYTES", 0)
    sealed_members._clear()
    path, content = _member()
    with artifact_request():
        member = sealed_claim_member(content, path=path)
        sealed_members._clear()
        with artifact_request():
            assert sealed_claim_member(content, path=path) is member
        # The oracle bypasses both the request carrier and process LRU.
        fresh = _parse_claim(content, path=path)
        assert member.claim == fresh
        assert member.facts == _claim_static_facts(
            fresh,
            path=path,
            input_digest=member.input_digest,
            artifact_digest=member.artifact_digest,
            statement_digest=member.statement_digest,
        )
    assert sealed_claim_member(content, path=path) is not member


def test_projection_subtree_reuse_matches_cold_values_after_each_input_changes():
    from cruxible_client.contracts.projection_extensions import normalize_projection_value
    from cruxible_core.compiler.projection_artifacts import (
        _normalized_pin,
        _normalized_source_mapping,
    )

    path, content = _member()
    member = sealed_claim_member(content, path=path)
    mapping = member.claim.backing.source_mappings[0]
    variants = (
        mapping,
        deepcopy(mapping),
        mapping.model_copy(
            update={
                "subject": mapping.subject.model_copy(
                    update={"artifact_path": "subjects/other/one.json"}
                )
            }
        ),
        mapping.model_copy(
            update={"spans": (mapping.spans[0].model_copy(update={"end_byte": 11}),)}
        ),
        mapping.model_copy(
            update={
                "spans": (
                    mapping.spans[0].model_copy(update={"content_digest": "sha256:" + "9" * 64}),
                )
            }
        ),
    )
    for value in variants:
        carried = _normalized_source_mapping(value)
        assert normalize_projection_value(carried) == normalize_projection_value(
            value.model_dump(mode="json")
        )
        with pytest.raises(TypeError):
            carried.value["subject"] = {}
    pin = member.claim.pins[0]
    for value in (
        pin,
        deepcopy(pin),
        pin.model_copy(update={"artifact_digest": "sha256:" + "8" * 64}),
    ):
        assert normalize_projection_value(_normalized_pin(value)) == normalize_projection_value(
            value.model_dump(mode="json")
        )


def test_source_windows_share_only_their_exact_normalized_address_within_a_request(monkeypatch):
    from cruxible_client.contracts.content_memo import artifact_request
    from cruxible_client.contracts.projection_extensions import normalize_projection_value
    from cruxible_core.compiler import projection_artifacts as module

    path, content = _member()
    mapping = sealed_claim_member(content, path=path).claim.backing.source_mappings[0]
    next_window = mapping.model_copy(
        update={"spans": (mapping.spans[0].model_copy(update={"end_byte": 11}),)}
    )
    other_subject = next_window.model_copy(
        update={
            "subject": mapping.subject.model_copy(
                update={"artifact_path": "subjects/other/one.json"}
            )
        }
    )
    addresses = []
    original = module.normalized_projection_value

    def normalized(value):
        if isinstance(value, dict) and value.get("tag") == mapping.subject.tag:
            addresses.append(value)
        return original(value)

    monkeypatch.setattr(module, "normalized_projection_value", normalized)
    module._SOURCE_MAPPINGS.clear()
    with artifact_request():
        for value in (mapping, next_window, other_subject):
            assert normalize_projection_value(module._normalized_source_mapping(value)) == (
                normalize_projection_value(value.model_dump(mode="json"))
            )
    assert len(addresses) == 2
    module._SOURCE_MAPPINGS.clear()
    with artifact_request():
        module._normalized_source_mapping(mapping)
    assert len(addresses) == 3  # a new request pays the cold address work again


def test_claim_identity_and_source_windows_share_the_same_cold_address(monkeypatch):
    from cruxible_client.contracts.content_memo import artifact_request
    from cruxible_core.compiler import projection_artifacts as module

    path, content = _member()
    claim = _parse_claim(content, path=path)
    arguments = dict(
        path=path,
        input_digest=file_digest(content).tagged,
        artifact_digest=claim_artifact_digest(claim).tagged,
        statement_digest=claim_statement_digest(claim.statement).tagged,
    )
    module._SOURCE_MAPPINGS.clear()
    cold = module._claim_static_facts(claim, **arguments)
    addresses = []
    original = module.normalized_projection_value

    def normalized(value):
        if isinstance(value, dict) and value.get("tag") == claim.statement.subject.tag:
            addresses.append(value)
        return original(value)

    monkeypatch.setattr(module, "normalized_projection_value", normalized)
    module._SOURCE_MAPPINGS.clear()
    with artifact_request():
        assert module._claim_static_facts(claim, **arguments) == cold
    assert addresses == [claim.backing.source_mappings[0].subject.model_dump(mode="json")]


def test_mapping_address_reuse_preserves_cold_serialization_of_subclasses_and_copies():
    from cruxible_client.contracts.content_memo import artifact_request
    from cruxible_client.contracts.projection_extensions import normalize_projection_value
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_core.compiler.projection_artifacts import _normalized_source_mapping

    path, content = _member()
    mapping = sealed_claim_member(content, path=path).claim.backing.source_mappings[0]

    class ExtendedAddress(SemanticAddress):
        extra: str = "must follow the declared parent field's serialization"

    variants = (
        mapping.model_copy(update={"subject": ExtendedAddress(**mapping.subject.model_dump())}),
        mapping.model_copy(
            update={
                "subject": mapping.subject.model_copy(
                    update={"artifact_path": ["copied without validation"]}
                )
            }
        ),
    )
    with artifact_request():
        for value in variants:
            with (
                pytest.warns(UserWarning)
                if isinstance(value.subject.artifact_path, list)
                else nullcontext()
            ):
                assert normalize_projection_value(_normalized_source_mapping(value)) == (
                    normalize_projection_value(value.model_dump(mode="json"))
                )
