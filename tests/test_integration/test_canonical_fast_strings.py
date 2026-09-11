"""Exact canonical byte and refusal parity at the ASCII optimization boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from enum import Enum, IntEnum, StrEnum

import pytest

from cruxible_client.contracts.canonical import (
    canonical_bytes,
    canonical_digest,
    normalize_canonical,
    pretty_canonical_bytes,
)
from cruxible_client.contracts.errors import CanonicalEncodingError


def _prior_string(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalEncodingError("string contains a non-Unicode surrogate") from exc
    return normalized


def _prior_normalizer(value: object, *, location: str = "$") -> object:
    """Frozen pre-optimization algorithm; never calls the production normalizer."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalEncodingError(f"{location}: floating-point values are forbidden")
    if isinstance(value, bytes):
        raise CanonicalEncodingError(
            f"{location}: runtime bytes are forbidden; binary fields use lowercase hex"
        )
    if isinstance(value, str):
        return _prior_string(value)
    if isinstance(value, list):
        return [
            _prior_normalizer(item, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        items = []
        seen = set()
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise CanonicalEncodingError(f"{location}: object keys must be strings")
            key = _prior_string(raw_key)
            if key in seen:
                raise CanonicalEncodingError(
                    f"{location}: keys collide after NFC normalization: {key!r}"
                )
            seen.add(key)
            items.append((key, _prior_normalizer(raw_value, location=f"{location}.{key}")))
        items.sort(key=lambda item: item[0].encode("utf-8"))
        return dict(items)
    if isinstance(value, Sequence):
        raise CanonicalEncodingError(f"{location}: arrays must be concrete lists")
    raise CanonicalEncodingError(f"{location}: unsupported value type {type(value).__name__}")


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        ("", ""),
        (
            'quote" backslash\\ slash/\b\f\n\r\t\x00\x1f',
            'quote" backslash\\ slash/\b\f\n\r\t\x00\x1f',
        ),
        ("".join(map(chr, range(128))), "".join(map(chr, range(128)))),
        ("caf\u00e9", "caf\u00e9"),
        ("cafe\u0301", "caf\u00e9"),
        ("\u212b", "\u00c5"),
        ("A\u030a\u0301", "\u01fa"),
        (
            "\u007f\u0080\u07ff\u0800\ud7ff\ue000\uffff\U00010000\U0010ffff",
            "\u007f\u0080\u07ff\u0800\ud7ff\ue000\uffff\U00010000\U0010ffff",
        ),
    ],
    ids=[
        "empty",
        "escapes",
        "all-ascii",
        "nfc",
        "decomposed",
        "angstrom",
        "combining",
        "scalar-boundaries",
    ],
)
def test_string_bytes_pretty_bytes_and_domain_digest_are_unchanged(
    value: str, normalized: str
) -> None:
    payload = {"z": [False, 0, None, value], "a": value}
    expected = {"a": normalized, "z": [False, 0, None, normalized]}
    assert normalize_canonical(payload) == _prior_normalizer(payload) == expected
    assert canonical_bytes(payload) == _json_bytes(expected)
    assert pretty_canonical_bytes(payload) == (
        json.dumps(expected, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    assert (
        canonical_digest("test-canonical-string-v1", payload)
        == hashlib.sha256(_json_bytes({"tag": "test-canonical-string-v1", **expected})).hexdigest()
    )


def test_ascii_escapes_have_exact_literal_json_spelling() -> None:
    assert canonical_bytes('"\\\b\f\n\r\t\x00\x1f\x7f') == (
        b'"\\"\\\\\\b\\f\\n\\r\\t\\u0000\\u001f\x7f"'
    )


def test_large_base64_keeps_exact_bytes_and_digest() -> None:
    encoded = base64.b64encode(bytes(range(256)) * 384).decode("ascii")
    expected = b'{"body":"' + encoded.encode("ascii") + b'","tag":"capture-test-v1"}'
    assert len(encoded) == 131_072
    assert canonical_bytes({"tag": "capture-test-v1", "body": encoded}) == expected
    assert (
        canonical_digest("capture-test-v1", {"body": encoded})
        == hashlib.sha256(expected).hexdigest()
    )
    assert normalize_canonical(encoded) == _prior_normalizer(encoded)


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff", "\ud83d\ude00"])
@pytest.mark.parametrize("position", ["value", "key", "nested"])
def test_surrogates_are_refused_even_when_a_pair_looks_like_an_escaped_scalar(
    surrogate: str, position: str
) -> None:
    value = {
        "value": surrogate,
        "key": {surrogate: "ascii"},
        "nested": {"outer": ["ascii", surrogate]},
    }[position]
    for normalizer in (_prior_normalizer, normalize_canonical):
        with pytest.raises(CanonicalEncodingError, match="non-Unicode surrogate") as caught:
            normalizer(value)
        assert isinstance(caught.value.__cause__, UnicodeEncodeError)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"outer": [{"bad": 1.5}]}, "$.outer[0].bad: floating-point values are forbidden"),
        ({"outer": [{"bad": float("nan")}]}, "$.outer[0].bad: floating-point values are forbidden"),
        (
            {"outer": [{"bad": b"ascii"}]},
            "$.outer[0].bad: runtime bytes are forbidden; binary fields use lowercase hex",
        ),
        ({"outer": [("ascii",)]}, "$.outer[0]: arrays must be concrete lists"),
        ({"outer": [{1: "ascii"}]}, "$.outer[0]: object keys must be strings"),
        ({"outer": [bytearray(b"ascii")]}, "$.outer[0]: arrays must be concrete lists"),
        ({"outer": [{"bad": object()}]}, "$.outer[0].bad: unsupported value type object"),
        ({"e\u0301": [float("inf")]}, "$.\u00e9[0]: floating-point values are forbidden"),
        (
            {"outer": {"e\u0301": 1, "\u00e9": 2}},
            "$.outer: keys collide after NFC normalization: '\u00e9'",
        ),
    ],
)
def test_nested_refusals_keep_exact_error_locations(value: object, message: str) -> None:
    for normalizer in (_prior_normalizer, normalize_canonical):
        with pytest.raises(CanonicalEncodingError) as caught:
            normalizer(value)
        assert str(caught.value) == message


