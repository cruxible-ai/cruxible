# ruff: noqa: F811 - shared instance fixture
"""Bare references normalize before identity/digest creation, using existing key laws."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.authoring.inputs import SubjectInput, SubjectObjectInput
from cruxible_client.contracts.canonical import canonical_bytes
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
def test_bare_and_mapping_references_store_identical_payloads_and_digests(
    instance: PlaybillInstance, object_reference: bool
) -> None:
    def payload(key: Any) -> Any:
        reference = SubjectKeyReference(subject_kind=PACKAGE, key=key)
        fact = (
            claim(
                by_key(ASSESSMENT, "r1", "cve-1"),
                f"{ASSESSMENT}.affects",
                SubjectObjectInput(kind="subject", subject=reference),
            )
            if object_reference
            else claim(reference, f"{PACKAGE}.title", "Demo")
        )
        # Keep the evidence identical; only the reference spelling differs.
        fact = fact.model_copy(update={"source": claim("x/y", "x.y", "fact").source})
        return change_set(
            SubjectInput(kind="subject", subject_kind=PACKAGE, key="  Ｄemo  Key "),
            create(ASSESSMENT, "r1", "cve-1"),
            fact,
        )

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    intents = [
        coordinator.create(
            actor=OWNER,
            payload=payload(key),
            canonical_timestamp="2026-10-16T12:00:00.000000Z",
        ).intent
        for key in ("  Ｄemo  Key ", {"slug": "  Ｄemo  Key "})
    ]
    bare, mapping = intents
    assert canonical_bytes(bare.payload.model_dump(mode="json")) == canonical_bytes(
        mapping.payload.model_dump(mode="json")
    )
    assert bare.payload_digest == mapping.payload_digest
    assert bare.create_fingerprint == mapping.create_fingerprint
    assert bare.intent_id == mapping.intent_id  # one idempotent governed operation
    computed, _ = coordinator._compute_and_bind_preflight(bare.intent_id, actor=OWNER)
    assert computed.result.verdict == "passed", computed.result.frontier
    assert computed.lowered is not None
    # The resolved key went through NFKC, trim, collapse-whitespace and casefold.
    assert any(
        path == f"subjects/{PACKAGE}/demo-key.json" for path, _ in computed.lowered.changed_members
    )


@pytest.mark.parametrize("object_reference", [False, True])
def test_bare_composite_reference_refuses_with_mapping_repair(
    instance: PlaybillInstance, object_reference: bool
) -> None:
    reference = SubjectKeyReference(subject_kind=ASSESSMENT, key="r1")
    fact = (
        claim(
            by_key(ASSESSMENT, "r2", "cve-2"),
            f"{ASSESSMENT}.supersedes",
            SubjectObjectInput(kind="subject", subject=reference),
        )
        if object_reference
        else claim(reference, f"{ASSESSMENT}.status", "open")
    )
    diagnostic = refusal(instance, create(ASSESSMENT, "r2", "cve-2"), fact)
    assert diagnostic.code == "cruxible.subject.key_invalid"
    assert "mapping" in diagnostic.repairs[0].description
    assert diagnostic.repairs[0].replacement["key"] == {
        f"{ASSESSMENT}.release": "<value>",
        f"{ASSESSMENT}.cve": "<value>",
    }


@pytest.mark.parametrize("key", ["Demo", 0, False])
def test_reference_contract_keeps_scalar_types(key: Any) -> None:
    reference = SubjectKeyReference(subject_kind=PACKAGE, key=key)
    assert type(reference.key) is type(key)
    assert reference.key == key


@pytest.mark.parametrize("key", [None, [], 1.5, {"slug": 1.5}])
def test_reference_contract_rejects_non_key_values(key: Any) -> None:
    with pytest.raises(ValidationError):
        SubjectKeyReference(subject_kind=PACKAGE, key=key)


def test_procedure_bare_and_mapping_keys_resolve_identically(instance: PlaybillInstance) -> None:
    from cruxible_core.indexes.subject_identity import IdentityView, tree_key_reader
    from cruxible_core.procedures.proposal_delivery import _SubjectKeys

    accept(instance, change_set(SubjectInput(kind="subject", subject_kind=PACKAGE, key="Demo Key")))
    base = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(base.git_oid)
    keys = _SubjectKeys(instance, base=base, base_tree=tree)
    with instance.bind_accepted_projection(base) as projection:
        keys.bind(IdentityView(projection.typed.connection, key_reader=tree_key_reader(tree)))
        bare = keys.address(SubjectKeyReference(subject_kind=PACKAGE, key="  ＤEMO KEY "))
        mapping = keys.address(
            SubjectKeyReference(subject_kind=PACKAGE, key={"slug": "  ＤEMO KEY "})
        )
    assert bare == mapping
    assert bare.artifact_path == f"subjects/{PACKAGE}/demo-key.json"


def test_procedure_bare_composite_key_has_typed_mapping_repair(instance: PlaybillInstance) -> None:
    from cruxible_core.procedures.proposal_delivery import ProposalDeliveryRefused, _SubjectKeys

    base = instance.accepted_coordinate()
    keys = _SubjectKeys(instance, base=base, base_tree=instance.immutable_tree_at(base.git_oid))
    with pytest.raises(ProposalDeliveryRefused) as caught:
        keys.address(SubjectKeyReference(subject_kind=ASSESSMENT, key="r1"))
    assert caught.value.details["code"] == "cruxible.subject.key_invalid"
    assert f"{ASSESSMENT}.release" in str(caught.value)
    assert f"{ASSESSMENT}.cve" in str(caught.value)


def test_bare_key_refuses_multiple_hint_holders(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.authoring.inputs import SubjectKindInput

    accept(
        instance,
        change_set(
            SubjectKindInput(
                kind="subject_kind", name=PACKAGE, keys=(f"{PACKAGE}.slug",), uniqueness="hint"
            )
        ),
    )
    accept(instance, change_set(SubjectInput(kind="subject", subject_kind=PACKAGE, key="Demo Key")))
    accept(
        instance,
        change_set(
            SubjectInput(kind="subject", subject_kind=PACKAGE, key="Demo Key", create_new=True)
        ),
    )
    diagnostic = refusal(
        instance,
        claim(
            SubjectKeyReference(subject_kind=PACKAGE, key="DEMO KEY"), f"{PACKAGE}.title", "Which?"
        ),
    )
    assert diagnostic.code == "cruxible.subject.key_ambiguous"
    assert len(diagnostic.repairs[0].replacement["candidates"]) == 2
