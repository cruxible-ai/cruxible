"""An authoring Claim names a Subject by kind and key, never by a predicted id.

A change-set Claim's subject, or its Subject-valued object, may be
``{"subject_kind": ..., "key": {...}}`` (one key part or several), mirroring a
Procedure v3 ``SubjectKeyReference``: it resolves at lowering to the Subject a
creation member of the same set lands on, else through the common resolver over
the base -- none refuses ``key_missing``, several ``key_ambiguous`` -- and a
reference the same change's own identity changes would redirect refuses
``identity_reference_unstable``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import (
    ClaimDispositionInput,
    ClaimInput,
    ClaimTypeInput,
    LiteralObjectInput,
    SelfSourceInput,
    SubjectInput,
    SubjectKindInput,
    SubjectObjectInput,
)
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.subject_kinds import SubjectKeyReference
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._identity_support import identity_claim_type
from tests.core_support._support import initialize_local
from tests.test_service.test_subject_identity import accept, change_set, compile_

ASSESSMENT = "sec.assessment"
FINDING = "sec.finding"
PACKAGE = "sec.package"
FORM = ("nfkc", "trim", "collapse_whitespace", "casefold")


def _key(kind: str, field: str) -> Any:
    return identity_claim_type(
        field,
        canonical_form=FORM,
        literal_schema={"type": "string"},
        predicate=f"{kind}.{field}",
        subject_kinds=(kind,),
    )


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    """Assessments keyed by (release, cve), unique; findings keyed alike, hint."""

    instance, _owner = initialize_local(tmp_path)
    members: list[Any] = []
    for kind, uniqueness in ((ASSESSMENT, "unique"), (FINDING, "hint")):
        members += [
            ClaimTypeInput(kind="claim_type", claim_type=_key(kind, "release")),
            ClaimTypeInput(kind="claim_type", claim_type=_key(kind, "cve")),
            SubjectKindInput(
                kind="subject_kind",
                name=kind,
                keys=(f"{kind}.release", f"{kind}.cve"),
                uniqueness=uniqueness,
            ),
        ]
    members += [
        ClaimTypeInput(
            kind="claim_type",
            claim_type=identity_claim_type(
                "status",
                canonical_form=None,
                literal_schema={"type": "string"},
                predicate=f"{ASSESSMENT}.status",
                subject_kinds=(ASSESSMENT,),
            ),
        ),
        ClaimTypeInput(
            kind="claim_type",
            claim_type=identity_claim_type(
                "supersedes",
                canonical_form=None,
                object_kind="subject",
                object_kinds=(ASSESSMENT,),
                predicate=f"{ASSESSMENT}.supersedes",
                subject_kinds=(ASSESSMENT,),
            ),
        ),
        ClaimTypeInput(kind="claim_type", claim_type=_key(PACKAGE, "slug")),
        SubjectKindInput(
            kind="subject_kind", name=PACKAGE, keys=(f"{PACKAGE}.slug",), uniqueness="unique"
        ),
        ClaimTypeInput(
            kind="claim_type",
            claim_type=identity_claim_type(
                "title",
                canonical_form=None,
                literal_schema={"type": "string"},
                predicate=f"{PACKAGE}.title",
                subject_kinds=(PACKAGE,),
            ),
        ),
        ClaimTypeInput(
            kind="claim_type",
            claim_type=identity_claim_type(
                "affects",
                canonical_form=None,
                object_kind="subject",
                object_kinds=(PACKAGE,),
                predicate=f"{ASSESSMENT}.affects",
                subject_kinds=(ASSESSMENT,),
            ),
        ),
        ClaimTypeInput(
            kind="claim_type",
            claim_type=identity_claim_type(
                "note",
                canonical_form=None,
                literal_schema={"type": "string"},
                predicate=f"{FINDING}.note",
                subject_kinds=(FINDING,),
            ),
        ),
    ]
    accept(instance, change_set(*members))
    return instance


def by_key(kind: str, release: str, cve: str) -> SubjectKeyReference:
    return SubjectKeyReference(subject_kind=kind, key={"release": release, "cve": cve})


def claim(subject: Any, predicate: str, value: Any, **fields: Any) -> ClaimInput:
    return ClaimInput(
        kind="claim",
        subject=subject,
        predicate=predicate,
        object=value
        if isinstance(value, SubjectObjectInput)
        else LiteralObjectInput(kind="literal", value=value),
        role="observation",
        rationale="Triaged.",
        source=SelfSourceInput(kind="self_source", body=f"{predicate}: {value}"),
        **fields,
    )


def create(kind: str, release: str, cve: str, **fields: Any) -> SubjectInput:
    return SubjectInput(
        kind="subject", subject_kind=kind, key={"release": release, "cve": cve}, **fields
    )


def live(instance: PlaybillInstance, subject_id: str, predicate: str) -> dict[str, Any]:
    """Each live Claim of ``predicate`` about the assessment, by Claim path."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    found: dict[str, Any] = {}
    for path, content in tree.items():
        if not path.startswith("claims/"):
            continue
        parsed = parse_claim(content, path=path)
        if (
            parsed.lifecycle.state == "live"
            and parsed.statement.predicate == predicate
            and parsed.statement.subject.artifact_path == f"subjects/{subject_id}.json"
        ):
            found[path] = parsed
    return found


