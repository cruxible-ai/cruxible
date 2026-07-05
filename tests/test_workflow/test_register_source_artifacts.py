"""Tests for workflow source-artifact registration steps."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent, indent

import pytest
from tests.support.workflow_helpers import write_lock_for_instance

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.service import (
    service_apply_workflow,
    service_register_source_artifact,
    service_run,
)
from cruxible_core.workflow import build_lock, compile_workflow, execute_workflow

_DEFAULT_ROWS_YAML = """- source_artifact_id: opinion_text_op_zeta
  opinion_id: OP-ZETA
  source_url: https://example.invalid/zeta
  plain_text: |
    # Zeta

    Zeta opinion text.
- source_artifact_id: opinion_text_op_alpha
  opinion_id: OP-ALPHA
  source_url: https://example.invalid/alpha
  plain_text: |
    # Alpha

    Alpha opinion text.
"""


def _register_instance(
    tmp_path: Path,
    *,
    workflow_type: str = "canonical",
    rows_yaml: str = _DEFAULT_ROWS_YAML,
) -> CruxibleInstance:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""version: "1.0"
name: register_source_artifacts_workflow

entity_types:
  Row:
    properties:
      id:
        type: string
        primary_key: true

relationships: []

contracts:
  EmptyInput:
    fields: {{}}

workflows:
  pin_sources:
    type: {workflow_type}
    contract_in: EmptyInput
    steps:
      - id: pin_texts
        register_source_artifacts:
          items:
{indent(dedent(rows_yaml).strip(), "            ")}
          artifact_id: $item.source_artifact_id
          content: $item.plain_text
          kind: markdown
          label: $item.opinion_id
          original_uri: $item.source_url
        as: pinned
    returns: pinned
"""
    )
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    write_lock_for_instance(instance)
    return instance


def _list_source_artifacts(instance: CruxibleInstance):
    store = instance.get_source_artifact_store()
    try:
        return store.list_artifacts()
    finally:
        store.close()


def _get_source_artifact(instance: CruxibleInstance, artifact_id: str):
    store = instance.get_source_artifact_store()
    try:
        return store.get_artifact(artifact_id)
    finally:
        store.close()


def _workflow_receipts(instance: CruxibleInstance):
    store = instance.get_receipt_store()
    try:
        summaries = store.list_receipts(
            query_name="pin_sources",
            operation_type="workflow",
        )
        return [
            store.get_receipt(summary["receipt_id"])
            for summary in summaries
            if isinstance(summary.get("receipt_id"), str)
        ]
    finally:
        store.close()


def test_compile_refuses_register_source_artifacts_in_utility_workflow(
    tmp_path: Path,
) -> None:
    instance = _register_instance(tmp_path, workflow_type="utility")
    config = instance.load_config()

    with pytest.raises(
        ConfigError,
        match="must be type: canonical to use register_source_artifacts",
    ):
        compile_workflow(
            config,
            build_lock(config),
            "pin_sources",
            {},
            config_base_path=instance.get_config_path().parent,
        )


def test_register_source_artifacts_service_preview_reports_plan_without_persisting(
    tmp_path: Path,
) -> None:
    instance = _register_instance(tmp_path)

    preview = service_run(instance, "pin_sources", {})

    assert preview.mode == "preview"
    assert preview.workflow_type == "canonical"
    assert preview.apply_digest is not None
    assert preview.output == {
        "registered": 2,
        "noops": 0,
        "artifact_ids": ["opinion_text_op_alpha", "opinion_text_op_zeta"],
    }
    assert preview.apply_previews["pin_texts"] == preview.output
    assert _list_source_artifacts(instance) == []


def test_register_source_artifacts_service_apply_with_preview_digest_registers_artifacts_and_receipts(
    tmp_path: Path,
) -> None:
    instance = _register_instance(tmp_path)
    preview = service_run(instance, "pin_sources", {})
    assert preview.apply_digest is not None

    applied = service_apply_workflow(
        instance,
        "pin_sources",
        {},
        expected_apply_digest=preview.apply_digest,
        expected_head_snapshot_id=preview.head_snapshot_id,
    )

    assert applied.mode == "apply"
    assert applied.workflow_type == "canonical"
    assert applied.committed_snapshot_id is not None
    assert applied.output == {
        "registered": 2,
        "noops": 0,
        "artifact_ids": ["opinion_text_op_alpha", "opinion_text_op_zeta"],
    }
    assert set(applied.output) == {"registered", "noops", "artifact_ids"}
    assert applied.apply_previews["pin_texts"] == applied.output

    artifacts = sorted(
        _list_source_artifacts(instance),
        key=lambda artifact: artifact.source_artifact_id,
    )
    assert [artifact.source_artifact_id for artifact in artifacts] == [
        "opinion_text_op_alpha",
        "opinion_text_op_zeta",
    ]
    for artifact in artifacts:
        assert artifact.source_kind == "markdown"
        assert artifact.local_path is None

    receipts = {
        receipt.receipt_id: receipt
        for receipt in _workflow_receipts(instance)
        if receipt is not None
    }
    assert preview.receipt_id in receipts
    assert applied.receipt_id in receipts
    assert receipts[preview.receipt_id].workflow_mode == "preview"
    assert receipts[applied.receipt_id].workflow_mode == "apply"
    assert receipts[applied.receipt_id].committed is True


