"""ClaimType v8: the canonical form literal values are stored and compared in.

Every earlier format keeps its exact bytes, digest and ``model_dump``: the new
field exists on the shared model, is null for them and is never written.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.canonical_forms import (
    CANONICAL_TRANSFORMS,
    canonicalize_literal,
    canonicalize_text,
    is_canonical_literal,
    validate_canonical_form,
)
from cruxible_client.contracts.claim_types import (
    V8_FIELDS,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    effective_canonical_form,
    evaluate_claim_type_law,
    parse_claim_type,
    render_claim_type,
)
from tests.test_claims._claim_type_format_fixtures import historical_claim_types
from tests.test_claims.test_claim_type_v7 import PRE_V7_FINGERPRINTS, v7

PATH = claim_type_path("project.work_item.status")


def v8(form: tuple[str, ...] = ("trim", "casefold"), **update: object) -> ClaimType:
    base = v7().model_dump(mode="python")
    base.update(artifact_format="playbill-claim-type-v8", canonical_form=form)
    base.update(update)
    return ClaimType.model_validate(base)


# --- the transforms ------------------------------------------------------------


@pytest.mark.parametrize(
    ("transform", "raw", "canonical"),
    [
        ("nfc", "é", "é"),
        ("nfkc", "ﬁle", "file"),
        ("trim", " \t a b \n", "a b"),
        ("collapse_whitespace", " a \t\n b ", " a b "),
        ("casefold", "Straße", "strasse"),
        ("upper", "straße", "STRASSE"),
        ("strip_punctuation", "CVE-2024.1 (x)", "CVE20241 x"),
    ],
)
def test_each_transform_is_the_rule_it_names(transform: str, raw: str, canonical: str) -> None:
    assert canonicalize_text(raw, (transform,)) == canonical


def test_the_canonical_value_is_the_fixed_point_so_canonicalizing_is_idempotent() -> None:
    form = ("trim", "strip_punctuation")
    # One pass leaves the space a stripped trailing period exposed; the fixed point does not.
    assert canonicalize_text(" a .", form) == "a"
    for raw in (" a .", "⑴ X", "e.́"):
        for order in (form, ("strip_punctuation", "nfkc", "trim"), ("casefold", "nfc")):
            once = canonicalize_text(raw, order)
            assert canonicalize_text(once, order) == once


def test_a_literal_is_canonical_member_by_member_and_only_text_changes() -> None:
    form = ("trim", "casefold")
    value = {"Name": " A ", "items": [" B", 3, True, None], "n": 2}
    assert canonicalize_literal(value, form) == {
        "Name": "a",
        "items": ["b", 3, True, None],
        "n": 2,
    }
    assert canonicalize_literal(" A ", ()) == " A "
    assert is_canonical_literal("a", form) and not is_canonical_literal(" a", form)


def test_a_form_refuses_unknown_repeated_and_conflicting_transforms() -> None:
    assert len(CANONICAL_TRANSFORMS) == 7
    assert validate_canonical_form(("nfc", "trim", "casefold")) == ("nfc", "trim", "casefold")
    for bad, needle in (
        (("lower",), "unknown canonical transform"),
        (("trim", "trim"), "at most once"),
        (("nfc", "nfkc"), "at most one of nfc and nfkc"),
        (("casefold", "upper"), "at most one of casefold and upper"),
    ):
        with pytest.raises(ValueError, match=needle):
            validate_canonical_form(bad)


# --- the format ------------------------------------------------------------------


def test_every_earlier_format_keeps_its_bytes_digest_and_model_dump() -> None:
    from tests.test_claims._claim_type_format_fixtures import fingerprints

    assert fingerprints() == PRE_V7_FINGERPRINTS
    for claim_type in (*historical_claim_types().values(), v7()):
        assert not set(V8_FIELDS) & set(claim_type.model_dump(mode="json"))
        assert effective_canonical_form(claim_type) == ()


def test_v8_writes_its_form_round_trips_and_digests_apart_from_v7() -> None:
    claim_type = v8()
    assert claim_type.model_dump(mode="json")["canonical_form"] == ["trim", "casefold"]
    assert parse_claim_type(render_claim_type(claim_type), path=PATH) == claim_type
    assert claim_type_digest(claim_type) != claim_type_digest(v7())
    assert effective_canonical_form(claim_type) == ("trim", "casefold")
    empty = v8(())
    assert empty.model_dump(mode="json")["canonical_form"] == []
    assert effective_canonical_form(empty) == ()


def test_v8_states_its_form_and_only_v8_may_carry_one() -> None:
    with pytest.raises(ValidationError, match="states its canonical_form explicitly"):
        v8(None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="only ClaimType v8 and later can carry"):
        v7(canonical_form=("trim",))


def test_a_form_applies_only_to_literals_and_every_enum_member_is_already_canonical() -> None:
    with pytest.raises(ValidationError, match="only to literal ClaimTypes"):
        v8(object_kind="exact_content", literal_schema=None, member_descriptions=())
    with pytest.raises(ValidationError, match="'Done' is not in the declared canonical form"):
        v8(literal_schema={"enum": ["Done", "ready"], "type": "string"})
    assert v8(("upper",), literal_schema={"enum": ["DONE"], "type": "string"})


def test_the_claim_type_law_accepts_v8_under_the_v7_rules() -> None:
    from cruxible_client.contracts.laws import (
        ACCEPTANCE_LAWS,
        CLAIM_TYPE_LAW_V8_REVISION_1,
    )

    accepted = evaluate_claim_type_law(v8(), path=PATH, predecessor=None)
    assert accepted.verdict == "accepted", accepted
    resolution = v7().resolution_policy.model_copy(update={"required_basis_kinds": ("direct",)})
    refused = evaluate_claim_type_law(
        v8(evidence_requirement="none", resolution_policy=resolution), path=PATH, predecessor=None
    )
    assert refused.verdict == "refused"
    assert refused.diagnostics[0].code == "cruxible.claim_type.evidence_requirement_unsatisfiable"
    installed = ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-type-v8")
    assert installed.coordinate == CLAIM_TYPE_LAW_V8_REVISION_1


# --- lowering authored input -----------------------------------------------------


def test_stating_a_form_lowers_to_v8_and_an_edit_keeps_it_unless_named() -> None:
    from tests.test_claims.test_claim_type_v7 import _input, _lower

    plain = _lower(_input(), {})
    assert plain.artifact_format == "playbill-claim-type-v7"
    formed = _lower(_input(canonical_form=["nfc", "trim"]), {})
    assert formed.artifact_format == "playbill-claim-type-v8"
    assert formed.canonical_form == ("nfc", "trim")
    path = claim_type_path(formed.predicate)
    tree = {path: render_claim_type(formed)}
    kept = _lower(_input(), tree)
    assert (kept.artifact_format, kept.canonical_form) == (
        "playbill-claim-type-v8",
        ("nfc", "trim"),
    )
    cleared = _lower(_input(canonical_form=None), tree)
    assert (cleared.artifact_format, cleared.canonical_form) == ("playbill-claim-type-v8", ())
    moved = _lower(_input(canonical_form=["casefold"]), tree)
    assert moved.canonical_form == ("casefold",)
    assert moved.lifecycle.predecessor_digest == claim_type_digest(formed).tagged
    # Over a v7 ClaimType, leaving the field out stays v7.
    assert _lower(_input(), {path: render_claim_type(plain)}).artifact_format == (
        "playbill-claim-type-v7"
    )
