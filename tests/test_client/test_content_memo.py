"""Pure artifact parses run once per exact content; digests of sealed artifacts once per object."""

from __future__ import annotations

import copy
import gc
import pickle
import weakref

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_client.contracts import claim_types
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import (
    ClaimTypeFormatError,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.content_memo import (
    FrozenDict,
    FrozenList,
    ParseMemo,
    ValueMemo,
    is_sealed,
    seal,
    sealed_memo,
)
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
    # Every caller gets the one sealed parse; it cannot change, at any depth.
    assert first == value == again and again is first and parsed == [path]
    assert is_sealed(first) and isinstance(first.literal_schema, FrozenDict)
    with pytest.raises(ValidationError):
        first.identity.name = "poisoned"  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.literal_schema["type"] = "integer"  # type: ignore[index]
    with pytest.raises(TypeError):
        first.literal_schema.update(type="integer")  # type: ignore[union-attr]
    assert parse_claim_type(content, path=path) == value
    # A different path is a different parse (and still refuses as before).
    with pytest.raises(ClaimTypeFormatError):
        parse_claim_type(content, path="claim-types/other.json")
    with pytest.raises(ClaimTypeFormatError):
        parse_claim_type(content, path="claim-types/other.json")
    assert parsed == [path, "claim-types/other.json", "claim-types/other.json"]
    # The digest of the remembered value is the one computed afresh.
    assert claim_type_digest(again) == claim_type_digest(value)


def test_a_sealed_digest_is_computed_once_and_equals_the_fresh_one(monkeypatch):
    calls = []
    real = claim_types.CLAIM_TYPE_DIGEST_FUNCTIONS["playbill-claim-type-v1"]

    def counted(value):  # type: ignore[no-untyped-def]
        calls.append(value)
        return real(value)

    monkeypatch.setitem(claim_types.CLAIM_TYPE_DIGEST_FUNCTIONS, "playbill-claim-type-v1", counted)
    claim_types._CLAIM_TYPE_PARSES.clear()
    value = _claim_type("sha256:" + "2" * 64)
    path = claim_type_path(value.predicate)
    sealed = parse_claim_type(render_claim_type(value), path=path)
    digests = {claim_type_digest(sealed) for _ in range(5)}
    assert digests == {real(value)} and len(calls) == 1
    # An unsealed object with the same content digests the same, afresh each time.
    assert claim_type_digest(value) == claim_type_digest(value) == real(value)
    assert len(calls) == 3


def test_copies_of_a_sealed_artifact_are_never_served_its_digest():
    claim_types._CLAIM_TYPE_PARSES.clear()
    value = _claim_type("sha256:" + "3" * 64)
    sealed = parse_claim_type(render_claim_type(value), path=claim_type_path(value.predicate))
    digest = claim_type_digest(sealed)
    # model_copy with an update: a new, unsealed object digests its own content.
    changed = sealed.model_copy(update={"literal_schema": {"type": "integer"}})
    assert not is_sealed(changed)
    assert claim_type_digest(changed) != digest
    assert claim_type_digest(changed) == claim_types.CLAIM_TYPE_DIGEST_FUNCTIONS[
        changed.artifact_format
    ](changed)
    # A deep copy or an unpickled copy is plain and mutable again, never sealed;
    # changing it changes its digest and never the sealed original's.
    for duplicate in (copy.deepcopy(sealed), pickle.loads(pickle.dumps(sealed))):
        assert not is_sealed(duplicate) and duplicate == sealed
        assert type(duplicate.literal_schema) is dict
        duplicate.literal_schema["type"] = "integer"  # type: ignore[index]
        assert claim_type_digest(duplicate) == claim_type_digest(changed) != digest
    assert claim_type_digest(sealed) == digest
    assert claim_types.CLAIM_TYPE_DIGEST_FUNCTIONS[sealed.artifact_format](sealed) == digest
    # A shallow copy of a sealed container is the caller's own plain container.
    assert type(copy.copy(sealed.literal_schema)) is dict
    assert type(dict(sealed.literal_schema)) is dict  # type: ignore[arg-type]


class _Leaf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tags: list[str]
    meta: dict[str, object]


class _Loose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str


class _Root(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    leaves: tuple[_Leaf, ...]
    loose: _Loose | None = None


def test_sealing_is_deep_and_all_or_nothing():
    root = _Root(leaves=(_Leaf(tags=["a"], meta={"k": [1, {"z": 2}]}),))
    assert seal(root) and is_sealed(root) and is_sealed(root.leaves[0])
    leaf = root.leaves[0]
    assert isinstance(leaf.tags, FrozenList) and isinstance(leaf.meta["k"], FrozenList)
    nested = leaf.meta["k"][1]  # type: ignore[index]
    with pytest.raises(TypeError):
        nested["z"] = 3
    with pytest.raises(TypeError):
        leaf.tags.append("b")
    # The dumps of a sealed model are the caller's own plain values.
    dumped = root.model_dump(mode="json")
    dumped["leaves"][0]["meta"]["k"].append(3)
    assert root.model_dump(mode="json")["leaves"][0]["meta"] == {"k": [1, {"z": 2}]}
    # One mutable (non-frozen) model anywhere in the tree: nothing changes at all.
    mixed = _Root(leaves=(_Leaf(tags=["a"], meta={}),), loose=_Loose(note="n"))
    assert not seal(mixed)
    assert not is_sealed(mixed) and not is_sealed(mixed.leaves[0])
    assert type(mixed.leaves[0].tags) is list and type(mixed.leaves[0].meta) is dict


def test_a_sealed_memo_is_bounded_by_the_lives_of_its_objects():
    from cruxible_client.contracts import content_memo

    @sealed_memo
    def tag_count(value: _Root) -> int:
        return sum(len(leaf.tags) for leaf in value.leaves)

    roots = [_Root(leaves=(_Leaf(tags=["a"] * n, meta={}),)) for n in range(50)]
    for root in roots:
        seal(root)
    assert [tag_count(root) for root in roots] == list(range(50))

    # Identify exactly the entries our own 50 roots and 50 leaves hold, by id.
    # Checking against that set -- rather than the ambient size of _SEALED --
    # keeps this deterministic: some other, unrelated sealed object elsewhere
    # in the process can be sitting in a reference cycle that only this test's
    # gc.collect() call happens to resolve, which moves a raw-length "before"
    # baseline without any object of ours being involved.
    our_keys = {id(root) for root in roots} | {id(root.leaves[0]) for root in roots}
    assert len(our_keys) == 100
    assert our_keys <= set(content_memo._SEALED)

    # Drive the assertion off weakref callbacks on our own objects, not off
    # refcount timing: a callback fires exactly when CPython actually frees
    # the object, whether that happens on `del` or only once gc.collect()
    # breaks a cycle, so there is nothing to race.
    released: set[int] = set()
    watchers = [
        weakref.ref(obj, lambda _ref, key=key: released.add(key))
        for root in roots
        for key, obj in ((id(root), root), (id(root.leaves[0]), root.leaves[0]))
    ]
    del roots, root
    gc.collect()
    assert released == our_keys
    assert watchers  # keep the weakrefs themselves alive until after the asserts
    assert our_keys.isdisjoint(content_memo._SEALED)


def test_digests_follow_the_current_content_of_shallowly_frozen_models():
    """Review F-001: a nested container changed in place changes the digest.

    Frozen models are frozen only shallowly (``ClaimType.literal_schema`` is a
    dict), so no digest is remembered for an unsealed model object: each digest
    is computed from the content the model holds when asked, and equal content
    in distinct objects digests the same. Only a sealed (deeply immutable)
    object has its digest remembered.
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


def test_owner_contract_schemas_are_frozen_so_procedures_can_be_sealed():
    from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
    from cruxible_client.contracts.records import record_normalization_schema

    field = PropertySchema(type="json", required=True, json_schema={"type": ["object", "null"]})
    assert field.optional is False
    with pytest.raises(ValidationError):
        field.optional = True  # type: ignore[misc]
    schema = ContractSchema(fields={"payload": field, "name": PropertySchema(type="string")})
    adjusted = record_normalization_schema(schema, {"payload": None, "name": "x"})
    assert adjusted.fields["payload"].optional is True and adjusted is not schema
    assert schema.fields["payload"].optional is False
    assert seal(schema) and isinstance(schema.fields, FrozenDict)
    assert isinstance(schema.fields["payload"].json_schema, FrozenDict)
