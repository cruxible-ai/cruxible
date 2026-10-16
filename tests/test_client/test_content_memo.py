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
    artifact_request,
    is_sealed,
    request_derive,
    seal,
    sealed_derive,
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


def test_a_keyed_derivation_is_remembered_per_sealed_object_and_key():
    calls = []

    def derive(label: str) -> str:
        calls.append(label)
        return label.upper()

    sealed = _Leaf(tags=["a"], meta={})
    assert seal(sealed)
    first = sealed_derive(sealed, ("test.keyed", "x"), lambda: derive("x"))
    again = sealed_derive(sealed, ("test.keyed", "x"), lambda: derive("x"))
    other = sealed_derive(sealed, ("test.keyed", "y"), lambda: derive("y"))
    assert (first, again, other) == ("X", "X", "Y") and calls == ["x", "y"]
    # An unsealed object, or a copy of a sealed one, derives afresh every time.
    plain = _Leaf(tags=["a"], meta={})
    copied = sealed.model_copy()
    for value in (plain, plain, copied):
        sealed_derive(value, ("test.keyed", "x"), lambda: derive("x"))
    assert calls == ["x", "y", "x", "x", "x"]


def test_a_sealed_model_is_encoded_once_and_a_copy_afresh(monkeypatch):
    from cruxible_client.contracts import canonical

    encoded = []
    real = canonical.canonical_bytes

    def counted(value):  # type: ignore[no-untyped-def]
        encoded.append(value)
        return real(value)

    monkeypatch.setattr(canonical, "canonical_bytes", counted)
    sealed = _Leaf(tags=["é", "b"], meta={"k": [1, None]})
    assert seal(sealed)
    expected = real(sealed.model_dump(mode="json"))
    assert {canonical.model_canonical_bytes(sealed) for _ in range(3)} == {expected}
    assert len(encoded) == 1
    copied = sealed.model_copy(update={"tags": ["c"]})
    assert canonical.model_canonical_bytes(copied) == real(copied.model_dump(mode="json"))
    assert len(encoded) == 2


def test_composed_digests_and_cheap_comparisons_equal_the_canonical_ones():
    from cruxible_client.contracts.candidates import (
        DependencyProofReference,
        dependency_proof_order,
    )
    from cruxible_client.contracts.canonical import (
        canonical_bytes,
        canonical_digest,
        canonical_equal,
        canonical_list_bytes,
        canonical_list_digest,
        composed_canonical_digest,
    )
    from cruxible_client.contracts.errors import CanonicalEncodingError

    items = [{"b": "é", "a": [1, "x", None]}, {"z": True}, {}]
    payload = {"sequence": 3, "alpha": "ç", "nested": {"y": [False, None]}}
    encoded = [canonical_bytes(item) for item in items]
    assert canonical_list_digest("dom-é", "members", encoded) == canonical_digest(
        "dom-é", {"members": items}
    )
    assert composed_canonical_digest(
        "d",
        payload,
        {
            "members": canonical_list_bytes(encoded),
            "law_evidence": canonical_list_bytes([]),
            "whole": canonical_bytes(items[0]),
        },
    ) == canonical_digest("d", {**payload, "members": items, "law_evidence": [], "whole": items[0]})
    with pytest.raises(CanonicalEncodingError):
        canonical_list_digest("d", "tag", encoded)
    with pytest.raises(CanonicalEncodingError):
        composed_canonical_digest("d", {"members": 1}, {"members": canonical_list_bytes(encoded)})
    for path in ("claims/ab/CLM-1.json", "subjects/kïnd/é.json"):
        reference = DependencyProofReference(
            source_path=path,
            source_artifact_digest="sha256:" + "1" * 64,
            target_path="claim-types/x.json",
            target_artifact_digest="sha256:" + "2" * 64,
            pin_role="claim-type",
        )
        assert dependency_proof_order(reference) == canonical_bytes(
            reference.model_dump(mode="json")
        )
    for left, right in (
        ({"a": "x", "b": [1, None]}, {"a": "x", "b": [1, None]}),
        ({"a": 1}, {"a": True}),
        ({"a": "é"}, {"a": "é"}),
        ({"a": "x"}, {"a": "y"}),
        (FrozenDict({"a": FrozenList(["x"])}), {"a": ["x"]}),
    ):
        assert canonical_equal(left, right) is (canonical_bytes(left) == canonical_bytes(right))


