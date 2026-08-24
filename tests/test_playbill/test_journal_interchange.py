"""Normalized segment export, import, mirroring, and verified handoff laws."""

from __future__ import annotations

from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from cruxible_client.contracts.errors import PlaybillJournalError
from cruxible_core.playbill.exhaust import (
    JournalExportBundleV1,
    JournalHeadVectorV1,
    build_journal_export,
    build_journal_head_manifest,
    import_journal_export,
    parse_journal_export,
    render_journal_export,
    verified_journal_handoff,
)
from tests.test_playbill.test_journal_backends import (
    NOW,
    _activate,
    _append,
    _backend,
    _draft,
    _HeadSigner,
    _stream,
)


def _fixture(tmp_path, name: str = "source"):
    backend = _backend(tmp_path, name)
    _activate(backend)
    for index in range(70):
        _append(
            backend,
            f"record-{index:03d}",
            recorded_at=NOW + timedelta(seconds=index),
        )
    journal_range = backend.range_from_sequences(
        _stream(), "runs-2026-08", first_sequence=1, last_sequence=70
    )
    vector = JournalHeadVectorV1(partitions=(backend.read_head(_stream(), "runs-2026-08"),))
    signer = _HeadSigner(Ed25519PrivateKey.generate())
    manifest = build_journal_head_manifest(vector, asserted_at=NOW, signer=signer)
    return (
        backend,
        journal_range,
        manifest,
        signer.private_key.public_key().public_bytes_raw().hex(),
    )


def test_export_import_export_is_byte_identical_across_local_stores(tmp_path) -> None:
    source, journal_range, manifest, public_key = _fixture(tmp_path)
    first = build_journal_export(source, ranges=(journal_range,), head_manifest=manifest)
    wire = render_journal_export(first)
    assert parse_journal_export(wire) == first
    assert len(first.manifest.segments) == 2  # absolute sequence bucket boundary at 64

    target = _backend(tmp_path, "target")
    imported = import_journal_export(target, first, expected_head_public_key=public_key)
    assert imported == (source.read_head(_stream(), "runs-2026-08"),)
    second = build_journal_export(target, ranges=(journal_range,), head_manifest=manifest)
    assert render_journal_export(second) == wire
    assert (
        import_journal_export(target, first, expected_head_public_key=public_key) == imported
    )  # idempotent mirror retry


def test_export_refuses_missing_reordered_duplicate_or_tampered_segments(tmp_path) -> None:
    source, journal_range, manifest, _ = _fixture(tmp_path)
    bundle = build_journal_export(source, ranges=(journal_range,), head_manifest=manifest)
    payload = bundle.model_dump(mode="json")

    missing = {**payload, "contents": payload["contents"][:-1]}
    with pytest.raises(ValidationError, match="descriptor order"):
        JournalExportBundleV1.model_validate(missing)

    reordered = {
        **payload,
        "contents": list(reversed(payload["contents"])),
    }
    with pytest.raises(ValidationError, match="descriptor order"):
        JournalExportBundleV1.model_validate(reordered)

    duplicated = {
        **payload,
        "manifest": {
            **payload["manifest"],
            "segments": (
                payload["manifest"]["segments"][0],
                payload["manifest"]["segments"][0],
            ),
        },
    }
    with pytest.raises(ValidationError, match="unique|omit|overlap"):
        JournalExportBundleV1.model_validate(duplicated)

    tampered = bundle.model_dump(mode="json")
    tampered["contents"][0]["content_hex"] = "00" + tampered["contents"][0]["content_hex"][2:]
    with pytest.raises(ValidationError, match="does not reproduce"):
        JournalExportBundleV1.model_validate(tampered)


def test_import_refuses_a_fork_instead_of_merging(tmp_path) -> None:
    source, journal_range, manifest, public_key = _fixture(tmp_path)
    bundle = build_journal_export(source, ranges=(journal_range,), head_manifest=manifest)

    target = _backend(tmp_path, "fork")
    _activate(target, token="fork-writer")
    target.append(
        _draft("different-first"),
        expected_head=target.read_head(_stream(), "runs-2026-08"),
        fencing_token="fork-writer",
    )
    with pytest.raises(PlaybillJournalError, match="fork merge"):
        import_journal_export(target, bundle, expected_head_public_key=public_key)


def test_verified_handoff_fences_old_writer_before_new_append(tmp_path) -> None:
    source, journal_range, manifest, public_key = _fixture(tmp_path)
    target = _backend(tmp_path, "target")
    imported = verified_journal_handoff(
        source,
        target,
        ranges=(journal_range,),
        head_manifest=manifest,
        source_fencing_token="writer-a",
        target_fencing_token="writer-b",
        expected_head_public_key=public_key,
    )
    expected = imported[0]

    with pytest.raises(PlaybillJournalError, match="active fencing token"):
        source.append(
            _draft("after-handoff"),
            expected_head=expected,
            fencing_token="writer-a",
        )
    appended = target.append(
        _draft("after-handoff"),
        expected_head=expected,
        fencing_token="writer-b",
    )
    assert appended.record.sequence == 71
