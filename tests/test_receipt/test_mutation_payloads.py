"""Unit tests for mutation payload retention helpers."""

from __future__ import annotations

import hashlib
import json

import pytest

from cruxible_core.primitives import canonical_json
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.mutation_payloads import (
    MutationPayloadMetadata,
    compute_payload_digest,
    retain_mutation_payload,
)
from cruxible_core.receipt.store import SQLiteReceiptStore


class TestComputePayloadDigest:
    def test_digest_matches_recomputed_canonical_hash(self):
        payload = {"count": 3, "source": "kev"}
        digest, byte_count = compute_payload_digest(payload)
        expected = canonical_json(payload).encode("utf-8")
        assert digest == f"sha256:{hashlib.sha256(expected).hexdigest()}"
        assert byte_count == len(expected)

    def test_digest_is_key_order_independent(self):
        a = {"count": 3, "source": "kev"}
        b = {"source": "kev", "count": 3}
        assert compute_payload_digest(a) == compute_payload_digest(b)

    def test_digest_changes_with_content(self):
        d1, _ = compute_payload_digest({"count": 3})
        d2, _ = compute_payload_digest({"count": 4})
        assert d1 != d2


class TestRetainMutationPayload:
    @pytest.mark.parametrize("mode", ["metadata", "preview", "full"])
    def test_every_mode_stamps_digest_and_byte_count(self, mode: str):
        payload = {"count": 3, "source": "kev"}
        _, metadata = retain_mutation_payload(payload, retention=mode)
        assert isinstance(metadata, MutationPayloadMetadata)
        assert metadata.retention == mode
        expected_digest, expected_bytes = compute_payload_digest(payload)
        assert metadata.payload_digest == expected_digest
        assert metadata.byte_count == expected_bytes
        assert metadata.payload_digest.startswith("sha256:")

    def test_full_mode_returns_body_unchanged(self):
        payload = {"count": 3, "source": "kev"}
        retained, metadata = retain_mutation_payload(payload, retention="full")
        assert retained == payload
        assert metadata.stored_inline is True
        assert metadata.truncated is False

    def test_metadata_mode_sheds_small_body(self):
        # Option "b": metadata means metadata. Even a small payload carries NO
        # body -- only the omitted marker with digest + byte_count.
        payload = {"count": 3, "source": "kev"}
        retained, metadata = retain_mutation_payload(payload, retention="metadata")
        assert retained != payload
        assert "_cruxible_payload_omitted" in retained
        assert retained["_cruxible_payload_omitted"]["payload_digest"] == metadata.payload_digest
        assert metadata.stored_inline is False
        assert metadata.truncated is True
        # No raw body keys leak into the omitted marker.
        assert "source" not in retained
        assert "count" not in retained

    def test_metadata_mode_sheds_large_body(self):
        payload = {"blob": "x" * 10_000}
        retained, metadata = retain_mutation_payload(payload, retention="metadata")
        assert "_cruxible_payload_omitted" in retained
        assert retained["_cruxible_payload_omitted"]["payload_digest"] == metadata.payload_digest
        assert metadata.stored_inline is False
        assert metadata.truncated is True
        assert "blob" not in retained
        # Digest still reflects the full payload, enabling external hash-match.
        expected_digest, _ = compute_payload_digest(payload)
        assert metadata.payload_digest == expected_digest

    def test_preview_mode_sheds_small_body(self):
        # Option "b": preview means preview. Even a small payload is reduced to a
        # bounded structural preview -- never the raw full body.
        payload = {"count": 3, "source": "kev"}
        retained, metadata = retain_mutation_payload(payload, retention="preview")
        assert "_cruxible_payload_preview" in retained
        assert metadata.stored_inline is False
        assert metadata.truncated is True

    def test_preview_mode_sheds_large_body(self):
        payload = {"blob": "x" * 10_000}
        retained, metadata = retain_mutation_payload(payload, retention="preview")
        assert "_cruxible_payload_preview" in retained
        assert metadata.stored_inline is False
        assert metadata.truncated is True
        # The raw 10k blob must not survive verbatim in the preview.
        assert retained.get("blob") != payload["blob"]
        # Digest still reflects the full payload, not the truncated preview.
        expected_digest, _ = compute_payload_digest(payload)
        assert metadata.payload_digest == expected_digest

    def test_unsupported_mode_raises(self):
        with pytest.raises(ValueError, match="Unsupported mutation payload retention"):
            retain_mutation_payload({}, retention="external")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Raw-SQLite persistence regression (the proof the retention bug is fixed):
# a large oversized mutation body must survive in NONE of the three persisted
# copies under metadata/preview, while full keeps it.
# ---------------------------------------------------------------------------


# >32 KB so the historical "keep small payloads inline" shortcut would have kept
# this body fully inline; under option "b" it must be shed regardless of size.
_LARGE_BODY = "x" * 40_000
_LARGE_PAYLOAD = {"blob": _LARGE_BODY}


