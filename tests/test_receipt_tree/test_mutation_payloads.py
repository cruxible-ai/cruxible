"""Unit tests for mutation payload retention helpers."""

from __future__ import annotations

import hashlib

import pytest

from cruxible_core.primitives import canonical_json
from cruxible_core.receipt_tree.payloads import (
    MutationPayloadMetadata,
    compute_payload_digest,
    retain_mutation_payload,
)


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
