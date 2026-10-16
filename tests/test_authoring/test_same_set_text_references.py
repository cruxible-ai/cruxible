# ruff: noqa: F811 - shared instance fixture
"""Text references use the same resolver over accepted and same-set Subjects."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import (
    SubjectInput,
    SubjectKindInput,
    SubjectObjectInput,
)
from cruxible_client.contracts.subject_kinds import SubjectKeyReference
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._identity_support import OWNER
from tests.test_service.test_authoring_key_references import (  # noqa: F401
    ASSESSMENT,
    PACKAGE,
    accept,
    by_key,
    change_set,
    claim,
    create,
    instance,
    live,
    refusal,
)


@pytest.mark.parametrize("object_reference", [False, True])
def test_same_set_text_and_structured_references_land_identically(
    instance: PlaybillInstance, object_reference: bool
) -> None:
    def payload(reference: Any) -> Any:
        fact = (
            claim(
                by_key(ASSESSMENT, "r1", "cve-1"),
                f"{ASSESSMENT}.affects",
                SubjectObjectInput(kind="subject", subject=reference),
            )
            if object_reference
            else claim(reference, f"{PACKAGE}.title", "Demo")
        )
        fact = fact.model_copy(update={"source": claim("x/y", "x.y", "fact").source})
        return change_set(
            SubjectInput(kind="subject", subject_kind=PACKAGE, key="Demo Key"),
            create(ASSESSMENT, "r1", "cve-1"),
            fact,
        )

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    results = []
    for reference in (
        f"{PACKAGE}/  ＤEMO  KEY  ",
        SubjectKeyReference(subject_kind=PACKAGE, key={"slug": "  ＤEMO  KEY  "}),
    ):
        _, computed = coordinator.preview(
            actor=OWNER,
            payload=payload(reference),
            canonical_timestamp="2026-10-16T12:00:00.000000Z",
        )
        assert computed.result.verdict == "passed", computed.result.frontier
        assert computed.lowered is not None
        from cruxible_client.contracts.claims import claim_statement_digest, parse_claim

        # Claim ids and creation-operation bindings are independently minted;
        # the governed statements and their digests must be identical.

        results.append(
            sorted(
                claim_statement_digest(parse_claim(content, path=path).statement).tagged
                for path, content in computed.lowered.changed_members
                if path.startswith("claims/")
            )
        )
    assert results[0] == results[1]


def test_unknown_same_set_text_keeps_the_current_object_repair(instance: PlaybillInstance) -> None:
    diagnostic = refusal(
        instance,
        SubjectInput(kind="subject", subject_kind=PACKAGE, key="Demo"),
        create(ASSESSMENT, "r1", "cve-1"),
        claim(
            by_key(ASSESSMENT, "r1", "cve-1"),
            f"{ASSESSMENT}.affects",
            SubjectObjectInput(kind="subject", subject=f"{PACKAGE}/Unknown"),
        ),
    )
    assert diagnostic.code == "cruxible.authoring.object_subject_not_found"
    assert "same change set" in diagnostic.repairs[0].description


def test_same_set_hint_holders_are_ambiguous(instance: PlaybillInstance) -> None:
    # A canonical text distinct from either id must see both hint holders.
    accept(
        instance,
        change_set(
            SubjectKindInput(
                kind="subject_kind", name=PACKAGE, keys=(f"{PACKAGE}.slug",), uniqueness="hint"
            )
        ),
    )
    diagnostic = refusal(
        instance,
        SubjectInput(kind="subject", subject_kind=PACKAGE, key="Demo Key"),
        SubjectInput(kind="subject", subject_kind=PACKAGE, key="Demo Key", create_new=True),
        claim(f"{PACKAGE}/DEMO KEY", f"{PACKAGE}.title", "Which?"),
    )
    assert diagnostic.code == "cruxible.subject.key_ambiguous"
    assert len(diagnostic.repairs[0].replacement["candidates"]) == 2


def test_same_set_text_reads_new_vocabulary(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.authoring.inputs import ClaimTypeInput
    from tests.test_service.test_authoring_key_references import _key
    from tests.test_service.test_subject_identity import _field

    kind = "sec.new_package"
    accept(
        instance,
        change_set(
            ClaimTypeInput(kind="claim_type", claim_type=_key(kind, "slug")),
            ClaimTypeInput(kind="claim_type", claim_type=_field(kind, "title")),
            SubjectKindInput(
                kind="subject_kind", name=kind, keys=(f"{kind}.slug",), uniqueness="unique"
            ),
            SubjectInput(kind="subject", subject_kind=kind, key="Demo Key"),
            claim(f"{kind}/DEMO KEY", f"{kind}.title", "Demo"),
        ),
    )
    assert live(instance, f"{kind}/demo-key", f"{kind}.title")


def test_same_set_text_preserves_accepted_alias_precedence(instance: PlaybillInstance) -> None:
    from tests.test_service.test_authoring_key_references import (
        _alias_through_shorthand,
        _packages_alpha_and_bar,
    )

    _packages_alpha_and_bar(instance)
    accept(instance, change_set(_alias_through_shorthand("Alpha Key", "Bar Key")))
    accept(
        instance,
        change_set(
            SubjectInput(kind="subject", subject_kind=PACKAGE, key="Other Key"),
            claim(f"{PACKAGE}/Bar Key", f"{PACKAGE}.title", "Alpha"),
        ),
    )
    assert live(instance, f"{PACKAGE}/alpha-key", f"{PACKAGE}.title")
    assert not live(instance, f"{PACKAGE}/bar-key", f"{PACKAGE}.title")
