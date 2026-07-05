"""Tests for provider payload helper objects."""

from __future__ import annotations

from pathlib import Path

import pytest

import cruxible_core.provider.payloads as payloads
from cruxible_core.graph.evidence import merge_evidence_ref_objects
from cruxible_core.provider.payloads import (
    EvidenceRef,
    JsonItems,
    ParsedTabularBundle,
    evidence_ref,
    merge_evidence_refs,
)
from cruxible_core.provider.types import ProviderContext, ResolvedArtifact


def test_parsed_tabular_bundle_accepts_valid_bundle() -> None:
    bundle = ParsedTabularBundle.from_payload(
        {
            "artifact": {"name": "bundle"},
            "tables": {
                "assets": {
                    "columns": ["asset_id"],
                    "rows": [{"asset_id": "A-1"}],
                    "row_count": 1,
                },
                "owners": [{"owner_id": "O-1"}],
            },
            "files": [{"path": "assets.csv"}],
            "diagnostics": [],
        }
    )

    assert bundle.require_table("assets") == [{"asset_id": "A-1"}]
    assert bundle.require_table("owners") == [{"owner_id": "O-1"}]
    assert bundle.optional_table("missing") == []
    assert bundle.table_names() == ["assets", "owners"]
    assert bundle.to_payload()["tables"]["assets"]["row_count"] == 1


def test_parsed_tabular_bundle_rejects_missing_tables() -> None:
    with pytest.raises(ValueError, match="input.tables"):
        ParsedTabularBundle.from_payload({"artifact": {}})


def test_parsed_tabular_bundle_rejects_non_list_table_rows() -> None:
    with pytest.raises(ValueError, match="parsed table 'assets' to contain rows"):
        ParsedTabularBundle.from_payload(
            {"artifact": {}, "tables": {"assets": {"rows": {"asset_id": "A-1"}}}}
        )


def test_parsed_tabular_bundle_rejects_non_dict_rows() -> None:
    with pytest.raises(ValueError, match="entry 0 to be an object"):
        ParsedTabularBundle.from_payload({"artifact": {}, "tables": {"assets": {"rows": ["A-1"]}}})


def test_parsed_tabular_bundle_require_table_rejects_missing_table() -> None:
    bundle = ParsedTabularBundle.from_payload({"artifact": {}, "tables": {}})

    with pytest.raises(ValueError, match="Expected parsed table 'assets'"):
        bundle.require_table("assets")


def test_json_items_accepts_default_items_payload() -> None:
    payload = JsonItems.from_payload({"items": [{"id": "A"}]})

    assert payload.items == [{"id": "A"}]
    assert payload.to_payload() == {"items": [{"id": "A"}]}


def test_json_items_accepts_custom_key() -> None:
    payload = JsonItems.from_payload({"rows": [{"id": "A"}]}, key="rows")

    assert payload.to_payload(key="rows") == {"rows": [{"id": "A"}]}


def test_json_items_rejects_missing_items() -> None:
    with pytest.raises(ValueError, match="'items' to be a list of objects"):
        JsonItems.from_payload({})


def test_json_items_rejects_non_list_items() -> None:
    with pytest.raises(ValueError, match="'items' to be a list of objects"):
        JsonItems.from_payload({"items": {"id": "A"}})


def test_json_items_rejects_non_dict_entries() -> None:
    with pytest.raises(ValueError, match="entry 0 to be an object"):
        JsonItems.from_payload({"items": ["A"]})


def test_merge_evidence_refs_preserves_order_and_dedupes() -> None:
    first = evidence_ref("inventory", "row-1", observed_at="2026-05-24")
    duplicate = evidence_ref("inventory", "row-1", observed_at="2026-05-25")
    second = evidence_ref("scanner", "finding-2")

    assert merge_evidence_refs([first], [duplicate, second]) == [first, second]