def test_a_memo_of_few_large_records_may_keep_an_entry_over_a_sixteenth():
    large: ParseMemo[_Leaf] = ParseMemo(capacity=2, max_bytes=4096, max_entry_bytes=2048)
    content = b"y" * 300
    first = large.parse(content, "p", None, lambda: _Leaf(tags=["a"], meta={}))
    assert large.parse(content, "p", None, lambda: _Leaf(tags=["b"], meta={})) is first
    assert is_sealed(first)
    small: ParseMemo[_Leaf] = ParseMemo(capacity=2, max_bytes=4096)
    small.parse(content, "p", None, lambda: _Leaf(tags=["a"], meta={}))
    assert not small._entries


def test_a_member_file_digest_is_taken_once_per_exact_bytes(monkeypatch):
    import hashlib

    from cruxible_client.contracts import canonical

    hashed: list[bytes] = []
    real = hashlib.sha256

    def counted(content=b""):  # type: ignore[no-untyped-def]
        hashed.append(bytes(content))
        return real(content)

    monkeypatch.setattr(canonical.hashlib, "sha256", counted)
    canonical._FILE_DIGESTS.clear()
    content = b'{"member": "first"}\n'
    first = canonical.file_digest(content)
    # The same object, and equal bytes read back elsewhere, reuse the one digest.
    assert canonical.file_digest(content) is first
    assert canonical.file_digest(bytes(bytearray(content))) is first
    assert first.value == real(content).hexdigest() and hashed == [content]

    class Tricky(bytes):
        pass

    # A bytes subclass is never kept: it is hashed afresh every time.
    canonical.file_digest(Tricky(content))
    canonical.file_digest(Tricky(content))
    assert len(hashed) == 3
    assert canonical.file_digest(b"other").value == real(b"other").hexdigest()
    canonical._FILE_DIGESTS.clear()


def test_claim_type_derived_rules_and_digests_are_taken_once_per_sealed_claim_type(
    monkeypatch,
):
    from cruxible_client.contracts import claim_verdicts

    claim_types._CLAIM_TYPE_PARSES.clear()
    value = _claim_type("sha256:" + "4" * 64)
    sealed = parse_claim_type(render_claim_type(value), path=claim_type_path(value.predicate))
    digest = claim_type_digest(sealed).tagged
    hashed: list[str] = []
    real = claim_verdicts.typed_digest

    def counted(kind, domain, payload):  # type: ignore[no-untyped-def]
        hashed.append(domain)
        return real(kind, domain, payload)

    monkeypatch.setattr(claim_verdicts, "typed_digest", counted)
    rules = [
        claim_verdicts.claim_adjudication_rule(sealed, claim_type_digest=digest) for _ in range(3)
    ]
    assert rules[0] is rules[1] is rules[2] and is_sealed(rules[0])
    digests = {claim_verdicts.claim_adjudication_rule_digest(rule) for rule in rules}
    assert len(digests) == 1 and len(hashed) == 2  # the policy digest, then the rule's
    # Another argument is another rule; an unsealed ClaimType compiles afresh, equal.
    other = claim_verdicts.claim_adjudication_rule(
        sealed, claim_type_digest=digest, origin_supports=True
    )
    assert other is not rules[0] and other.tag == "playbill-claim-adjudication-rule-v2"
    fresh = claim_verdicts.claim_adjudication_rule(value, claim_type_digest=digest)
    assert fresh == rules[0] and fresh is not rules[0]


