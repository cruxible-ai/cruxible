"""Workflow apply coverage for config-declared entity identity keys."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.workflow_helpers import write_lock_for_instance

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DataValidationError
from cruxible_core.service import EntityWriteInput, service_add_entity_inputs
from cruxible_core.workflow.executor import execute_workflow

IDENTITY_WORKFLOW_CONFIG = """\
version: '1.0'
name: workflow_entity_identity
entity_types:
  Account:
    unique_by: [name, family]
    id_pattern: '^account_[a-z0-9_]+$'
    properties:
      account_id: {type: string, primary_key: true}
      name: {type: string}
      family: {type: string}
relationships: []
contracts:
  AccountInput:
    fields:
      account_id: {type: string}
      name: {type: string}
      family: {type: string}
workflows:
  apply_account:
    type: canonical
    contract_in: AccountInput
    steps:
      - id: accounts
        make_entities:
          entity_type: Account
          items:
            - account_id: $input.account_id
              name: $input.name
              family: $input.family
          entity_id: $item.account_id
          properties:
            name: $item.name
            family: $item.family
        as: accounts
      - id: apply_accounts
        apply_entities:
          entities_from: accounts
        as: apply_accounts
    returns: apply_accounts
"""


def _identity_workflow_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(IDENTITY_WORKFLOW_CONFIG)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    write_lock_for_instance(instance)
    return instance


def test_workflow_apply_enforces_unique_by(tmp_path: Path) -> None:
    instance = _identity_workflow_instance(tmp_path)
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="Account",
                entity_id="account_bluest",
                properties={"name": "Bluest Account", "family": "Checking"},
            )
        ],
    )

    with pytest.raises(DataValidationError, match=r"violates unique_by \[name, family\]"):
        execute_workflow(
            instance,
            instance.load_config(),
            "apply_account",
            {
                "account_id": "account_duplicate",
                "name": "BLUEST, ACCOUNT!",
                "family": "checking",
            },
            mode="apply",
        )

    assert instance.load_graph().get_entity("Account", "account_duplicate") is None


def test_workflow_apply_enforces_id_pattern(tmp_path: Path) -> None:
    instance = _identity_workflow_instance(tmp_path)

    with pytest.raises(DataValidationError, match="does not match id_pattern"):
        execute_workflow(
            instance,
            instance.load_config(),
            "apply_account",
            {
                "account_id": "INVALID-ID",
                "name": "Pattern failure",
                "family": "Checking",
            },
            mode="apply",
        )

    assert instance.load_graph().get_entity("Account", "INVALID-ID") is None
