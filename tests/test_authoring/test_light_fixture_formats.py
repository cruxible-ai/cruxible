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
