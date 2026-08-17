"""Normalized content-addressed segment interchange for operational journals."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.canonical import CasDigest, canonical_bytes
from cruxible_core.playbill.errors import PlaybillJournalError
from cruxible_core.playbill.exhaust.backends import JournalBackendProtocol, LocalJournalBackend
from cruxible_core.playbill.exhaust.records import (
    JournalHeadManifestV1,
    JournalPartitionHeadV1,
    JournalRangeV1,
    JournalStreamIdentityV1,
    StoredProcedureJournalRecordV1,
    journal_head_key,
    verify_journal_head_manifest,
    verify_journal_range,
)

JOURNAL_SEGMENT_MAX_BYTES_V1 = 1024 * 1024
JOURNAL_SEGMENT_SEQUENCE_BUCKET_V1 = 64
JOURNAL_SEGMENT_BOUNDARY_RULE_V1: Literal["sequence-bucket-64-max-1048576-v1"] = (
    "sequence-bucket-64-max-1048576-v1"
)

_HEX_RE = re.compile(r"^(?:[0-9a-f]{2})*$")


class _StrictInterchangeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _segment_sort_key(
    segment: "JournalSegmentDescriptorV1",
) -> tuple[bytes, bytes, bytes, bytes, int]:
    stream = segment.stream
    return (
        stream.instance_id.encode("utf-8"),
        stream.journal_family.encode("utf-8"),
        stream.stream_id.encode("utf-8"),
        segment.partition_id.encode("utf-8"),
        segment.first_sequence,
    )


def _range_sort_key(
    journal_range: JournalRangeV1,
) -> tuple[bytes, bytes, bytes, bytes, int]:
    stream = journal_range.stream
    return (
        stream.instance_id.encode("utf-8"),
        stream.journal_family.encode("utf-8"),
        stream.stream_id.encode("utf-8"),
        journal_range.partition_id.encode("utf-8"),
        journal_range.first_sequence,
    )


class JournalSegmentDescriptorV1(_StrictInterchangeModel):
    tag: Literal["playbill-journal-segment-descriptor-v1"] = (
        "playbill-journal-segment-descriptor-v1"
    )
    stream: JournalStreamIdentityV1
    partition_id: str
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    record_count: int = Field(ge=1)
    content_digest: str
    byte_length: int = Field(ge=1)
    oversized_single_record: bool = False

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _range(self) -> "JournalSegmentDescriptorV1":
        if self.last_sequence - self.first_sequence + 1 != self.record_count:
            raise ValueError("journal segment record_count does not match its sequence range")
        if self.oversized_single_record:
            if self.record_count != 1 or self.byte_length <= JOURNAL_SEGMENT_MAX_BYTES_V1:
                raise ValueError("oversized journal segment must contain one actually large record")
        elif self.byte_length > JOURNAL_SEGMENT_MAX_BYTES_V1:
            raise ValueError("ordinary journal segment exceeds the frozen byte boundary")
        return self


class JournalSegmentContentV1(_StrictInterchangeModel):
    tag: Literal["playbill-journal-segment-content-v1"] = "playbill-journal-segment-content-v1"
    content_digest: str
    content_hex: str

    @field_validator("content_digest")
    @classmethod
    def _content_digest(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value

    @field_validator("content_hex")
    @classmethod
    def _content_hex(cls, value: str) -> str:
        if not _HEX_RE.fullmatch(value):
            raise ValueError("journal segment content must be lowercase hexadecimal bytes")
        return value

    @model_validator(mode="after")
    def _reproduce(self) -> "JournalSegmentContentV1":
        digest = CasDigest(hashlib.sha256(bytes.fromhex(self.content_hex)).hexdigest()).tagged
        if digest != self.content_digest:
            raise ValueError("journal segment content digest does not reproduce")
        return self


class JournalExportV1(_StrictInterchangeModel):
    """The one normalized manifest shape; record identity is independent of packing."""

    tag: Literal["playbill-journal-export-v1"] = "playbill-journal-export-v1"
    boundary_rule: Literal["sequence-bucket-64-max-1048576-v1"] = JOURNAL_SEGMENT_BOUNDARY_RULE_V1
    head_manifest: JournalHeadManifestV1
    ranges: tuple[JournalRangeV1, ...]
    segments: tuple[JournalSegmentDescriptorV1, ...]

    @field_validator("ranges")
    @classmethod
    def _ranges(cls, value: tuple[JournalRangeV1, ...]) -> tuple[JournalRangeV1, ...]:
        if not value:
            raise ValueError("journal export requires at least one exact range")
        keys = tuple(_range_sort_key(item) for item in value)
        if keys != tuple(sorted(keys)):
            raise ValueError("journal export ranges must be canonically sorted")
        identities = tuple(key[:4] for key in keys)
        if len(identities) != len(set(identities)):
            raise ValueError("journal export permits only one contiguous range per partition")
        return value

    @field_validator("segments")
    @classmethod
    def _segments(
        cls, value: tuple[JournalSegmentDescriptorV1, ...]
    ) -> tuple[JournalSegmentDescriptorV1, ...]:
        if not value:
            raise ValueError("journal export requires content-addressed segments")
        keys = tuple(_segment_sort_key(item) for item in value)
        if keys != tuple(sorted(keys)):
            raise ValueError("journal export segments must be canonically sorted")
        if len({item.content_digest for item in value}) != len(value):
            raise ValueError("journal export segment digests must be unique")
        return value

    @model_validator(mode="after")
    def _coverage(self) -> "JournalExportV1":
        by_partition: dict[tuple[str, str, str, str], list[JournalSegmentDescriptorV1]] = (
            defaultdict(list)
        )
        for segment in self.segments:
            by_partition[
                (
                    segment.stream.instance_id,
                    segment.stream.journal_family,
                    segment.stream.stream_id,
                    segment.partition_id,
                )
            ].append(segment)

        heads = {
            (
                head.stream.instance_id,
                head.stream.journal_family,
                head.stream.stream_id,
                head.partition_id,
            ): head
            for head in self.head_manifest.statement.head_vector.partitions
        }
        range_keys: set[tuple[str, str, str, str]] = set()
        for journal_range in self.ranges:
            key = (
                journal_range.stream.instance_id,
                journal_range.stream.journal_family,
                journal_range.stream.stream_id,
                journal_range.partition_id,
            )
            range_keys.add(key)
            segments = by_partition.get(key, [])
            expected = journal_range.first_sequence
            for segment in segments:
                if segment.first_sequence != expected:
                    raise ValueError("journal export segments omit, overlap, or reorder a range")
                expected = segment.last_sequence + 1
            if expected != journal_range.last_sequence + 1:
                raise ValueError("journal export segments do not cover the exact declared range")
            head = heads.get(key)
            if head is None or (
                head.sequence != journal_range.last_sequence
                or head.record_digest != journal_range.expected_head_digest
            ):
                raise ValueError("signed journal head does not authenticate the exported range")
        if set(by_partition) != range_keys or set(heads) != range_keys:
            raise ValueError("journal export ranges, segments, and signed heads must agree exactly")
        return self


class JournalExportBundleV1(_StrictInterchangeModel):
    tag: Literal["playbill-journal-export-bundle-v1"] = "playbill-journal-export-bundle-v1"
    manifest: JournalExportV1
    contents: tuple[JournalSegmentContentV1, ...]

    @model_validator(mode="after")
    def _contents(self) -> "JournalExportBundleV1":
        expected = tuple(item.content_digest for item in self.manifest.segments)
        actual = tuple(item.content_digest for item in self.contents)
        if actual != expected:
            raise ValueError("journal export contents must match descriptor order exactly")
        return self


def _record_line(record: StoredProcedureJournalRecordV1) -> bytes:
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


def _pack_range(
    journal_range: JournalRangeV1,
    records: tuple[StoredProcedureJournalRecordV1, ...],
) -> tuple[tuple[JournalSegmentDescriptorV1, JournalSegmentContentV1], ...]:
    verify_journal_range(journal_range, records)
    packed: list[tuple[JournalSegmentDescriptorV1, JournalSegmentContentV1]] = []
    current: list[StoredProcedureJournalRecordV1] = []
    current_bytes = bytearray()
    current_bucket: int | None = None

    def flush() -> None:
        nonlocal current, current_bytes, current_bucket
        if not current:
            return
        content = bytes(current_bytes)
        digest = CasDigest(hashlib.sha256(content).hexdigest()).tagged
        descriptor = JournalSegmentDescriptorV1(
            stream=journal_range.stream,
            partition_id=journal_range.partition_id,
            first_sequence=current[0].record.sequence,
            last_sequence=current[-1].record.sequence,
            record_count=len(current),
            content_digest=digest,
            byte_length=len(content),
            oversized_single_record=len(current) == 1
            and len(content) > JOURNAL_SEGMENT_MAX_BYTES_V1,
        )
        packed.append(
            (
                descriptor,
                JournalSegmentContentV1(
                    content_digest=digest,
                    content_hex=content.hex(),
                ),
            )
        )
        current = []
        current_bytes = bytearray()
        current_bucket = None

    for stored in records:
        line = _record_line(stored)
        bucket = (stored.record.sequence - 1) // JOURNAL_SEGMENT_SEQUENCE_BUCKET_V1
        if len(line) > JOURNAL_SEGMENT_MAX_BYTES_V1:
            flush()
            current = [stored]
            current_bytes = bytearray(line)
            current_bucket = bucket
            flush()
            continue
        if current and (
            bucket != current_bucket
            or len(current_bytes) + len(line) > JOURNAL_SEGMENT_MAX_BYTES_V1
        ):
            flush()
        if not current:
            current_bucket = bucket
        current.append(stored)
        current_bytes.extend(line)
    flush()
    return tuple(packed)


def build_journal_export(
    backend: JournalBackendProtocol,
    *,
    ranges: tuple[JournalRangeV1, ...],
    head_manifest: JournalHeadManifestV1,
) -> JournalExportBundleV1:
    ordered_ranges = tuple(sorted(ranges, key=_range_sort_key))
    packed = tuple(
        item
        for journal_range in ordered_ranges
        for item in _pack_range(journal_range, backend.read_exact_range(journal_range))
    )
    manifest = JournalExportV1(
        head_manifest=head_manifest,
        ranges=ordered_ranges,
        segments=tuple(descriptor for descriptor, _ in packed),
    )
    return JournalExportBundleV1(
        manifest=manifest,
        contents=tuple(content for _, content in packed),
    )


def render_journal_export(bundle: JournalExportBundleV1) -> bytes:
    return canonical_bytes(bundle.model_dump(mode="json")) + b"\n"


def parse_journal_export(content: bytes) -> JournalExportBundleV1:
    try:
        bundle = JournalExportBundleV1.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlaybillJournalError("journal export bundle is malformed") from exc
    if render_journal_export(bundle) != content:
        raise PlaybillJournalError("journal export bundle is not canonical")
    return bundle


def _records_from_segment(
    descriptor: JournalSegmentDescriptorV1,
    content: JournalSegmentContentV1,
) -> tuple[StoredProcedureJournalRecordV1, ...]:
    body = bytes.fromhex(content.content_hex)
    if len(body) != descriptor.byte_length:
        raise PlaybillJournalError("journal segment byte length does not reproduce")
    lines = body.splitlines(keepends=True)
    if not lines or any(not line.endswith(b"\n") for line in lines):
        raise PlaybillJournalError("journal segment record framing is invalid")
    records: list[StoredProcedureJournalRecordV1] = []
    for line in lines:
        raw = line[:-1]
        try:
            stored = StoredProcedureJournalRecordV1.model_validate(json.loads(raw))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PlaybillJournalError("journal segment contains a malformed record") from exc
        if canonical_bytes(stored.model_dump(mode="json")) != raw:
            raise PlaybillJournalError("journal segment record is not canonical")
        records.append(stored)
    if len(records) != descriptor.record_count or (
        records[0].record.sequence != descriptor.first_sequence
        or records[-1].record.sequence != descriptor.last_sequence
    ):
        raise PlaybillJournalError("journal segment descriptor does not match its records")
    if any(
        record.record.stream != descriptor.stream
        or record.record.partition_id != descriptor.partition_id
        for record in records
    ):
        raise PlaybillJournalError("journal segment contains substituted stream coordinates")
    return tuple(records)


def import_journal_export(
    backend: JournalBackendProtocol,
    bundle: JournalExportBundleV1,
    *,
    expected_head_public_key: str,
) -> tuple[JournalPartitionHeadV1, ...]:
    verify_journal_head_manifest(
        bundle.manifest.head_manifest,
        expected_public_key=expected_head_public_key,
    )
    contents = dict(zip(bundle.manifest.segments, bundle.contents, strict=True))
    records_by_partition: dict[tuple[str, str, str, str], list[StoredProcedureJournalRecordV1]] = (
        defaultdict(list)
    )
    for descriptor, content in contents.items():
        key = (
            descriptor.stream.instance_id,
            descriptor.stream.journal_family,
            descriptor.stream.stream_id,
            descriptor.partition_id,
        )
        records_by_partition[key].extend(_records_from_segment(descriptor, content))

    imported: list[JournalPartitionHeadV1] = []
    for journal_range in bundle.manifest.ranges:
        key = (
            journal_range.stream.instance_id,
            journal_range.stream.journal_family,
            journal_range.stream.stream_id,
            journal_range.partition_id,
        )
        records = tuple(records_by_partition[key])
        verify_journal_range(journal_range, records)
        current = backend.read_head(journal_range.stream, journal_range.partition_id)
        if (
            current.sequence == journal_range.last_sequence
            and current.record_digest == journal_range.expected_head_digest
        ):
            local_range = backend.read_exact_range(journal_range)
            if local_range != records:
                raise PlaybillJournalError("journal import head matches but local prefix differs")
            imported.append(current)
            continue
        expected = JournalPartitionHeadV1(
            stream=journal_range.stream,
            partition_id=journal_range.partition_id,
            sequence=journal_range.first_sequence - 1,
            record_digest=journal_range.expected_previous_digest,
        )
        if current != expected:
            raise PlaybillJournalError("journal import refuses a missing prefix or fork merge")
        imported.append(backend.import_verified_range(records, expected_head=expected))
    return tuple(sorted(imported, key=journal_head_key))


def verified_journal_handoff(
    source: LocalJournalBackend,
    target: LocalJournalBackend,
    *,
    ranges: tuple[JournalRangeV1, ...],
    head_manifest: JournalHeadManifestV1,
    source_fencing_token: str,
    target_fencing_token: str,
    expected_head_public_key: str,
) -> tuple[JournalPartitionHeadV1, ...]:
    """Mirror exact prefixes, fence every old writer, then activate the new home."""

    bundle = build_journal_export(source, ranges=ranges, head_manifest=head_manifest)
    imported = import_journal_export(
        target,
        bundle,
        expected_head_public_key=expected_head_public_key,
    )
    for head in imported:
        if target.read_head(head.stream, head.partition_id) != head:
            raise PlaybillJournalError("handoff target failed complete-prefix verification")
    for head in imported:
        source.fence_writer(
            head.stream,
            head.partition_id,
            expected_fencing_token=source_fencing_token,
        )
    for head in imported:
        target.activate_writer(
            head.stream,
            head.partition_id,
            fencing_token=target_fencing_token,
            expected_head=head,
        )
    return imported


__all__ = [
    "JOURNAL_SEGMENT_BOUNDARY_RULE_V1",
    "JOURNAL_SEGMENT_MAX_BYTES_V1",
    "JOURNAL_SEGMENT_SEQUENCE_BUCKET_V1",
    "JournalExportBundleV1",
    "JournalExportV1",
    "JournalSegmentContentV1",
    "JournalSegmentDescriptorV1",
    "build_journal_export",
    "import_journal_export",
    "parse_journal_export",
    "render_journal_export",
    "verified_journal_handoff",
]