def test_merge_evidence_ref_objects_matches_compact_payload_merge() -> None:
    first = evidence_ref("inventory", "row-1", observed_at="2026-05-24")
    duplicate = evidence_ref("inventory", "row-1", observed_at="2026-05-25")
    second = EvidenceRef(source="scanner", source_record_id="finding-2")

    refs = merge_evidence_ref_objects([first], [duplicate, second])

    assert refs == [EvidenceRef.model_validate(first), second]
    assert [ref.to_payload() for ref in refs] == merge_evidence_refs(
        [first],
        [duplicate, second],
    )


def test_evidence_ref_collects_extra_fields_into_metadata() -> None:
    ref = evidence_ref(
        "inventory",
        "row-1",
        observed_at="2026-05-24",
        criteria="product_name",
    )

    assert ref == {
        "source": "inventory",
        "source_record_id": "row-1",
        "metadata": {
            "observed_at": "2026-05-24",
            "criteria": "product_name",
        },
    }
    assert EvidenceRef.model_validate(ref).metadata["criteria"] == "product_name"


def test_evidence_ref_model_dump_uses_compact_payload() -> None:
    ref = EvidenceRef(
        source="inventory",
        source_record_id="row-1",
        criteria="product_name",
    )

    assert ref.model_dump(mode="json") == {
        "source": "inventory",
        "source_record_id": "row-1",
        "metadata": {"criteria": "product_name"},
    }


def test_evidence_ref_rejects_empty_identity() -> None:
    with pytest.raises(ValueError, match="source and source_record_id"):
        evidence_ref("", "row-1")


def test_source_artifact_evidence_ref_blessed_locator_shape() -> None:
    ref = payloads.source_artifact_evidence_ref(
        "opinion_text_op_loper_bright",
        "mdchunk_abc123",
        quote="Chevron is overruled.",
        char_start=100,
        char_end=121,
        content_hash="sha256:deadbeef",
        label="Loper Bright opinion text",
        opinion_id="op_loper_bright",
    )

    assert ref == {
        "source": "source_artifact",
        "source_record_id": "mdchunk_abc123",
        "artifact_id": "opinion_text_op_loper_bright",
        "label": "Loper Bright opinion text",
        "metadata": {
            "quote": "Chevron is overruled.",
            "char_start": 100,
            "char_end": 121,
            "expected_content_hash": "sha256:deadbeef",
            "opinion_id": "op_loper_bright",
        },
    }
    assert EvidenceRef.model_validate(ref).artifact_id == "opinion_text_op_loper_bright"


def test_source_artifact_evidence_ref_omits_absent_locator_keys() -> None:
    ref = payloads.source_artifact_evidence_ref("artifact_x", "chunk_y")
    assert ref == {
        "source": "source_artifact",
        "source_record_id": "chunk_y",
        "artifact_id": "artifact_x",
    }


def test_load_artifact_json_prefers_context_artifact(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifact"
    fallback_dir = tmp_path / "fallback"
    artifact_dir.mkdir()
    fallback_dir.mkdir()
    (artifact_dir / "data.json").write_text('{"from": "artifact"}')
    (fallback_dir / "data.json").write_text('{"from": "fallback"}')
    context = ProviderContext(
        workflow_name="wf",
        step_id="step",
        provider_name="provider",
        provider_version="1.0.0",
        artifact=ResolvedArtifact(
            name="bundle",
            kind="directory",
            uri="./data",
            local_path=str(artifact_dir),
        ),
    )

    assert payloads.load_artifact_json(context, "data.json") == {"from": "artifact"}
    assert payloads.load_artifact_json(None, "data.json", fallback_dir=fallback_dir) == {
        "from": "fallback"
    }


def test_load_artifact_json_errors_are_specific(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no artifact directory available"):
        payloads.load_artifact_json(None, "data.json")
    with pytest.raises(ValueError, match="not found"):
        payloads.load_artifact_json(None, "missing.json", fallback_dir=tmp_path)
    (tmp_path / "bad.json").write_text("[1, 2]")
    with pytest.raises(ValueError, match="JSON object"):
        payloads.load_artifact_json(None, "bad.json", fallback_dir=tmp_path)


def test_verdict_vocabulary_constants() -> None:
    assert payloads.VERDICTS == {"support", "unsure", "contradict"}
