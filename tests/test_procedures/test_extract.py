"""The extract runtime: an accepted CaptureContract's decoding, typed records, one cache.

These run the extractor itself over a KEV-shaped catalog; the served run lane
that feeds it a Capture is covered in ``test_extract_runs``.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import pytest

from cruxible_client.contracts.captures import (
    CaptureCsvDecoding,
    CaptureDecoding,
    CaptureDecodingLimits,
)
from cruxible_client.contracts.procedures.extract import ExtractNode
from cruxible_client.contracts.shapes import (
    DuplicateArm,
    FieldAbsent,
    FieldInvalid,
    FieldPresent,
    InvalidRecordArm,
    RecordArm,
    validate_contract,
)
from cruxible_core.procedures import extract
from cruxible_core.procedures.extract import ExtractRefused, run_extract

KEV_ROW = validate_contract(
    {
        "name": "security.kev-entry",
        "version": 1,
        "fields": {
            "date_added": "date?",
            "due_date": "date?",
            "ransomware_use": "enum_of security.vulnerability.kev_ransomware_use?",
            "vendor_project": "string",
        },
    }
)
RANSOMWARE = ("Known", "Unknown")
JSON = CaptureDecoding(
    format="json",
    limits=CaptureDecodingLimits(decoded_bytes=1 << 20, depth=16, records=1000),
)


def _enums(name: str) -> tuple[str, ...] | None:
    return RANSOMWARE if name == "security.vulnerability.kev_ransomware_use" else None


def _entry(cve: str, **overrides: Any) -> dict[str, Any]:
    entry = {
        "cveID": cve,
        "vendorProject": "Acme",
        "dateAdded": "2026-09-01",
        "dueDate": "2026-09-22",
        "knownRansomwareCampaignUse": "Unknown",
    }
    entry.update(overrides)
    return {key: value for key, value in entry.items() if value is not None}


def _catalog(*entries: dict[str, Any], count: int | None = None, **header: Any) -> bytes:
    document: dict[str, Any] = {
        "catalogVersion": "2026.09.30",
        "dateReleased": "2026-09-30T12:00:00.000000Z",
        "count": len(entries) if count is None else count,
        "vulnerabilities": list(entries),
    }
    document.update(header)
    return json.dumps({k: v for k, v in document.items() if v is not None}).encode()


def _node(**overrides: Any) -> ExtractNode:
    spec: dict[str, Any] = {
        "kind": "extract",
        "node_id": "entries",
        "capture": "$steps.catalog.capture",
        "records": "/vulnerabilities/*",
        "complete_when": {"count_at": "/count"},
        "key": {"cve_id": {"at": "cveID", "type": "string"}},
        "duplicates": "refuse_key",
        "row": {"name": "security.kev-entry", "version": 1},
        "fields": {
            "date_added": {"at": "dateAdded", "parse": "date", "presence": "optional"},
            "due_date": {"at": "dueDate", "parse": "date", "presence": "optional"},
            "ransomware_use": {
                "at": "knownRansomwareCampaignUse",
                "canonicalize": "claim_type",
                "presence": "optional",
            },
            "vendor_project": {"at": "vendorProject", "presence": "required"},
        },
        "header": {
            "catalog_version": {"at": "/catalogVersion", "presence": "required"},
            "date_released": {
                "at": "/dateReleased",
                "parse": "datetime",
                "presence": "required",
            },
        },
        "as": "entries",
    }
    spec.update(overrides)
    return ExtractNode.model_validate(spec)


def _run(content: bytes, node: ExtractNode | None = None, **kwargs: Any):  # type: ignore[no-untyped-def]
    return run_extract(
        node or _node(),
        capture_digest="sha256:" + __import__("hashlib").sha256(content).hexdigest(),
        content=content,
        decoding=kwargs.pop("decoding", JSON),
        row=KEV_ROW,
        enums=_enums,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    extract.clear_extract_cache()
    extract.STATS.reset()


def test_records_carry_their_locator_typed_fields_key_and_header() -> None:
    outcome = _run(_catalog(_entry("CVE-2026-0001"), _entry("CVE-2026-0002", dueDate=None)))

    assert outcome.complete
    first, second = outcome.records
    assert first.record == "/vulnerabilities/0" and first.key == {"cve_id": "CVE-2026-0001"}
    added = first.fields["date_added"]
    assert isinstance(added, FieldPresent) and added.value.isoformat() == "2026-09-01"
    assert added.at == "/vulnerabilities/0/dateAdded"
    assert first.fields["ransomware_use"] == FieldPresent(
        value="Unknown", at="/vulnerabilities/0/knownRansomwareCampaignUse"
    )
    assert isinstance(second.fields["due_date"], FieldAbsent)
    assert isinstance(outcome.header["catalog_version"], FieldPresent)
    released = outcome.header["date_released"]
    assert isinstance(released, FieldPresent)
    value = outcome.value()
    assert value["rows"][0]["date_added"] == "2026-09-01"
    assert value["count"] == 2 and value["complete"] is True


def test_a_count_mismatch_leaves_the_extract_incomplete() -> None:
    outcome = _run(_catalog(_entry("CVE-2026-0001"), count=5))

    assert not outcome.complete_when_held and not outcome.complete
    assert len(outcome.records) == 1


def test_an_out_of_enum_value_is_invalid_while_its_siblings_stay_present() -> None:
    outcome = _run(_catalog(_entry("CVE-2026-0001", knownRansomwareCampaignUse="Maybe")))

    (record,) = outcome.records
    use = record.fields["ransomware_use"]
    assert isinstance(use, FieldInvalid) and use.problem.code == "not_in_enum"
    assert isinstance(record.fields["date_added"], FieldPresent)
    assert isinstance(record.fields["due_date"], FieldPresent)
    # The record still reads; its invalid field is reported, never guessed.
    assert outcome.invalid_fields == {"ransomware_use": 1}


def test_a_selector_that_matches_nothing_reads_no_records_and_is_incomplete() -> None:
    outcome = _run(
        json.dumps(
            {"catalogVersion": "v", "dateReleased": "2026-09-30T12:00:00.000000Z", "count": 0}
        ).encode()
    )

    assert outcome.records == [] and not outcome.complete


def test_canonicalize_matches_an_enum_member_case_insensitively() -> None:
    outcome = _run(_catalog(_entry("CVE-2026-0001", knownRansomwareCampaignUse="known")))

    assert outcome.records[0].fields["ransomware_use"] == FieldPresent(
        value="Known", at="/vulnerabilities/0/knownRansomwareCampaignUse"
    )


def test_a_duplicate_key_is_duplicate_on_both_records_and_nothing_reads_for_it() -> None:
    outcome = _run(
        _catalog(_entry("CVE-2026-0001"), _entry("CVE-2026-0002"), _entry("CVE-2026-0001"))
    )

    duplicates = [item for item in outcome.results if isinstance(item, DuplicateArm)]
    assert [item.record for item in duplicates] == ["/vulnerabilities/0", "/vulnerabilities/2"]
    assert all(item.first == "/vulnerabilities/0" for item in duplicates)
    assert [record.key["cve_id"] for record in outcome.records] == ["CVE-2026-0002"]
    assert not outcome.complete


def test_duplicates_first_keeps_the_first_and_refuse_run_refuses() -> None:
    content = _catalog(_entry("CVE-2026-0001"), _entry("CVE-2026-0001", vendorProject="Other"))
    kept = _run(content, _node(duplicates="first"))
    assert [record.record for record in kept.records] == ["/vulnerabilities/0"]
    assert kept.complete  # first is a declared choice, not an incomplete read

    with pytest.raises(ExtractRefused, match="extract_duplicate_key"):
        _run(content, _node(duplicates="refuse_run"))


def test_a_missing_key_is_an_invalid_record_not_a_guess() -> None:
    outcome = _run(_catalog({"vendorProject": "Acme"}, _entry("CVE-2026-0002")))

    invalid = [item for item in outcome.results if isinstance(item, InvalidRecordArm)]
    assert invalid[0].problems[0].code == "key_absent"
    assert not outcome.complete


def test_a_missing_required_header_field_refuses_the_node() -> None:
    with pytest.raises(ExtractRefused) as caught:
        _run(_catalog(_entry("CVE-2026-0001"), catalogVersion=None))

    assert caught.value.code == "extract.header_invalid@1"
    assert caught.value.details == {"field": "catalog_version", "code": "absent_required"}


def test_an_invalid_header_lexeme_refuses_with_it() -> None:
    with pytest.raises(ExtractRefused) as caught:
        _run(_catalog(_entry("CVE-2026-0001"), dateReleased="yesterday"))

    assert caught.value.details == {
        "field": "date_released",
        "code": "type_mismatch",
        "lexeme": "yesterday",
    }


CSV_ROW = validate_contract(
    {"name": "inventory.row", "version": 1, "fields": {"qty": "int", "name": "string"}}
)


def _csv_node() -> ExtractNode:
    return ExtractNode.model_validate(
        {
            "kind": "extract",
            "node_id": "rows",
            "capture": "$steps.file.capture",
            "records": "/*",
            "key": {"sku": {"at": "sku"}},
            "row": {"name": "inventory.row", "version": 1},
            "fields": {"qty": {"at": "qty", "parse": "int"}, "name": {"at": "name"}},
            "as": "rows",
        }
    )


def _csv(limit: int, *, encoding: str = "identity") -> CaptureDecoding:
    return CaptureDecoding(
        format="csv",
        encoding=encoding,  # type: ignore[arg-type]
        csv=CaptureCsvDecoding(comment_prefix="#", header=True),
        limits=CaptureDecodingLimits(decoded_bytes=limit, depth=4, records=10_000),
    )


def test_csv_rows_are_complete_when_the_header_is_present_and_every_row_reads() -> None:
    content = b"# inventory\nsku,qty,name\nA1,3,bolt\nB2,x,nut\n"
    outcome = run_extract(
        _csv_node(),
        capture_digest="sha256:" + "1" * 64,
        content=content,
        decoding=_csv(1 << 16),
        row=CSV_ROW,
    )
    assert outcome.complete
    first, second = outcome.records
    assert first.record == "/rows/0" and first.fields["qty"].value == 3  # type: ignore[union-attr]
    assert isinstance(second.fields["qty"], FieldInvalid)


def test_a_csv_without_its_declared_header_is_incomplete() -> None:
    outcome = run_extract(
        _csv_node(),
        capture_digest="sha256:" + "2" * 64,
        content=b"",
        decoding=_csv(1 << 16),
        row=CSV_ROW,
    )
    assert not outcome.complete


def test_a_gzip_csv_over_its_decoded_limit_refuses() -> None:
    body = b"sku,qty,name\n" + b"".join(b"S%d,1,x\n" % index for index in range(5000))
    content = gzip.compress(body)
    assert len(content) < 32768 < len(body)

    with pytest.raises(ExtractRefused) as caught:
        run_extract(
            _csv_node(),
            capture_digest="sha256:" + "3" * 64,
            content=content,
            decoding=_csv(32768, encoding="gzip"),
            row=CSV_ROW,
        )
    assert caught.value.code == "decoded_limit_exceeded"

    roomy = run_extract(
        _csv_node(),
        capture_digest="sha256:" + "3" * 64,
        content=content,
        decoding=_csv(1 << 20, encoding="gzip"),
        row=CSV_ROW,
    )
    assert len(roomy.records) == 5000


def test_a_contract_without_decoding_is_refused() -> None:
    with pytest.raises(ExtractRefused, match="extract_decoding_undeclared"):
        _run(_catalog(_entry("CVE-2026-0001")), decoding=None)


def test_one_capture_decodes_once_across_completeness_specs_and_watch_sets() -> None:
    """R02: each layer is keyed by its own declarations; only_keys is never a key."""

    content = _catalog(*(_entry(f"CVE-2026-{index:04d}") for index in range(1, 6)))
    counted = _node()
    missing_count = _node(complete_when={"count_at": "/total"})

    assert _run(content, counted).complete
    assert not _run(content, missing_count).complete
    assert extract.STATS.decodes == 1
    assert extract.STATS.evaluations == 2

    watch_a = _run(content, counted, only_keys=[{"cve_id": "CVE-2026-0002"}])
    watch_b = _run(
        content, counted, only_keys=[{"cve_id": "CVE-2026-0005"}, {"cve_id": "CVE-2026-0001"}]
    )
    assert [r.key["cve_id"] for r in watch_a.records] == ["CVE-2026-0002"]
    assert [r.key["cve_id"] for r in watch_b.records] == ["CVE-2026-0001", "CVE-2026-0005"]
    assert extract.STATS.decodes == 1 and extract.STATS.evaluations == 2
    # Fields are read once per (declaration, record): the watch sets reuse them.
    assert extract.STATS.field_reads == 5 * 4
    assert isinstance(watch_a.results[0], RecordArm)


def _rerun_field(content: bytes, row: Any, *, enums: Any = _enums, **overrides: Any):  # type: ignore[no-untyped-def]
    return run_extract(
        _node(**overrides),
        capture_digest="sha256:" + __import__("hashlib").sha256(content).hexdigest(),
        content=content,
        decoding=JSON,
        row=row,
        enums=enums,
    )


def _row(**fields: str) -> Any:
    declared = {
        "date_added": "date?",
        "due_date": "date?",
        "ransomware_use": "enum_of security.vulnerability.kev_ransomware_use?",
        "vendor_project": "string",
    }
    declared.update(fields)
    return validate_contract({"name": "security.kev-entry", "version": 1, "fields": declared})


def test_a_warm_field_cache_never_serves_a_result_typed_under_another_declaration() -> None:
    """F1: the field layer is keyed by the effective type, requiredness, name and enums."""

    content = _catalog(_entry("CVE-2026-0001", vendorProject="7", dueDate=None))
    fields = {
        "vendor_project": {"at": "vendorProject", "presence": "required"},
        "due_date": {"at": "dueDate", "parse": "date", "presence": "optional"},
        "ransomware_use": {"at": "knownRansomwareCampaignUse", "presence": "optional"},
    }

    first = _rerun_field(content, _row(), fields=fields).records[0].fields
    assert isinstance(first["vendor_project"], FieldPresent)
    assert isinstance(first["due_date"], FieldAbsent)
    assert isinstance(first["ransomware_use"], FieldPresent)

    # The same Capture and path under an int row type is a different key.
    as_int = _rerun_field(content, _row(vendor_project="int"), fields=fields).records[0].fields
    assert isinstance(as_int["vendor_project"], FieldInvalid)
    assert as_int["vendor_project"].problem.code == "type_mismatch"

    # A cached optional absence never answers for a required field.
    required = {**fields, "due_date": {"at": "dueDate", "parse": "date", "presence": "required"}}
    due = _rerun_field(content, _row(), fields=required).records[0].fields["due_date"]
    assert isinstance(due, FieldInvalid) and due.problem.code == "absent_required"

    # A field read under another name names itself in its problem.
    renamed_row = _row(vendor_id="int")
    renamed = {**fields, "vendor_id": {"at": "vendorProject", "presence": "required"}}
    renamed.pop("vendor_project")
    renamed_result = (
        _rerun_field(content, renamed_row, fields=renamed).records[0].fields["vendor_id"]
    )
    assert isinstance(renamed_result, FieldInvalid)
    assert renamed_result.problem.field == "vendor_id"

    # A changed enum membership is a different key: "Unknown" leaves the enum.
    narrowed = _rerun_field(content, _row(), enums=lambda _name: ("Known",), fields=fields)
    use = narrowed.records[0].fields["ransomware_use"]
    assert isinstance(use, FieldInvalid) and use.problem.code == "not_in_enum"
    assert extract.STATS.decodes == 1


def test_a_changed_nested_contract_is_a_different_field_key() -> None:
    """F1: a nested Contract's resolved shape is part of the field typing."""

    content = json.dumps(
        {"count": 1, "vulnerabilities": [{"cveID": "CVE-1", "vendor": {"name": "Acme"}}]}
    ).encode()
    loose = validate_contract({"name": "vendor", "version": 1, "fields": {"name": "string"}})
    strict = validate_contract(
        {"name": "vendor", "version": 1, "fields": {"name": "string", "id": "int"}}
    )
    row = validate_contract(
        {"name": "security.kev-entry", "version": 1, "fields": {"vendor": "vendor@1"}}
    )
    node = _node(
        fields={"vendor": {"at": "vendor", "presence": "required"}}, header={}, complete_when=None
    )

    def run(nested: Any) -> Any:
        return (
            run_extract(
                node,
                capture_digest="sha256:" + "0" * 64,
                content=content,
                decoding=JSON,
                row=row,
                resolve=lambda _ref: nested,
            )
            .records[0]
            .fields["vendor"]
        )

    assert isinstance(run(loose), FieldPresent)
    result = run(strict)
    assert isinstance(result, FieldInvalid) and result.problem.code == "absent_required"