def test_register_source_artifacts_service_apply_rejects_stale_preview_after_conflicting_artifact(
    tmp_path: Path,
) -> None:
    instance = _register_instance(tmp_path)
    preview = service_run(instance, "pin_sources", {})
    assert preview.apply_digest is not None

    service_register_source_artifact(
        instance,
        source_content="# Alpha\n\nConflicting text.\n",
        source_artifact_id="opinion_text_op_alpha",
    )

    with pytest.raises(QueryExecutionError, match="already exists with different content digest"):
        service_apply_workflow(
            instance,
            "pin_sources",
            {},
            expected_apply_digest=preview.apply_digest,
            expected_head_snapshot_id=preview.head_snapshot_id,
        )

    artifacts = _list_source_artifacts(instance)
    assert [artifact.source_artifact_id for artifact in artifacts] == ["opinion_text_op_alpha"]
    assert _get_source_artifact(instance, "opinion_text_op_zeta") is None


def test_register_source_artifacts_happy_path_outputs_and_chunks(
    tmp_path: Path,
) -> None:
    instance = _register_instance(tmp_path)

    result = execute_workflow(instance, instance.load_config(), "pin_sources", {}, mode="apply")

    assert result.output == {
        "registered": 2,
        "noops": 0,
        "artifact_ids": ["opinion_text_op_alpha", "opinion_text_op_zeta"],
    }
    assert result.apply_previews["pin_texts"] == result.output
    assert set(result.output) == {"registered", "noops", "artifact_ids"}

    store = instance.get_source_artifact_store()
    try:
        for artifact_id in result.output["artifact_ids"]:
            artifact = store.get_artifact(artifact_id)
            chunks = store.list_chunks(artifact_id)
            assert artifact is not None
            assert artifact.source_kind == "markdown"
            assert artifact.local_path is None
            assert chunks
            assert any(chunk.block_selector == "paragraph:1" for chunk in chunks)
    finally:
        store.close()


def test_register_source_artifacts_rerun_is_idempotent(
    tmp_path: Path,
) -> None:
    instance = _register_instance(tmp_path)

    first = execute_workflow(instance, instance.load_config(), "pin_sources", {}, mode="apply")
    second = execute_workflow(instance, instance.load_config(), "pin_sources", {}, mode="apply")

    assert first.output["registered"] == 2
    assert second.output == {
        "registered": 0,
        "noops": 2,
        "artifact_ids": ["opinion_text_op_alpha", "opinion_text_op_zeta"],
    }

    store = instance.get_source_artifact_store()
    try:
        stored_ids = {
            artifact.source_artifact_id
            for artifact in store.list_artifacts()
            if artifact.source_artifact_id.startswith("opinion_text_op_")
        }
        assert stored_ids == {"opinion_text_op_alpha", "opinion_text_op_zeta"}
    finally:
        store.close()


def test_register_source_artifacts_metadata_drift_noop_preserves_original_metadata(
    tmp_path: Path,
) -> None:
    instance = _register_instance(tmp_path)
    first = execute_workflow(instance, instance.load_config(), "pin_sources", {}, mode="apply")
    assert first.output["registered"] == 2
    before = _get_source_artifact(instance, "opinion_text_op_alpha")
    assert before is not None
    assert before.label == "OP-ALPHA"
    assert before.original_uri == "https://example.invalid/alpha"
    assert before.source_retention == "manifest_only"
    assert before.archived is False

    config = instance.load_config()
    step = config.workflows["pin_sources"].steps[0]
    assert step.register_source_artifacts is not None
    step.register_source_artifacts.label = "Changed Label"
    step.register_source_artifacts.original_uri = "https://example.invalid/changed"
    step.register_source_artifacts.retention = "archive"
    instance.save_config(config)
    write_lock_for_instance(instance)

    second = execute_workflow(instance, instance.load_config(), "pin_sources", {}, mode="apply")

    assert second.output == {
        "registered": 0,
        "noops": 2,
        "artifact_ids": ["opinion_text_op_alpha", "opinion_text_op_zeta"],
    }
    after = _get_source_artifact(instance, "opinion_text_op_alpha")
    assert after is not None
    assert after.label == before.label
    assert after.original_uri == before.original_uri
    assert after.source_retention == before.source_retention
    assert after.archived == before.archived
    assert after.archive_content_hash == before.archive_content_hash
    assert after.created_at == before.created_at


def test_register_source_artifacts_digest_conflict_errors(
    tmp_path: Path,
) -> None:
    instance = _register_instance(tmp_path)
    service_register_source_artifact(
        instance,
        source_content="# Alpha\n\nOriginal text.\n",
        source_artifact_id="opinion_text_op_alpha",
    )

    with pytest.raises(
        QueryExecutionError,
        match="row 1 artifact_id 'opinion_text_op_alpha' already exists with different content digest",
    ):
        execute_workflow(instance, instance.load_config(), "pin_sources", {}, mode="apply")


def test_register_source_artifacts_invalid_id_errors(tmp_path: Path) -> None:
    instance = _register_instance(
        tmp_path,
        rows_yaml="""        - source_artifact_id: bad id
          opinion_id: BAD
          source_url: https://example.invalid/bad
          plain_text: |
            # Bad

            Bad id text.
        """,
    )

    with pytest.raises(QueryExecutionError, match="row 0 invalid artifact_id 'bad id'"):
        execute_workflow(instance, instance.load_config(), "pin_sources", {}, mode="apply")


@pytest.mark.parametrize("plain_text_yaml", ['""', "123"])
def test_register_source_artifacts_empty_or_non_string_content_errors(
    tmp_path: Path,
    plain_text_yaml: str,
) -> None:
    instance = _register_instance(
        tmp_path,
        rows_yaml=f"""        - source_artifact_id: opinion_text_op_bad
          opinion_id: BAD
          source_url: https://example.invalid/bad
          plain_text: {plain_text_yaml}
        """,
    )

    with pytest.raises(
        QueryExecutionError,
        match="row 0 artifact_id 'opinion_text_op_bad' content must resolve to a non-empty string",
    ):
        execute_workflow(instance, instance.load_config(), "pin_sources", {}, mode="apply")
