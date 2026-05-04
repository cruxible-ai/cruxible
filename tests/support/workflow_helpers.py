"""Helpers for workflow compiler and executor tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
from textwrap import dedent, indent

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.workflow import build_lock, get_lock_path, write_lock


def write_lock_for_instance(instance: CruxibleInstance) -> None:
    config = instance.load_config()
    write_lock(build_lock(config, instance.get_config_path().parent), get_lock_path(instance))


def json_contract_workflow_yaml(
    *,
    workflow_payload_field: str,
    provider_payload_field: str,
    provider_items_field: str,
) -> str:
    return f"""\
version: "1.0"
name: json_contract_workflow
enums:
  verdict:
    values: [support, reject]
entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true
relationships: []
contracts:
  WorkflowInput:
    fields:
{indent(dedent(workflow_payload_field).strip(), "      ")}
  ProviderInput:
    fields:
{indent(dedent(provider_payload_field).strip(), "      ")}
  ProviderOutput:
    fields:
{indent(dedent(provider_items_field).strip(), "      ")}
providers:
  echo_json:
    kind: function
    contract_in: ProviderInput
    contract_out: ProviderOutput
    ref: tests.support.workflow_test_providers.echo_json_payload
    version: "1.0.0"
    deterministic: true
    runtime: python
workflows:
  validate_json_payload:
    contract_in: WorkflowInput
    steps:
      - id: echo
        provider: echo_json
        input:
          payload: $input.payload
        as: echo
    returns: echo
"""


def json_contract_instance(tmp_path: Path, config_yaml: str) -> CruxibleInstance:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_yaml)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    write_lock_for_instance(instance)
    return instance


def dataflow_instance(
    tmp_path: Path,
    *,
    steps_yaml: str,
    returns: str,
    contract_fields_yaml: str = "fields: {}",
) -> CruxibleInstance:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""\
version: "1.0"
name: dataflow_workflow
kind: world_model

entity_types:
  Row:
    properties:
      id:
        type: string
        primary_key: true

relationships: []

contracts:
  DataflowInput:
{indent(dedent(contract_fields_yaml).strip(), "    ")}

workflows:
  dataflow:
    contract_in: DataflowInput
    steps:
{indent(dedent(steps_yaml).strip(), "      ")}
    returns: {returns}
"""
    )
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    write_lock_for_instance(instance)
    return instance


def compute_directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(child.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(child.read_bytes()).hexdigest().encode())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"
