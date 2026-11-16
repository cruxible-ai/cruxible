"""The vocabulary file's format: the strict loader, the shorthands, and export's round trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.authoring.vocabulary_file import (
    AcceptedVocabulary,
    entry_input,
    lower_entry,
    render_vocabulary_file,
)
from cruxible_client.contracts.claim_type_inputs import (
    ClaimTypeInputRecord,
    lower_claim_type_input,
)
from cruxible_client.contracts.claim_types import (
    ClaimFreshnessDuration,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.shapes import EnumType, SubjectType
from cruxible_client.contracts.subject_kinds import SubjectKind
from cruxible_client.contracts.vocabulary_file import (
    VocabularyField,
    VocabularyFileError,
    load_strict_yaml,
    load_vocabulary_file,
    read_vocabulary_header,
)

COORDINATE = AcceptedCoordinate(
    git_oid="a" * 40,
    semantic_root="sha256:" + "b" * 64,
    generation_root="sha256:" + "c" * 64,
    compiler_digest="sha256:" + "d" * 64,
)

HEADER = f"""format: cruxible-vocabulary-v1
instance: inst_x
based_on:
  git_oid: {"a" * 40}
  semantic_root: sha256:{"b" * 64}
  generation_root: sha256:{"c" * 64}
  compiler_digest: sha256:{"d" * 64}
  vocabulary_digest: sha256:{"e" * 64}