def test_a_changed_key_enum_membership_rebuilds_the_record_index() -> None:
    """F1: layer D is keyed by the key types as resolved, enum members included."""

    content = _catalog(_entry("CVE-2026-0001"))
    node = _node(
        key={"use": {"at": "knownRansomwareCampaignUse", "type": "enum_of k.use"}},
        duplicates="first",
    )

    def run(members: tuple[str, ...]) -> Any:
        return run_extract(
            node,
            capture_digest="sha256:" + "1" * 64,
            content=content,
            decoding=JSON,
            row=KEV_ROW,
            enums=lambda name: members if name == "k.use" else RANSOMWARE,
        )

    assert run(("Unknown",)).records[0].key == {"use": "Unknown"}
    assert isinstance(run(("Known",)).results[0], InvalidRecordArm)
    assert extract.STATS.decodes == 2


@pytest.mark.parametrize("size", [5, 500])
def test_a_warm_watch_set_visits_only_its_keys_records_whatever_the_capture_holds(
    size: int,
) -> None:
    """F2: selection after layer D is O(|only_keys|): fixed watch sets cost the same."""

    content = _catalog(*(_entry(f"CVE-2026-{index:04d}") for index in range(1, size + 1)))
    _run(content)
    extract.STATS.reset()

    watched = _run(content, only_keys=[{"cve_id": "CVE-2026-0003"}, {"cve_id": "CVE-2026-0001"}])
    assert [record.key["cve_id"] for record in watched.records] == [
        "CVE-2026-0001",
        "CVE-2026-0003",
    ]
    assert extract.STATS.d_hits == 1 and extract.STATS.decodes == 0
    assert extract.STATS.selected == 2
    assert extract.STATS.field_reads == 0 and extract.STATS.f_hits == 2 * 4

    extract.STATS.reset()
    empty = _run(content, only_keys=[])
    assert empty.records == [] and extract.STATS.selected == 0
    assert extract.STATS.field_reads == 0 and extract.STATS.f_hits == 0

    extract.STATS.reset()
    unknown = _run(content, only_keys=[{"cve_id": "CVE-1999-0000"}])
    assert unknown.records == [] and extract.STATS.selected == 0
