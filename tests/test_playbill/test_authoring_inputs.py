"""PC-G3b decision-only input lowering and ClaimType lint laws."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.inputs import (
    AuthoringInputError,
    ClaimInput,
    LiteralObjectInput,
    ProcedureInput,
    SelfSourceInput,
    WorkingSelectionInput,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.captures import foreign_source_capture_contract
from cruxible_core.playbill.claim_type_inputs import (
    claim_type_input_example,
    lint_claim_type_input,
)
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
)
from tests.test_playbill.test_authoring_procedures import AUTHORITY, _slot_definition


def _coordinator(instance) -> AuthoringIntentCoordinator:  # type: ignore[no-untyped-def]
    return AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: "9" * 32,
        ),
        claim_id_factory=lambda: "CLM-" + "8" * 32,
    )


def _claim_input(*, working: bool = False) -> ClaimInput:
    return ClaimInput(
        kind="claim",
        subject="project.work_item/wi-42",
        predicate="project.work_item.status",
        object=LiteralObjectInput(kind="literal", value="ready"),
        role="observation",
        rationale="The accepted work source reports ready.",
        source=(
            WorkingSelectionInput(kind="working_selection", source_id="repo.work-items")
            if working
            else SelfSourceInput(kind="self_source", body="status: ready\n")
        ),
        citation_role="evidence" if working else None,
    )


def test_input_create_binds_friendly_subject_to_the_stored_intent_base(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    view = coordinator.create_input(
        actor=actor,
        input=_claim_input(),
        canonical_timestamp=TIMESTAMP,
    )

    assert view.intent.base_coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert view.intent.payload.statement.subject.artifact_path == (
        "subjects/project.work_item/wi-42.yaml"
    )
    assert view.intent.payload.tag == "playbill-claim-authoring-payload-v1"
    assert "digest" not in _claim_input().model_dump_json()


def test_create_and_compile_refuse_working_selection_with_bind_repair(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)

    with pytest.raises(AuthoringInputError) as raised:
        coordinator.create_input(
            actor=AuthenticatedActor(actor_id="owner"),
            input=_claim_input(working=True),
            canonical_timestamp=TIMESTAMP,
        )

    assert raised.value.field_path == "input.source"
    assert "authoring bind" in raised.value.repair


def test_tagless_procedure_input_injects_slot_tags_and_compiles(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinator = _coordinator(instance)
    definition = _slot_definition().model_dump(mode="json", by_alias=True)

    def remove_tags(value: object) -> object:
        if isinstance(value, dict):
            return {key: remove_tags(item) for key, item in value.items() if key != "tag"}
        if isinstance(value, list):
            return [remove_tags(item) for item in value]
        return value

    tagless = remove_tags(definition)
    assert isinstance(tagless, dict)
    for key in ("contract_in", "contract_out"):
        tagless[key] = {
            "kind": "slot",
            "slot_name": definition[key]["slot_name"],  # type: ignore[index]
        }
    result = coordinator.compile_input(
        actor=AuthenticatedActor(actor_id="owner"),
        input=ProcedureInput(
            kind="procedure",
            definition=tagless,
            authority=AUTHORITY,
            activation_policy="drain",
        ),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "passed"


def test_claim_type_example_is_tagless_and_anticipated_sources_are_lint_only(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    example = claim_type_input_example().model_copy(
        update={"anticipated_source_ids": ("repo.work-items",)}
    )

    lint = lint_claim_type_input(
        instance,
        example,
        coordinate=instance.accepted_coordinate(),
    )

    encoded = example.model_dump_json()
    assert '"tag"' not in encoded
    assert "identity" not in json.loads(encoded)
    assert [item.code for item in lint.warnings] == [
        "playbill.claim_type.anticipated_source_contract_omitted"
    ]
    warning = lint.warnings[0]
    expected = foreign_source_capture_contract("repo.work-items")
    assert warning.contract_identity == expected.identity.qualified
    assert warning.contract_digest is not None
    assert warning.contract_digest in warning.replacement_rule_fragment[
        "capture_contract_digests"
    ]