def test_nfc_key_order_and_normalized_values_keep_exact_bytes() -> None:
    value = {"\U00010000": "last", "z": "ascii", "e\u0301": "A\u030a", "\ue000": "middle"}
    expected = {"z": "ascii", "\u00e9": "\u00c5", "\ue000": "middle", "\U00010000": "last"}
    assert list(normalize_canonical(value)) == list(expected)
    assert canonical_bytes(value) == _json_bytes(expected)
    assert _prior_normalizer(value) == expected


class _EncodingHook(str):
    def isascii(self) -> bool:
        raise AssertionError("a string subclass must not enter the exact-string shortcut")

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        raise UnicodeEncodeError(encoding, self, 0, 1, "subclass encoding hook")


@pytest.mark.parametrize("as_key", [False, True])
def test_string_subclass_encoding_hook_retains_its_refusal(as_key: bool) -> None:
    text = _EncodingHook("ascii")
    value = {text: "value"} if as_key else {"value": text}
    for normalizer in (_prior_normalizer, normalize_canonical):
        with pytest.raises(CanonicalEncodingError) as caught:
            normalizer(value)
        assert isinstance(caught.value.__cause__, UnicodeEncodeError)
        assert caught.value.__cause__.reason == "subclass encoding hook"


class _Status(str, Enum):
    READY = "ready"


class _Label(StrEnum):
    CAFE = "cafe\u0301"


class _Count(IntEnum):
    ONE = 1


class _Unsupported(Enum):
    READY = "ready"


def test_string_subclasses_and_enums_keep_prior_scalar_interpretations() -> None:
    class PlainString(str):
        pass

    value = {_Status.READY: [_Status.READY, _Label.CAFE, _Count.ONE, PlainString("ascii")]}
    expected = {"ready": ["ready", "caf\u00e9", 1, "ascii"]}
    assert normalize_canonical(value) == _prior_normalizer(value) == expected
    assert canonical_bytes(value) == _json_bytes(expected)
    for normalizer in (_prior_normalizer, normalize_canonical):
        with pytest.raises(CanonicalEncodingError, match="unsupported value type _Unsupported"):
            normalizer(_Unsupported.READY)