def refusal(instance: PlaybillInstance, *members: Any) -> Any:
    result = compile_(instance, change_set(*members))
    assert result.verdict == "refused", result
    (diagnostic,) = result.frontier.diagnostics
    return diagnostic


def test_a_claim_names_an_existing_subject_by_its_composite_key(
    instance: PlaybillInstance,
) -> None:
    accept(instance, change_set(create(ASSESSMENT, "R1", "CVE-2021-44228")))

    # Spelled as the run read it; the key's canonical form finds the one holder.
    accept(
        instance,
        change_set(
            claim(by_key(ASSESSMENT, " r1 ", "cve-2021-44228"), f"{ASSESSMENT}.status", "open")
        ),
    )

    (status,) = live(instance, f"{ASSESSMENT}/r1--cve-2021-44228", f"{ASSESSMENT}.status").values()
    assert status.statement.object.value == "open"  # type: ignore[union-attr]


def test_a_key_a_creation_member_of_the_same_set_holds_names_that_creation(
    instance: PlaybillInstance,
) -> None:
    accept(
        instance,
        change_set(
            create(ASSESSMENT, "R2", "CVE-2014-0160"),
            claim(by_key(ASSESSMENT, "R2", "CVE-2014-0160"), f"{ASSESSMENT}.status", "new"),
        ),
    )

    assert live(instance, f"{ASSESSMENT}/r2--cve-2014-0160", f"{ASSESSMENT}.status")


def test_a_subject_valued_object_names_its_subject_by_key(instance: PlaybillInstance) -> None:
    accept(
        instance,
        change_set(create(ASSESSMENT, "r1", "cve-1"), create(ASSESSMENT, "r2", "cve-1")),
    )

    accept(
        instance,
        change_set(
            claim(
                by_key(ASSESSMENT, "R2", "CVE-1"),
                f"{ASSESSMENT}.supersedes",
                SubjectObjectInput(kind="subject", subject=by_key(ASSESSMENT, "R1", "CVE-1")),
            )
        ),
    )

    (link,) = live(instance, f"{ASSESSMENT}/r2--cve-1", f"{ASSESSMENT}.supersedes").values()
    assert link.statement.object.address.artifact_path == (  # type: ignore[union-attr]
        f"subjects/{ASSESSMENT}/r1--cve-1.json"
    )


def test_a_key_no_subject_holds_refuses_key_missing(instance: PlaybillInstance) -> None:
    diagnostic = refusal(
        instance, claim(by_key(ASSESSMENT, "r9", "cve-9"), f"{ASSESSMENT}.status", "open")
    )

    assert diagnostic.code == "cruxible.subject.key_missing"
    assert '"kind": "subject"' in diagnostic.repairs[0].description


def test_a_key_two_subjects_hold_refuses_key_ambiguous(instance: PlaybillInstance) -> None:
    accept(instance, change_set(create(FINDING, "r1", "cve-1")))
    accept(instance, change_set(create(FINDING, "r1", "cve-1", create_new=True)))

    diagnostic = refusal(
        instance, claim(by_key(FINDING, "r1", "cve-1"), f"{FINDING}.note", "which one?")
    )

    assert diagnostic.code == "cruxible.subject.key_ambiguous"


def test_a_reference_the_same_change_moves_the_key_of_refuses_as_unstable(
    instance: PlaybillInstance,
) -> None:
    accept(instance, change_set(create(ASSESSMENT, "r1", "cve-1")))
    subject = f"{ASSESSMENT}/r1--cve-1"
    (release_claim,) = live(instance, subject, f"{ASSESSMENT}.release")

    diagnostic = refusal(
        instance,
        # Moves the key from (r1, cve-1) to (r3, cve-1) ...
        claim(
            subject,
            f"{ASSESSMENT}.release",
            "r3",
            revises=release_claim.rsplit("/", 1)[-1].removesuffix(".json"),
        ),
        # ... while a sibling still names the Subject by the key it leaves.
        claim(by_key(ASSESSMENT, "r1", "cve-1"), f"{ASSESSMENT}.status", "open"),
    )

    assert diagnostic.code == "cruxible.authoring.identity_reference_unstable"


