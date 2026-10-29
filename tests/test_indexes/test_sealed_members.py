"""Carried Claim rows equal fresh derivation and cannot survive changed bytes."""

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