"""


def _type(**stated: object):  # type: ignore[no-untyped-def]
    return lower_claim_type_input(ClaimTypeInputRecord.model_validate(stated), tree={})


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        ("a: 1\na: 2\n", "appears twice"),
        ("a: yes\n", "reads as a boolean"),
        ("a: True\n", "reads as a boolean"),
        ("a: 1.0\n", "reads as a decimal number"),
        ("a: 2026-11-15\n", "reads as a date"),
        ("a: 0o17\n", "reads as a number"),
        ("a: 1e3\n", "reads as a number"),
        ("a: 1E+3\n", "reads as a number"),
        ("a: -2.5e-2\n", "reads as a decimal number"),
        ("a: .5e-2\n", "reads as"),
        ("a: 017\n", "reads as a number"),
        ("a:\n", "an empty value"),
        ("a: ~\n", "reads as null"),
        ("a: &x 1\nb: *x\n", "an anchor"),
        ("base: {x: 1}\nother:\n  <<: {x: 1}\n", "merge key"),
        ("1: one\n", "is not text"),
        ("a: [unclosed\n", "not valid YAML"),
    ],
)
def test_the_strict_loader_refuses_what_a_reader_could_take_two_ways(
    text: str, refusal: str
) -> None:
    with pytest.raises(VocabularyFileError, match=refusal):
        load_strict_yaml(text)


def test_the_strict_loader_reads_the_plain_spellings() -> None:
    assert load_strict_yaml("a: true\nb: 12\nc: null\nd: '1.0'\ne: '0.5.0'\nf: 0.5.0\n") == {
        "a": True,
        "b": 12,
        "c": None,
        "d": "1.0",
        "e": "0.5.0",
        "f": "0.5.0",
    }


@pytest.mark.parametrize(
    ("body", "refusal"),
    [
        ("kinds:\n  dev.item:\n    colour: red\n", "colour"),
        (
            "kinds:\n  dev.item:\n    fields:\n      slug: {shape: x}\n",
            "unknown field property shape",
        ),
        ("kinds:\n  dev.item:\n    fields:\n      slug: {predicate: x.y}\n", "predicate is not"),
        ("kinds:\n  dev.item:\n    fields:\n      s: {enum: [a], link: dev.item}\n", "not both"),
        ("kinds:\n  dev.item:\n    fields:\n      s: {enum: [a], value: int}\n", "drop value"),
        ("kinds:\n  dev.item:\n    fields:\n      s: {key: true, many: true}\n", "a key holds one"),
        ("kinds:\n  dev.item:\n    fields:\n      s: {reason: superseded}\n", "reason goes with"),
        ("kinds:\n  dev.item:\n    fields:\n      s: {many: true, cardinality: many}\n", "many"),
        (
            "kinds:\n  dev.item:\n    fields:\n      s:\n"
            "        enum: {a: {becomes: b, retire: superseded}}\n",
            "becomes",
        ),
        ("kinds:\n  dev.item:\n    fields:\n      s: {cardinality: some}\n", "cardinality"),
        ("kinds:\n  dev.item:\n    fields:\n      s: {many: 'true'}\n", "many"),
    ],
)
def test_unknown_or_contradictory_keys_refuse_naming_the_field(body: str, refusal: str) -> None:
    with pytest.raises(VocabularyFileError, match=refusal):
        load_vocabulary_file(HEADER + body)


def test_a_whole_file_refuses_scientific_enum_members_unless_quoted() -> None:
    body = "kinds:\n  dev.item:\n    fields:\n      size:\n        enum: [{members}]\n"
    with pytest.raises(VocabularyFileError, match="1e3"):
        load_vocabulary_file(HEADER + body.format(members="1e3, 1E+3"))
    quoted = load_vocabulary_file(HEADER + body.format(members="'1e3', '1E+3'"))
    assert quoted.kinds["dev.item"].fields["size"].enum == ("1e3", "1E+3")


def test_the_header_is_read_without_the_kinds() -> None:
    broken = HEADER + "kinds:\n  dev.item: [this is not a kind\n"
    baseline = read_vocabulary_header(broken)
    assert baseline is not None and baseline.git_oid == "a" * 40
    assert read_vocabulary_header("format: other\n") is None
    assert read_vocabulary_header("not: [yaml\n") is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("git_oid", "not-an-oid"),
        ("semantic_root", "sha256:short"),
        ("compiler_digest", "nope"),
        ("vocabulary_digest", "sha256:XYZ"),
    ],
)
def test_a_malformed_baseline_is_unreadable_not_forwarded(field: str, value: str) -> None:
    current = {
        "git_oid": "a" * 40,
        "semantic_root": "sha256:" + "b" * 64,
        "compiler_digest": "sha256:" + "d" * 64,
        "vocabulary_digest": "sha256:" + "e" * 64,
    }
    broken = HEADER.replace(current[field], value)
    assert read_vocabulary_header(broken) is None
    with pytest.raises(VocabularyFileError, match="based_on"):
        load_vocabulary_file(broken + "kinds: {}\n")


def test_shorthands_expand_to_the_claim_type_input() -> None:
    entry = VocabularyField.model_validate(
        {"enum": {"open": "Not done.", "done": None}, "many": True, "description": "State."}
    )
    record = entry_input("dev.item.state", entry, None)
    assert record.value == EnumType(members=("open", "done"))
    assert record.cardinality == "many"
    assert [(item.member, item.description) for item in record.member_descriptions or ()] == [
        ("open", "Not done.")
    ]
    link = entry_input(
        "dev.item.blocks", VocabularyField.model_validate({"link": ["b.k", "a.k"]}), None
    )
    assert link.value == SubjectType(subject_kinds=("a.k", "b.k"))
    assert link.cardinality == "one"


def test_an_edit_keeps_what_the_entry_leaves_out() -> None:
    accepted = _type(
        predicate="dev.item.blocks",
        cardinality="many",
        value="subject<dev.item>",
        description="Items it blocks.",
    )
    # Neither many nor description stated: both keep the accepted value.
    edited = lower_entry(
        "dev.item.blocks", VocabularyField.model_validate({"link": "dev.item"}), accepted
    )
    assert edited.cardinality == "many" and edited.description == "Items it blocks."
    assert edited.lifecycle.predecessor_digest == claim_type_digest(accepted).tagged
    # A stated cardinality change takes the short form's resolution policy for it.
    one = lower_entry(
        "dev.item.blocks",
        VocabularyField.model_validate({"link": "dev.item", "many": False}),
        accepted,
    )
    assert one.cardinality == "one" and one.resolution_policy.selector == "only_contender"
    tree = {claim_type_path(accepted.predicate): render_claim_type(accepted)}
    assert one == lower_claim_type_input(
        entry_input(
            "dev.item.blocks",
            VocabularyField.model_validate({"link": "dev.item", "many": False}),
            accepted,
        ),
        tree=tree,
    )


def test_export_writes_shorthand_and_full_form_and_reads_back_to_each_digest() -> None:
    types = [
        _type(
            predicate="dev.item.slug",
            cardinality="one",
            value='string{pattern: "^[a-z0-9-]+$", min_len: 1, max_len: 80}',
        ),
        _type(
            predicate="dev.item.state",
            cardinality="one",
            value={"kind": "enum", "members": ["open", "done"]},
            member_descriptions=[{"member": "open", "description": "Not done."}],
            default_role="normative",
        ),
        _type(
            predicate="dev.item.tags", cardinality="many", value={"kind": "enum", "members": ["a"]}
        ),
        _type(
            predicate="dev.item.seen",
            cardinality="one",
            value="string{max_len: 64}",
            permitted_roles=["observation"],
            evidence=["osv.dev"],
        ),
        _type(
            predicate="dev.item.count",
            cardinality="one",
            value="int",
            revision_evidence="accumulate",
            evidence_freshness={"stale_after": "P30D"},
            canonical_form=[],
        ),
        _type(
            predicate="dev.item.parent",
            cardinality="one",
            value="subject<dev.item | dev.note>",
            allowed_subject_kinds=["dev.item", "dev.note"],
        ),
    ]
    keyed = SubjectKind.model_validate(
        {
            "identity": {"kind": "SubjectKind", "name": "dev.item"},
            "description": "A planned piece of work.",
            "identity_rule": {
                "rule": "keys",
                "parts": [
                    {
                        "claim_type": "dev.item.slug",
                        "normalizer": {"normalizer": "lowercase", "version": 1},
                    }
                ],
                "uniqueness": "hint",
            },
        }
    )
    minted = SubjectKind.model_validate(
        {
            "identity": {"kind": "SubjectKind", "name": "dev.note"},
            "identity_rule": {"rule": "minted"},
        }
    )
    accepted = AcceptedVocabulary(
        coordinate=COORDINATE,
        claim_types={item.predicate: item for item in types},
        subject_kinds={"dev.item": keyed, "dev.note": minted},
        kits={"ClaimType:dev.item.count": "acme", "SubjectKind:dev.note": "acme"},
    )

    rendered = render_vocabulary_file(accepted, instance_id="inst_x")

    assert rendered == render_vocabulary_file(accepted, instance_id="inst_x")
    assert "      # kit: acme\n      count:" in rendered
    assert "  # kit: acme\n  dev.note:" in rendered
    parsed = load_vocabulary_file(rendered)
    assert parsed.based_on.vocabulary_digest == accepted.digest
    item = parsed.kinds["dev.item"]
    assert item.uniqueness == "hint" and item.normalizers is not None
    assert item.fields["slug"].key is True
    assert item.fields["state"].enum == {"open": "Not done.", "done": None}
    assert item.fields["tags"].enum == ("a",)
    assert item.fields["seen"].stated == {
        "value": "string{max_len: 64}",
        "permitted_roles": ["observation"],
        "evidence": ["osv.dev"],
    }
    assert item.fields["count"].stated["evidence_freshness"] == {"stale_after": "PT2592000S"}
    assert item.fields["parent"].link == ("dev.item", "dev.note")
    assert parsed.kinds["dev.note"].minted is True
    for claim_type in types:
        kind, _, name = claim_type.predicate.rpartition(".")
        lowered = lower_entry(claim_type.predicate, parsed.kinds[kind].fields[name], None)
        assert claim_type_digest(lowered) == claim_type_digest(claim_type), claim_type.predicate


_KIT_FIXTURE = Path(__file__).parents[1] / "fixtures" / "security_kit_vocabulary.json"


def _kit_vocabulary() -> AcceptedVocabulary:
    data = json.loads(_KIT_FIXTURE.read_text(encoding="utf-8"))
    types = [ClaimType.model_validate(item) for item in data["claim_types"]]
    kinds = [SubjectKind.model_validate(item) for item in data["subject_kinds"]]
    return AcceptedVocabulary(
        coordinate=COORDINATE,
        claim_types={item.predicate: item for item in types},
        subject_kinds={item.subject_kind: item for item in kinds},
    )


def test_every_shape_the_security_kit_uses_exports_and_lowers_back() -> None:
    accepted = _kit_vocabulary()
    shapes = {
        "inverse": "security.component.affected_by",
        "enum with meanings": "security.app_exposure.verdict",
        "requires complete input": "security.finding.verification",
        "decimal": "security.vulnerability.epss_score",
        "date": "security.vulnerability.kev_date_added",
    }
    assert accepted.claim_types["security.vulnerability.listed_in"].inverse is not None
    for predicate in shapes.values():
        assert predicate in accepted.claim_types

    rendered = render_vocabulary_file(accepted, instance_id="inst_x")

    parsed = load_vocabulary_file(rendered)
    assert parsed.kinds["security.component"].fields["purl"].key is True
    assert parsed.kinds["security.exposure"].fields["component"].link == "security.component"
    for claim_type in accepted.claim_types.values():
        kind, _, name = claim_type.predicate.rpartition(".")
        entry = parsed.kinds[kind].fields[name]
        # As a new definition, and as an unchanged edit over the accepted one.
        lowered = lower_entry(claim_type.predicate, entry, None)
        assert claim_type_digest(lowered) == claim_type_digest(claim_type), claim_type.predicate
        edited = lower_entry(claim_type.predicate, entry, claim_type)
        assert edited.model_copy(update={"lifecycle": claim_type.lifecycle}) == claim_type


def test_an_inverse_with_its_own_horizon_and_meaning_exports_and_lowers_back() -> None:
    accepted = _kit_vocabulary()
    base = accepted.claim_types["security.component.affected_by"]
    assert base.inverse is not None
    varied = base.model_copy(
        update={
            "inverse": base.inverse.model_copy(
                update={
                    "stale_after": ClaimFreshnessDuration(microseconds=7 * 86_400_000_000),
                    "description": "No vulnerability affects this exact version.",
                }
            )
        }
    )
    vocabulary = AcceptedVocabulary(
        coordinate=COORDINATE,
        claim_types={varied.predicate: varied},
        subject_kinds={},
    )

    parsed = load_vocabulary_file(render_vocabulary_file(vocabulary, instance_id="inst_x"))

    entry = parsed.kinds["security.component"].fields["affected_by"]
    assert entry.stated["inverse"]["stale_after"] == "PT604800S"
    assert claim_type_digest(lower_entry(varied.predicate, entry, None)) == claim_type_digest(
        varied
    )


def test_export_refusal_names_every_definition_that_cannot_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.authoring import vocabulary_file

    accepted = _kit_vocabulary()
    broken = {"security.finding.locus", "security.catalog.catalog_id"}
    real = vocabulary_file._minimal_input

    def minimal(claim_type: ClaimType) -> dict[str, object]:
        if claim_type.predicate in broken:
            raise vocabulary_file.VocabularyExportRefused("cannot")
        return real(claim_type)

    monkeypatch.setattr(vocabulary_file, "_minimal_input", minimal)

    with pytest.raises(vocabulary_file.VocabularyExportRefused) as refused:
        render_vocabulary_file(accepted, instance_id="inst_x")

    message = str(refused.value)
    assert "2 definition(s)" in message
    for predicate in broken:
        assert f"ClaimType:{predicate}" in message