def test_a_reference_by_a_kind_with_no_keys_refuses(instance: PlaybillInstance) -> None:
    diagnostic = refusal(
        instance,
        claim(
            SubjectKeyReference(subject_kind="sec.unknown", key={"slug": "x"}),
            f"{ASSESSMENT}.status",
            "open",
        ),
    )

    assert diagnostic.code == "cruxible.subject.key_invalid"


def test_dispositions_still_name_claims_by_id(instance: PlaybillInstance) -> None:
    """A key reference changes only how the Subject is named; the slot law is unchanged."""

    accept(instance, change_set(create(ASSESSMENT, "r1", "cve-1")))
    subject = by_key(ASSESSMENT, "R1", "CVE-1")
    accept(instance, change_set(claim(subject, f"{ASSESSMENT}.status", "open")))
    (status,) = live(instance, f"{ASSESSMENT}/r1--cve-1", f"{ASSESSMENT}.status")
    status_id = status.rsplit("/", 1)[-1].removesuffix(".json")

    accept(
        instance,
        change_set(
            claim(
                subject,
                f"{ASSESSMENT}.status",
                "closed",
                dispositions=(ClaimDispositionInput(claim_id=status_id, disposition="contradict"),),
            )
        ),
    )

    values = {
        item.statement.object.value  # type: ignore[union-attr]
        for item in live(instance, f"{ASSESSMENT}/r1--cve-1", f"{ASSESSMENT}.status").values()
    }
    assert "closed" in values


def test_a_single_claim_input_names_its_subject_by_key(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.authoring.inputs import lower_authoring_input

    accept(instance, change_set(create(ASSESSMENT, "r1", "cve-1")))

    accept(
        instance,
        lower_authoring_input(
            claim(by_key(ASSESSMENT, "R1", "CVE-1"), f"{ASSESSMENT}.status", "x")
        ),
    )

    assert live(instance, f"{ASSESSMENT}/r1--cve-1", f"{ASSESSMENT}.status")
    missing = compile_(
        instance,
        lower_authoring_input(
            claim(by_key(ASSESSMENT, "r7", "cve-7"), f"{ASSESSMENT}.status", "x")
        ),
    )
    assert missing.verdict == "refused"
    assert missing.frontier.diagnostics[0].code == "cruxible.subject.key_missing"


# Review H F-001: an alias authored through kind/<text> shorthand is overlaid on
# the Subject lowering lands it on, so it cannot redirect a sibling key reference.


def _packages_alpha_and_bar(instance: PlaybillInstance) -> None:
    accept(
        instance,
        change_set(
            SubjectInput(kind="subject", subject_kind=PACKAGE, key="Alpha Key"),
            SubjectInput(kind="subject", subject_kind=PACKAGE, key="Bar Key"),
        ),
    )


def _alias_through_shorthand(text: str, value: str) -> ClaimInput:
    """``semantic.alias`` stated on ``sec.package/<text>``: lowering resolves the text by key."""

    return ClaimInput(
        kind="claim",
        subject=f"{PACKAGE}/{text}",
        predicate="semantic.alias",
        object=LiteralObjectInput(kind="literal", value=value),
        role="normative",
        rationale="Another name for it.",
        source=SelfSourceInput(kind="self_source", body=f"also known as {value}"),
    )


def test_an_alias_on_shorthand_cannot_redirect_a_sibling_key_subject(
    instance: PlaybillInstance,
) -> None:
    _packages_alpha_and_bar(instance)
    base = instance.accepted_coordinate()

    diagnostic = refusal(
        instance,
        _alias_through_shorthand("Alpha KEY", "Bar Key"),
        claim(
            SubjectKeyReference(subject_kind=PACKAGE, key={"slug": "Bar Key"}),
            f"{PACKAGE}.title",
            "B only",
        ),
    )

    assert diagnostic.code == "cruxible.authoring.identity_reference_unstable"
    assert instance.accepted_coordinate() == base


def test_an_alias_on_shorthand_cannot_redirect_a_sibling_key_value(
    instance: PlaybillInstance,
) -> None:
    _packages_alpha_and_bar(instance)
    accept(instance, change_set(create(ASSESSMENT, "r1", "cve-1")))

    diagnostic = refusal(
        instance,
        _alias_through_shorthand("alpha key", "Bar Key"),
        claim(
            f"{ASSESSMENT}/r1--cve-1",
            f"{ASSESSMENT}.affects",
            SubjectObjectInput(
                kind="subject",
                subject=SubjectKeyReference(subject_kind=PACKAGE, key={"slug": "bar key"}),
            ),
        ),
    )

    assert diagnostic.code == "cruxible.authoring.identity_reference_unstable"
