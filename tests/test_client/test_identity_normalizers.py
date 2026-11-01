"""SubjectKind identity normalizers: the fixed core set, its conformance fixtures (R12, D4).

Each normalizer version has one conformance fixture under
``tests/fixtures/identity_normalizers/<name>-v<N>.json``: raw values and the
canonical value (or the refusal) the version gives them. A fixture's bytes are
pinned below: a normalizer version never changes what it does, so changing a
case means a new version (and a new fixture), never an edited one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.identity_normalizers import (
    NORMALIZER_VERSIONS,
    KeyNormalizationRefused,
    KeyNormalizer,
    PurlTypeNormalizer,
    normalize_key_value,
    normalizer_spelling,
)
from cruxible_client.contracts.subject_kinds import (
    KeyClaimTypeShape,
    key_claim_type_problem,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "identity_normalizers"
_RULE: TypeAdapter[KeyNormalizer] = TypeAdapter(KeyNormalizer)

#: sha256 of each conformance fixture's bytes. A normalizer version is
#: immutable: a changed case is a new version with its own fixture.
FIXTURE_DIGESTS = {
    "none-v1.json": "4f210de24a290951a5b579ba0161dfe372be22af9869c32ba39f7e4bf160cf18",
    "lowercase-v1.json": "05f356e4aed90abcc695baf40e0336951fe01e4fd3c301335b10e121bad1e5ad",
    "pep503-v1.json": "6c01254bf78bd00d3bb8f1807060d3ac6855908a33030a4889031632d1fb2c8a",
    "purl-type-v1.json": "c2adbad0f4aaf7ccf470197593dd688fe99fa0434c08970c4069e133be1ba988",
}


def test_every_implemented_normalizer_version_has_exactly_one_pinned_fixture() -> None:
    expected = {
        f"{name}-v{version}.json"
        for name, versions in NORMALIZER_VERSIONS.items()
        for version in versions
    }
    assert {path.name for path in FIXTURES.glob("*.json")} == expected == set(FIXTURE_DIGESTS)
    for name, digest in FIXTURE_DIGESTS.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, name


def _cases() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    found = []
    for path in sorted(FIXTURES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for index, case in enumerate(data["cases"]):
            found.append((f"{path.stem}[{index}]", data["normalizer"], case))
    return found


@pytest.mark.parametrize(
    ("label", "rule", "case"), _cases(), ids=[label for label, _r, _c in _cases()]
)
def test_conformance_fixture(label: str, rule: dict[str, Any], case: dict[str, Any]) -> None:
    normalizer = _RULE.validate_python(rule)
    if "refused" in case:
        with pytest.raises(KeyNormalizationRefused) as caught:
            normalize_key_value(normalizer, case["raw"])
        assert caught.value.code == case["refused"]
        return
    canonical = normalize_key_value(normalizer, case["raw"])
    assert canonical == case["canonical"] and type(canonical) is type(case["canonical"])
    # Canonicalization is idempotent: the canonical value is its own canonical value.
    assert normalize_key_value(normalizer, canonical) == canonical
    if isinstance(normalizer, PurlTypeNormalizer):
        variant = PurlTypeNormalizer(version=normalizer.version, identity="package_variant")
        assert normalize_key_value(variant, case["raw"]) == case["variant"]
        assert normalize_key_value(variant, case["variant"]) == case["variant"]
        # The package identity of a variant is the package itself.
        assert normalize_key_value(normalizer, case["variant"]) == canonical


def test_two_raw_spellings_of_one_package_normalize_to_one_key() -> None:
    package = PurlTypeNormalizer(version=1, identity="package")
    variant = PurlTypeNormalizer(version=1, identity="package_variant")
    amd = "pkg:deb/debian/curl@8.5.0-2?arch=amd64"
    arm = "pkg:deb/Debian/curl@8.5.0-2?arch=arm64"
    assert normalize_key_value(package, amd) == normalize_key_value(package, arm)
    assert normalize_key_value(variant, amd) != normalize_key_value(variant, arm)


@pytest.mark.parametrize(
    "rule",
    [
        {"normalizer": "pep503", "version": 2},
        {"normalizer": "none", "version": 0},
        {"normalizer": "pep503"},
        {"normalizer": "purl-type", "version": 1},
        {"normalizer": "purl-type", "version": 1, "identity": "ecosystem"},
        {"normalizer": "nfkc", "version": 1},
        {"normalizer": "lowercase", "version": 1, "locale": "tr"},
    ],
)
def test_only_the_fixed_core_set_at_implemented_versions_is_accepted(
    rule: dict[str, Any],
) -> None:
    """No configurable normalization: a rule is one core name at one pinned version."""

    with pytest.raises(ValidationError):
        _RULE.validate_python(rule)


def test_a_rule_spells_its_name_version_and_purl_identity() -> None:
    assert normalizer_spelling(_RULE.validate_python({"normalizer": "pep503", "version": 1})) == (
        "pep503@1"
    )
    assert (
        normalizer_spelling(PurlTypeNormalizer(version=1, identity="package_variant"))
        == "purl-type@1 (package_variant)"
    )


def _shape(**update: Any) -> KeyClaimTypeShape:
    base: dict[str, Any] = {
        "predicate": "pkg.package.name",
        "live": True,
        "artifact_format": "cruxible-claim-type-v10",
        "object_kind": "literal",
        "cardinality": "one",
        "allowed_subject_kinds": ("pkg.package",),
        "literal_type": "string",
        "canonical_form": ("nfc", "trim"),
    }
    return KeyClaimTypeShape(**{**base, **update})


def test_a_text_normalizer_needs_a_string_key_claim_type() -> None:
    pep503 = _RULE.validate_python({"normalizer": "pep503", "version": 1})
    none = _RULE.validate_python({"normalizer": "none", "version": 1})
    assert key_claim_type_problem("pkg.package", _shape(), "pkg.package.name", pep503) is None
    integer = _shape(literal_type="integer")
    problem = key_claim_type_problem("pkg.package", integer, "pkg.package.name", pep503)
    assert problem is not None and "normalizes text" in problem
    assert key_claim_type_problem("pkg.package", integer, "pkg.package.name", none) is None


def test_a_lowercasing_normalizer_cannot_follow_an_upper_canonical_form() -> None:
    upper = _shape(canonical_form=("nfc", "upper"))
    for name in ("lowercase", "pep503"):
        rule = _RULE.validate_python({"normalizer": name, "version": 1})
        problem = key_claim_type_problem("pkg.package", upper, "pkg.package.name", rule)
        assert problem is not None and "upper" in problem
