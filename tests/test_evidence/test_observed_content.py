"""Observed content identity on Captures: what was seen, by content, whatever the envelope.

A workspace.file Capture records its source-read receipt's bytes digest; a
web.fetch v3 Capture records the origin body's digest, which core recomputes
and refuses on mismatch; every other Capture records none and reads as its
commitment. Absent, the envelope's bytes are exactly what they were.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    WEB_FETCH_V3_INTERFACE_DIGEST,
    CaptureFormatError,
    CaptureObservedContentMismatch,
    ProviderInvocationCaptureEvidence,
    ProviderProducerReceiptResolution,
    ProviderResultToExternalCapture,
    _verify_observed_content,
    build_provider_external_capture_v2,
    observed_content,
)
from cruxible_client.contracts.provider_execution import (
    ProviderInvocationOutputDigest,
    provider_invocation_output_digest,
)
from tests.core_support.p2b4_unit1._support import provider_capture_fixture
from tests.test_integration.p2b4_unit2.test_workspace_capture_receipt import _workspace_fixture


def _build(fixture: Any, **extra: Any) -> Any:
    return build_provider_external_capture_v2(
        store=fixture.store,
        contract=fixture.contract,
        result=fixture.result,
        receipt=fixture.receipt,
        occurrence=fixture.occurrence,
        producer=fixture.producer,
        bound_generation=fixture.bound_generation,
        **extra,
    )


def _web_fetch_fixture(tmp_path: Path, *, body: bytes, stated: str | None = None) -> Any:
    fixture = provider_capture_fixture(tmp_path)
    material = canonical_bytes(
        {
            "input_bucket": fixture.receipt.input_bucket,
            "retrieved": {
                "body_base64": base64.b64encode(body).decode("ascii"),
                "body_sha256": stated or "sha256:" + hashlib.sha256(body).hexdigest(),
            },
        }
    )
    result = ProviderResultToExternalCapture(
        **{
            **fixture.result.model_dump(mode="python"),
            "content_base64": base64.b64encode(material).decode("ascii"),
            "byte_length": len(material),
            "bytes_digest": "sha256:" + hashlib.sha256(material).hexdigest(),
        }
    )
    local = fixture.occurrence.local_execution.model_copy(
        update={"interface_digest": WEB_FETCH_V3_INTERFACE_DIGEST}
    )
    occurrence = fixture.occurrence.model_copy(
        update={"interface_digest": WEB_FETCH_V3_INTERFACE_DIGEST, "local_execution": local}
    )
    receipt = fixture.receipt.model_copy(
        update={
            "interface_digest": WEB_FETCH_V3_INTERFACE_DIGEST,
            "output": ProviderInvocationOutputDigest(
                output_digest=provider_invocation_output_digest(result.model_dump(mode="json"))
            ).model_dump(mode="json"),
        }
    )
    return replace(fixture, occurrence=occurrence, receipt=receipt, result=result)


def test_a_workspace_capture_records_the_bytes_it_read(tmp_path: Path) -> None:
    fixture, source_read, _output = _workspace_fixture(tmp_path)
    built = _build(fixture, source_read_receipt=source_read)
    assert built.envelope.observed_content_digest == source_read.bytes_digest
    seen = observed_content(built.envelope)
    assert (seen.digest, seen.basis) == (source_read.bytes_digest, "workspace_file_bytes")


def test_a_web_fetch_capture_records_the_verified_body(tmp_path: Path) -> None:
    body = b"<html>CVE-2026-0001</html>"
    built = _build(_web_fetch_fixture(tmp_path, body=body))
    expected = "sha256:" + hashlib.sha256(body).hexdigest()
    assert built.envelope.observed_content_digest == expected
    assert observed_content(built.envelope).basis == "web_fetch_body"


def test_a_web_fetch_body_digest_that_does_not_reproduce_refuses(tmp_path: Path) -> None:
    fixture = _web_fetch_fixture(tmp_path, body=b"real", stated="sha256:" + "0" * 64)
    with pytest.raises(CaptureObservedContentMismatch) as refused:
        _build(fixture)
    assert refused.value.error_code == "cruxible.capture.observed_content_mismatch"


def test_any_other_capture_records_none_and_keeps_its_bytes(tmp_path: Path) -> None:
    built = _build(provider_capture_fixture(tmp_path))
    assert built.envelope.observed_content_digest is None
    assert "observed_content_digest" not in built.envelope.model_dump(mode="json")
    seen = observed_content(built.envelope)
    assert (seen.digest, seen.basis) == (built.envelope.commitment.digest, "commitment")


def test_verification_refuses_an_observed_digest_the_evidence_does_not_yield(
    tmp_path: Path,
) -> None:
    fixture, source_read, _output = _workspace_fixture(tmp_path)
    built = _build(fixture, source_read_receipt=source_read)
    resolution = ProviderProducerReceiptResolution(
        receipt=fixture.receipt, occurrence=fixture.occurrence, source_read_receipt=source_read
    )
    _verify_observed_content(built.envelope, b"", resolution)
    forged = built.envelope.model_copy(update={"observed_content_digest": "sha256:" + "1" * 64})
    with pytest.raises(CaptureFormatError, match="observed_content_digest does not correspond"):
        _verify_observed_content(forged, b"", resolution)
    assert isinstance(built.envelope.production_evidence, ProviderInvocationCaptureEvidence)


def test_coverage_groups_two_captures_of_the_same_content_as_one_row(tmp_path: Path) -> None:
    import sqlite3

    from cruxible_client.contracts.captures import capture_contract_digest, capture_contract_path
    from cruxible_client.contracts.captures import render_capture_contract as render_contract
    from cruxible_core.indexes.evidence.citation_coverage import coverage_rows
    from cruxible_core.indexes.evidence.citation_sql import SCHEMA_SQL
    from tests.test_evidence.test_citation_sql import AT, World

    fixture, source_read, output = _workspace_fixture(tmp_path)
    first = _build(fixture, source_read_receipt=source_read)
    # The same file bytes read by another run: its derived request differs, so
    # the material echoing it -- and the commitment -- differs; the content does not.
    reread = source_read.model_copy(
        update={"derived_request_digest": "sha256:" + hashlib.sha256(b"rerun").hexdigest()}
    )
    material = canonical_bytes(
        {
            **output,
            "source": {**output["source"], "commitment_digest": reread.derived_request_digest},
        }
    )
    result = fixture.result.model_copy(
        update={
            "content_base64": base64.b64encode(material).decode("ascii"),
            "byte_length": len(material),
            "bytes_digest": "sha256:" + hashlib.sha256(material).hexdigest(),
        }
    )
    receipt = fixture.receipt.model_copy(
        update={
            "output": ProviderInvocationOutputDigest(
                output_digest=provider_invocation_output_digest(json.loads(material))
            ).model_dump(mode="json")
        }
    )
    second = _build(replace(fixture, result=result, receipt=receipt), source_read_receipt=reread)
    assert first.envelope.commitment.digest != second.envelope.commitment.digest
    assert first.envelope.observed_content_digest == second.envelope.observed_content_digest

    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE claims(identity TEXT PRIMARY KEY,path TEXT,"
        "artifact_digest TEXT,lifecycle TEXT) STRICT;"
        "CREATE TABLE capture_contracts(identity TEXT PRIMARY KEY,path TEXT,"
        "artifact_digest TEXT) STRICT;" + SCHEMA_SQL
    )
    world = World(connection, fixture.store)
    path = capture_contract_path(fixture.contract.identity.name)
    world.sources[path] = render_contract(fixture.contract)
    connection.execute(
        "INSERT INTO capture_contracts VALUES (?,?,?)",
        (
            fixture.contract.identity.qualified,
            path,
            capture_contract_digest(fixture.contract).tagged,
        ),
    )
    world.publish(world.claim(1, [first.capture_digest]), world.claim(2, [second.capture_digest]))
    by_content, _ = coverage_rows(world.reader, bodies=fixture.store, at=AT, version=2)
    (row,) = by_content.citations
    assert set(row.capture_digests) == {first.capture_digest, second.capture_digest}
    assert row.observed_content_digest == source_read.bytes_digest
    by_commitment, _ = coverage_rows(world.reader, bodies=fixture.store, at=AT, version=1)
    assert len(by_commitment.citations) == 2
