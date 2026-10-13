"""Pure artifact parses run once per exact content; digests follow the current content (perf B4)."""

from __future__ import annotations

import pytest

from cruxible_client.contracts import claim_types
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import (
    ClaimTypeFormatError,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.content_memo import ParseMemo, ValueMemo
from cruxible_client.contracts.subjects import (
    SubjectShell,
    parse_subject,
    render_subject,
    subject_digest,
    subject_path,
)
from tests.test_procedures.test_procedure_proposal_delivery import _claim_type


def _shell(subject_id: str) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"memo.kind/{subject_id}"),
        subject_kind="memo.kind",
        subject_id=subject_id,
    )


def test_a_parse_runs_once_per_exact_bytes_and_a_refusal_is_never_kept(monkeypatch):
    parsed = []
    real = claim_types._parse_claim_type

    def counted(content, *, path, codec):  # type: ignore[no-untyped-def]
        parsed.append(path)
        return real(content, path=path, codec=codec)

    monkeypatch.setattr(claim_types, "_parse_claim_type", counted)
    claim_types._CLAIM_TYPE_PARSES.clear()
    value = _claim_type("sha256:" + "1" * 64)
    path = claim_type_path(value.predicate)
    content = render_claim_type(value)
    first = parse_claim_type(content, path=path)
    again = parse_claim_type(bytes(bytearray(content)), path=path)
    # Each caller gets its own detached copy of the one parse.
    assert first == value == again and again is not first and parsed == [path]
    first.identity.__dict__["name"] = "poisoned"
    assert parse_claim_type(content, path=path) == value
    # A different path is a different parse (and still refuses as before).
    with pytest.raises(ClaimTypeFormatError):
        parse_claim_type(content, path="claim-types/other.json")
    with pytest.raises(ClaimTypeFormatError):
        parse_claim_type(content, path="claim-types/other.json")
    assert parsed == [path, "claim-types/other.json", "claim-types/other.json"]
    # The digest of the remembered value is the one computed afresh.
    assert claim_type_digest(again) == claim_type_digest(value)


def test_digests_follow_the_current_content_of_shallowly_frozen_models():
    """Review F-001: a nested container changed in place changes the digest.

    Frozen models are frozen only shallowly (``ClaimType.literal_schema`` is a
    dict), so no digest is remembered by the model object: each digest is
    computed from the content the model holds when asked, and equal content in
    distinct objects digests the same.
    """

    value = _claim_type("sha256:" + "1" * 64)
    before = claim_type_digest(value)
    assert value.literal_schema is not None
    value.literal_schema["type"] = "integer"
    after = claim_type_digest(value)
    assert after != before
    assert after == claim_types.CLAIM_TYPE_DIGEST_FUNCTIONS[value.artifact_format](value)
    shell = parse_subject(render_subject(_shell("a")), path=subject_path("memo.kind", "a"))
    assert subject_digest(shell) == subject_digest(_shell("a"))


def test_the_genesis_digest_memo_is_keyed_by_the_identity_strings():
    from cruxible_core.exhaust import records
    from cruxible_core.exhaust.records import (
        PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        JournalStreamIdentityV1,
        journal_genesis_digest,
    )

    stream = JournalStreamIdentityV1(
        instance_id="memo-instance",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="memo",
    )
    first = journal_genesis_digest(stream, "p-1")
    assert first == records._journal_genesis_digest(stream, "p-1")
    other = stream.model_copy(update={"stream_id": "memo-other"})
    assert journal_genesis_digest(other, "p-1") == records._journal_genesis_digest(other, "p-1")
    assert journal_genesis_digest(other, "p-1") != first
    assert all(isinstance(part, str) for key in records._GENESIS_DIGESTS._entries for part in key)


def test_memos_are_bounded():
    parses: ParseMemo[int] = ParseMemo(capacity=2, max_bytes=1 << 20)
    for number in range(5):
        parses.parse(str(number).encode(), "p", None, lambda number=number: number)
    assert len(parses._entries) == 2
    small: ParseMemo[int] = ParseMemo(capacity=10, max_bytes=256)
    small.parse(b"x" * 64, "p", None, lambda: 1)
    assert not small._entries  # one entry may never take a sixteenth of the budget
    values: ValueMemo[int] = ValueMemo(capacity=2)
    for number in range(4):
        values.derive(("k", number), lambda number=number: number)
    assert len(values._entries) == 2
    assert values.derive(["unhashable"], lambda: 7) == 7