def _build_and_persist_mutation_receipt(mode: str):
    """Build an add_entity mutation receipt with a large payload, persist it,
    and return ``(store, receipt_id, raw_row)`` where raw_row is the raw SQLite
    ``receipts`` row (parameters column + receipt_json column)."""
    builder = ReceiptBuilder(operation_type="add_entity", parameters=dict(_LARGE_PAYLOAD))
    builder.record_entity_write(
        entity_type="Vehicle", entity_id="V-RAW", is_update=False
    )
    builder.mark_committed()
    builder.apply_mutation_payload_retention(retention=mode)
    receipt = builder.build()

    store = SQLiteReceiptStore(":memory:")
    store.save_receipt(receipt)
    row = store._conn.execute(
        "SELECT parameters, receipt_json FROM receipts WHERE receipt_id = ?",
        (receipt.receipt_id,),
    ).fetchone()
    return store, receipt, row


class TestRawSqlitePayloadRetention:
    def test_metadata_sheds_large_body_from_every_persisted_copy(self):
        store, receipt, row = _build_and_persist_mutation_receipt("metadata")
        try:
            parameters_col = row["parameters"]
            receipt_json = row["receipt_json"]
            parsed = json.loads(receipt_json)

            # The full body must appear in NONE of the three persisted copies.
            # (a) receipts.parameters column
            assert _LARGE_BODY not in parameters_col
            # (b) receipt_json top-level .parameters
            assert _LARGE_BODY not in canonical_json(parsed["parameters"])
            # (c) receipt_json root mutation node detail["parameters"]
            root_detail_params = parsed["nodes"][0]["detail"]["parameters"]
            assert _LARGE_BODY not in canonical_json(root_detail_params)
            # Belt and suspenders: the raw 40k blob is nowhere in the whole row.
            assert _LARGE_BODY not in receipt_json

            # All three reduced copies are the omitted marker and agree.
            params_col_obj = json.loads(parameters_col)
            assert "_cruxible_payload_omitted" in params_col_obj
            assert "_cruxible_payload_omitted" in parsed["parameters"]
            assert "_cruxible_payload_omitted" in root_detail_params
            assert params_col_obj == parsed["parameters"] == root_detail_params

            # payload_digest + byte_count ARE present and computed over the
            # ORIGINAL full payload.
            expected_digest, expected_bytes = compute_payload_digest(_LARGE_PAYLOAD)
            marker = parsed["parameters"]["_cruxible_payload_omitted"]
            assert marker["payload_digest"] == expected_digest
            assert marker["byte_count"] == expected_bytes
            pm = parsed["nodes"][0]["payload_metadata"]
            assert pm["payload_digest"] == expected_digest
            assert pm["byte_count"] == expected_bytes
            assert pm["byte_count"] > 32 * 1024  # genuinely oversized
        finally:
            store.close()

    def test_preview_sheds_large_body_to_bounded_preview(self):
        store, receipt, row = _build_and_persist_mutation_receipt("preview")
        try:
            parameters_col = row["parameters"]
            receipt_json = row["receipt_json"]
            parsed = json.loads(receipt_json)

            # No copy carries the raw full body.
            assert _LARGE_BODY not in parameters_col
            assert _LARGE_BODY not in receipt_json
            root_detail_params = parsed["nodes"][0]["detail"]["parameters"]

            # All three copies are the bounded preview and agree.
            params_col_obj = json.loads(parameters_col)
            assert "_cruxible_payload_preview" in params_col_obj
            assert "_cruxible_payload_preview" in parsed["parameters"]
            assert "_cruxible_payload_preview" in root_detail_params
            assert params_col_obj == parsed["parameters"] == root_detail_params

            # Digest + byte_count computed over the original full payload.
            expected_digest, expected_bytes = compute_payload_digest(_LARGE_PAYLOAD)
            pm = parsed["nodes"][0]["payload_metadata"]
            assert pm["payload_digest"] == expected_digest
            assert pm["byte_count"] == expected_bytes
        finally:
            store.close()

    def test_full_retains_large_body_in_every_copy(self):
        store, receipt, row = _build_and_persist_mutation_receipt("full")
        try:
            parameters_col = row["parameters"]
            receipt_json = row["receipt_json"]
            parsed = json.loads(receipt_json)

            # full keeps the complete body in all three persisted copies.
            assert _LARGE_BODY in parameters_col
            assert json.loads(parameters_col) == _LARGE_PAYLOAD
            assert parsed["parameters"] == _LARGE_PAYLOAD
            assert parsed["nodes"][0]["detail"]["parameters"] == _LARGE_PAYLOAD

            expected_digest, expected_bytes = compute_payload_digest(_LARGE_PAYLOAD)
            pm = parsed["nodes"][0]["payload_metadata"]
            assert pm["payload_digest"] == expected_digest
            assert pm["byte_count"] == expected_bytes
            assert pm["stored_inline"] is True
            assert pm["truncated"] is False
        finally:
            store.close()