class _Stamped(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    meta: dict[str, object]


class _Stamp(_Stamped):
    extra: str = "x"


class _Holder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tag: str = "holder"
    one: _Stamped
    many: tuple[_Stamped, ...]
    maybe: _Stamped | None = None
    plain: dict[str, str]


def test_a_model_holding_sealed_models_composes_their_encodings(monkeypatch):
    import warnings

    from cruxible_client.contracts import canonical

    first = _Stamped(name="é", meta={"z": [1, None], "a": "b"})
    second = _Stamped(name="b", meta={})
    assert seal(first) and seal(second)
    holder = _Holder(one=first, many=(first, second), plain={"k": "v"})
    expected = canonical.canonical_bytes(holder.model_dump(mode="json"))
    assert canonical.model_canonical_bytes(first)  # remembered on the sealed member
    encoded: list[object] = []
    real = canonical.canonical_bytes
    monkeypatch.setattr(
        canonical, "canonical_bytes", lambda value: encoded.append(value) or real(value)
    )
    # The unsealed holder is composed: only its new member is encoded, never the
    # remembered one, and the bytes are exactly the whole dump's.
    assert canonical.model_canonical_bytes(holder) == expected
    assert len(encoded) == 1 and encoded[0] == second.model_dump(mode="json")
    # A subclass is dumped as its declared class, so it is never composed.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        subclassed = _Stamp(name="q", meta={})
        assert seal(subclassed)
        other = _Holder(one=subclassed, many=(first,), plain={})
        assert canonical.model_canonical_bytes(other) == real(other.model_dump(mode="json"))


def test_the_direct_encoder_writes_exactly_the_normalizing_encoders_bytes():
    import random

    from cruxible_client.contracts import canonical
    from cruxible_client.contracts.errors import CanonicalEncodingError
    from cruxible_client.contracts.primitives import canonical_json, pretty_json

    def outcome(encode, value):  # type: ignore[no-untyped-def]
        try:
            return encode(value)
        except CanonicalEncodingError as exc:
            return (type(exc), str(exc))

    def normalizing(value):  # type: ignore[no-untyped-def]
        return canonical_json(canonical.normalize_canonical(value)).encode("utf-8")

    def normalizing_pretty(value):  # type: ignore[no-untyped-def]
        return pretty_json(canonical.normalize_canonical(value)).encode("utf-8") + b"\n"

    # NFC and decomposed spellings (which collide as keys), surrogates, escapes,
    # astral characters, big integers, bools, sealed containers, and refusals.
    strings = ["", "a", "é", "é", "Å", "\ud800", "x\udfffy", "😀", '\x00\n"\\', "ß"]
    scalars: list[object] = [*strings, 0, -5, 10**30, True, False, None]
    random_values = random.Random(1015)

    def value(depth: int = 0) -> object:
        roll = random_values.random()
        if depth > 3 or roll < 0.3:
            return random_values.choice(scalars)
        if roll < 0.6:
            return [value(depth + 1) for _ in range(random_values.randint(0, 4))]
        return {
            random_values.choice(strings): value(depth + 1)
            for _ in range(random_values.randint(0, 4))
        }

    fixed: list[object] = [
        1.5,
        b"x",
        (1, 2),
        {1: "x"},
        FrozenDict({"b": FrozenList([1, "é"]), "a": None}),
        {"é": 1, "é": 2},
    ]
    for item in [*fixed, *(value() for _ in range(5000))]:
        assert outcome(canonical.canonical_bytes, item) == outcome(normalizing, item)
        assert outcome(canonical.pretty_canonical_bytes, item) == outcome(normalizing_pretty, item)


@pytest.mark.parametrize("shape", ["lists", "dicts", "mixed"])
def test_deeply_nested_values_encode_and_refuse_exactly_as_the_normalizing_encoder(shape):
    """The direct encoder's readiness check (and ``canonical_equal``'s identity
    check) hand any value nested past ``_DIRECT_DEPTH`` to the normalizing path:
    on every supported Python, a value the original accepts gets its exact
    bytes, and one too deep for the original still raises RecursionError."""

    import sys

    from cruxible_client.contracts import canonical
    from cruxible_client.contracts.primitives import canonical_json, pretty_json

    def nested(depth: int) -> object:
        value: object = 1
        for level in range(depth):
            if shape == "lists" or (shape == "mixed" and level % 2):
                value = [value]
            else:
                value = {"k": value}
        return value

    def outcome(encode, value):  # type: ignore[no-untyped-def]
        try:
            return encode(value)
        except RecursionError:
            return RecursionError

    encoders = (
        (
            canonical.canonical_bytes,
            lambda value: canonical_json(canonical.normalize_canonical(value)).encode("utf-8"),
        ),
        (
            canonical.pretty_canonical_bytes,
            lambda value: pretty_json(canonical.normalize_canonical(value)).encode("utf-8") + b"\n",
        ),
    )
    limit = sys.getrecursionlimit()
    threshold = canonical._DIRECT_DEPTH
    # Within the threshold the direct path stands; past it the original path
    # decides, so every outcome (bytes or RecursionError) is the original's on
    # the running Python. 600 is the reviewer's case: refused on 3.11.
    assert canonical._canonical_ready(nested(threshold)) is True
    assert canonical._canonical_ready(nested(threshold + 1)) is False
    accepted = 0
    for depth in (threshold - 1, threshold, threshold + 1, limit // 4, 400, 600, 2 * limit):
        value = nested(depth)
        for encode, original in encoders:
            expected = outcome(original, value)
            accepted += expected is not RecursionError
            assert outcome(encode, value) == expected, (depth, encode.__name__)
        old = encoders[0][1]
        for other in (nested(depth), nested(depth - 1)):
            pair = (value, other)
            equal = outcome(lambda both: canonical.canonical_equal(*both), pair)
            same = outcome(lambda both: old(both[0]) == old(both[1]), pair)
            assert equal == same, depth
    assert accepted >= 2


@pytest.mark.parametrize("sealed", [True, False])
def test_request_carrier_outlives_lru_eviction_and_keeps_detached_values_independent(sealed):
    memo = ParseMemo(capacity=1, max_bytes=1)
    calls = []

    def parsed(body):
        calls.append(body)
        return _Leaf(tags=[body.decode()], meta={}) if sealed else _Loose(note=body.decode())

    with artifact_request():
        first = memo.parse(b"first", "a", "codec", lambda: parsed(b"first"))
        memo.parse(b"second", "b", "codec", lambda: parsed(b"second"))
        memo.clear()  # process eviction cannot evict what the request owns
        with artifact_request():
            again = memo.parse(b"first", "a", "codec", lambda: parsed(b"first"))
        assert first == again
        assert (first is again) is sealed
        if not sealed:
            again.note = "changed"
            assert memo.parse(b"first", "a", "codec", lambda: parsed(b"first")).note == "first"
        assert calls == [b"first", b"second"]
    # Oversized entries were never retained process-wide; a new request is cold.
    with artifact_request():
        assert memo.parse(b"first", "a", "codec", lambda: parsed(b"first")) == first
    assert calls == [b"first", b"second", b"first"]


def test_request_carrier_keys_include_parser_path_codec_and_exact_bytes():
    memo = ParseMemo(capacity=1, max_bytes=1)
    other = ParseMemo(capacity=1, max_bytes=1)
    calls = []

    def parsed():
        calls.append(1)
        return _Leaf(tags=[], meta={})

    with artifact_request():
        first = memo.parse(b"a", "path", "codec", parsed)
        assert memo.parse(b"a", "path", "codec", parsed) is first
        assert memo.parse(b"b", "path", "codec", parsed) is not first
        assert memo.parse(b"a", "other", "codec", parsed) is not first
        assert memo.parse(b"a", "path", "other", parsed) is not first
        assert other.parse(b"a", "path", "codec", parsed) is not first
    assert len(calls) == 5


def test_request_carrier_releases_values_and_never_retains_refusals():
    def refused():
        raise ValueError("refused")

    memo = ParseMemo(capacity=1, max_bytes=1)
    with pytest.raises(RuntimeError), artifact_request():
        first = memo.parse(b"a", "path", "codec", lambda: _Leaf(tags=[], meta={}))
        reference = weakref.ref(first)
        derived = request_derive(("test.leaf",), lambda: _Leaf(tags=[], meta={}))
        derived_reference = weakref.ref(derived)
        for _ in range(2):
            with pytest.raises(ValueError, match="refused"):
                memo.parse(b"refused", "path", "codec", refused)
            with pytest.raises(ValueError, match="refused"):
                request_derive(("test.refused",), refused)
        del first, derived
        raise RuntimeError("release on error")
    gc.collect()
    assert reference() is derived_reference() is None
