"""Current shared fixtures author the formats admitted by their compiler."""

import pytest

from cruxible_client.contracts.captures import (
    capture_contract_path,
    parse_capture_contract,
    render_capture_contract,
)
from tests.core_support._identity_support import accept_tree
from tests.core_support._light_capture_support import (
    http_contract,
    procedure_contract,
    record_contract,
)
from tests.core_support._support import initialize_local


@pytest.mark.parametrize("factory", [record_contract, http_contract, procedure_contract])
def test_current_capture_fixtures_are_named_and_admitted(tmp_path, factory):
    instance, _ = initialize_local(tmp_path)
    contract = factory()
    body = render_capture_contract(contract)
    path = capture_contract_path(contract.identity.name)
    assert contract.artifact_format == "cruxible-capture-contract-v3"
    assert parse_capture_contract(body, path=path) == contract
    assert "pins" not in contract.model_dump(mode="json")
    accept_tree(instance, {path: body}, name="current-fixture")
    assert instance.blob_at(instance.accepted_coordinate().git_oid, path) == body


def test_identity_fixture_compiles_its_typed_evidence_dependencies(tmp_path):
    from cruxible_client.contracts.authoring.inputs import (
        ChangeSetInput,
        ClaimTypeInput,
        SubjectKindInput,
        lower_authoring_input,
    )
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._identity_support import identity_claim_type

    instance, _ = initialize_local(tmp_path)
    key = identity_claim_type(
        "name",
        predicate="catalog.part.name",
        subject_kinds=("catalog.part",),
        canonical_form=("nfc", "trim"),
        literal_schema={"type": "string"},
    )
    compiled = AuthoringIntentCoordinator.for_instance(instance).compile(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=lower_authoring_input(
            ChangeSetInput(
                kind="change_set",
                members=(
                    ClaimTypeInput(kind="claim_type", claim_type=key),
                    SubjectKindInput(
                        kind="subject_kind", name="catalog.part", keys=(key.predicate,)
                    ),
                ),
            )
        ),
        canonical_timestamp="2026-10-25T12:00:00.000000Z",
    )
    assert compiled.verdict == "passed", [
        d.model_dump(mode="json") for d in compiled.frontier.diagnostics
    ]


def test_write_fixture_self_source_policy_matches_the_contract_it_accepts(tmp_path):
    from tests.core_support._write_support import CLAIM_TYPES, SELF_SOURCE, seed_write_surface

    instance, _ = seed_write_surface(tmp_path)
    path = capture_contract_path(SELF_SOURCE.identity.name)
    accepted = parse_capture_contract(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    status = next(item for item in CLAIM_TYPES if item.predicate.endswith(".status"))
    (rule,) = status.evidence_admission_policy.rules
    assert [item.target for item in rule.capture_contracts] == [accepted.identity]
