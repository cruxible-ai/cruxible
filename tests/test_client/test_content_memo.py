"""Pure artifact parses and digests run once per exact content (perf B4)."""

from __future__ import annotations

import pytest

from cruxible_client.contracts import claim_types, subjects
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import (
    ClaimTypeFormatError,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.content_memo import ObjectMemo, ParseMemo, ValueMemo
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
    assert claim_type_digest(again) == claim_types._claim_type_digest(value)


def test_a_digest_is_kept_with_its_exact_object_only(monkeypatch):
    """Each hit returns its own digest value, equal to the one derived afresh."""

    digests = []
    real = subjects._subject_digest

    def counted(shell):  # type: ignore[no-untyped-def]
        digests.append(shell.subject_id)
        return real(shell)

    monkeypatch.setattr(subjects, "_subject_digest", counted)
    shell = parse_subject(render_subject(_shell("a")), path=subject_path("memo.kind", "a"))
    first, again = subject_digest(shell), subject_digest(shell)
    assert first == again == real(_shell("a")) and again is not first
    # An equal but distinct object derives again: identity, never a mutable key.
    assert subject_digest(_shell("a")) == real(_shell("a"))
    assert digests == ["a", "a"]


def test_memos_are_bounded():
    parses: ParseMemo[int] = ParseMemo(capacity=2, max_bytes=1 << 20)
    for number in range(5):
        parses.parse(str(number).encode(), "p", None, lambda number=number: number)
    assert len(parses._entries) == 2
    small: ParseMemo[int] = ParseMemo(capacity=10, max_bytes=256)
    small.parse(b"x" * 64, "p", None, lambda: 1)
    assert not small._entries  # one entry may never take a sixteenth of the budget
    objects: ObjectMemo[int] = ObjectMemo(capacity=3)
    held = [object() for _ in range(5)]
    for number, item in enumerate(held):
        objects.derive(item, lambda number=number: number)
    assert len(objects._entries) == 3
    values: ValueMemo[int] = ValueMemo(capacity=2)
    for number in range(4):
        values.derive(("k", number), lambda number=number: number)
    assert len(values._entries) == 2
    assert values.derive(["unhashable"], lambda: 7) == 7
